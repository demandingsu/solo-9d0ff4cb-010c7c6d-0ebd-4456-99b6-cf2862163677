"""Regression tests for demo-address translation and result provenance.

Only the configured demo deployment may be mapped to the bundled demo site.
Every other address must be crawled at its own origin, and a failed external
site must produce an indeterminate result pointing at the address the user
typed rather than at the local demo data.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.crawler import CheckOutcome, FetchIssue

# Mirror docker-compose.yml: the demo is reachable internally as demo:8000 and
# published to the browser as localhost:8001.
COMPOSE_ENV = {
    "DEMO_INTERNAL_URL": "http://demo:8000",
    "DEMO_PUBLIC_URL": "http://localhost:8001",
    "DEMO_LIST_URL": "http://localhost:8001/notices?page=1",
    "APP_PUBLIC_URL": "http://localhost:8000",
}


class _ReloadedModule:
    """Temporarily reload app.main with a given environment, then restore."""

    def __init__(self, env: dict[str, str] | None = None):
        self.env = env

    def __enter__(self):
        import app.main as main_module

        self._patch = mock.patch.dict("os.environ", self.env or {}, clear=False)
        self._patch.start()
        self.module = importlib.reload(main_module)
        return self.module

    def __exit__(self, *exc_info):
        self._patch.stop()
        importlib.reload(self.module)
        return False


def _unknown_outcome(list_url: str, project_no: str, section_no: str) -> CheckOutcome:
    return CheckOutcome(
        list_url=list_url,
        project_no=project_no,
        section_no=section_no,
        status_code="unknown",
        status_label="无法判定",
        determinable=False,
        effective_deadline=None,
        effective_deadline_raw=None,
        effective_deadline_has_time=False,
        deadline_source_url=None,
        deadline_source_title=None,
        reason="公告列表页抓取失败：网络请求失败",
        notices=[],
        changes=[],
        issues=[
            FetchIssue(
                url=list_url,
                reason="公告列表页抓取失败：网络请求失败",
                stage="list",
            )
        ],
        searched_pages=[list_url],
        checked_detail_urls=[],
        detail_page_count=0,
        warnings=[],
    )


class DemoTranslationTests(unittest.TestCase):
    def test_configured_demo_addresses_are_mapped(self):
        cases = {
            "http://localhost:8001/notices?page=1": "http://demo:8000/notices?page=1",
            "http://127.0.0.1:8001/notices?page=1": "http://demo:8000/notices?page=1",
            "http://[::1]:8001/notices?page=1": "http://demo:8000/notices?page=1",
            "http://demo:8000/notices?page=1": "http://demo:8000/notices?page=1",
            "http://localhost:8000/demo/notices?page=1": "http://demo:8000/notices?page=1",
        }
        with _ReloadedModule(COMPOSE_ENV) as main_module:
            for source, expected in cases.items():
                self.assertEqual(
                    main_module.demo_crawl_url(source), expected, msg=source
                )

    def test_other_origins_and_ports_are_never_mapped(self):
        with _ReloadedModule(COMPOSE_ENV) as main_module:
            untouched = [
                # Another host on the demo's port.
                "https://procurement.example.com:8001/notices?page=1",
                "http://evil.example:8001/notices?page=1",
                # An external site whose path happens to contain /demo/.
                "https://procurement.example.com/demo/notices?page=1",
                "http://other.example:8080/demo/list",
                # Different local port = different service.
                "http://localhost:9000/notices?page=1",
                "http://127.0.0.1:8002/notices?page=1",
                # The checker app on its normal port is not the demo site.
                "http://localhost:8000/notices?page=1",
                # The internal demo hostname is not a public demo alias.
                "http://demo:8001/notices?page=1",
                # Scheme aliases must not widen the demo origin.
                "https://localhost:8001/notices?page=1",
            ]
            for source in untouched:
                self.assertEqual(
                    main_module.demo_crawl_url(source), source, msg=source
                )


class ApiProvenanceTests(unittest.TestCase):
    """The result must describe the site that was actually crawled."""

    def _call(self, list_url: str):
        import app.main as main_module

        captured: list[str] = []

        async def fake_check_lot(crawl_url, project_no, section_no, **kwargs):
            captured.append(crawl_url)
            return _unknown_outcome(crawl_url, project_no, section_no)

        with mock.patch.object(main_module, "check_lot", new=fake_check_lot):
            response = TestClient(main_module.app).get(
                "/api/check",
                params={
                    "list_url": list_url,
                    "project_no": "DEMO-2026-001",
                    "section_no": "01",
                },
            )
        return response, captured

    def test_external_site_on_demo_port_is_crawled_at_its_own_origin(self):
        url = "https://procurement.example.com:8001/notices?page=1"
        response, captured = self._call(url)
        self.assertEqual(captured, [url])
        payload = response.json()
        self.assertEqual(payload["status"], "unknown")
        self.assertIsNone(payload["effective_deadline"])
        self.assertEqual(payload["input_url"], url)
        self.assertEqual(payload["failures"][0]["url"], url)

    def test_external_site_with_demo_path_is_not_rewritten(self):
        url = "https://procurement.example.com/demo/notices?page=1"
        response, captured = self._call(url)
        self.assertEqual(captured, [url])
        payload = response.json()
        self.assertFalse(payload["determinable"])
        self.assertIsNone(payload["effective_deadline"])
        self.assertEqual(payload["failures"][0]["url"], url)

    def test_configured_demo_address_is_still_served_offline(self):
        captured: list[str] = []

        async def fake_check_lot(crawl_url, project_no, section_no, **kwargs):
            captured.append(crawl_url)
            return _unknown_outcome(crawl_url, project_no, section_no)

        with _ReloadedModule(COMPOSE_ENV) as main_module:
            with mock.patch.object(main_module, "check_lot", new=fake_check_lot):
                response = TestClient(main_module.app).get(
                    "/api/check",
                    params={
                        "list_url": "http://localhost:8001/notices?page=1",
                        "project_no": "DEMO-2026-001",
                        "section_no": "01",
                    },
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, ["http://demo:8000/notices?page=1"])


class HtmlProvenanceTests(unittest.TestCase):
    """The rendered page must not present demo data for an external failure."""

    def test_external_failure_shows_unknown_and_original_address(self):
        import app.main as main_module

        url = "https://procurement.example.com:8001/notices?page=1"

        async def fake_check_lot(crawl_url, project_no, section_no, **kwargs):
            return _unknown_outcome(crawl_url, project_no, section_no)

        with mock.patch.object(main_module, "check_lot", new=fake_check_lot):
            response = TestClient(main_module.app).get(
                "/check",
                params={
                    "list_url": url,
                    "project_no": "DEMO-2026-001",
                    "section_no": "01",
                },
            )
        body = response.text
        self.assertEqual(response.status_code, 200)
        self.assertIn("无法判定", body)
        self.assertIn(url, body)
        # The local demo's current deadline must never leak into this result.
        self.assertNotIn("2026-11-12 09:30", body)


class PublicUrlTests(unittest.TestCase):
    def test_internal_demo_urls_become_proxy_links(self):
        from app.main import public_url_for

        self.assertEqual(
            public_url_for("http://localhost:8001/notices?page=1"),
            "/demo/notices?page=1",
        )

    def test_external_urls_are_kept_verbatim(self):
        from app.main import public_url_for

        external = "https://procurement.example.com:8001/notices?page=1"
        self.assertEqual(public_url_for(external), external)


if __name__ == "__main__":
    unittest.main()
