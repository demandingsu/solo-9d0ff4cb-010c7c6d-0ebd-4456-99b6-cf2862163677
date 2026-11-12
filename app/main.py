"""FastAPI web front end for the bid-deadline checker."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .crawler import check_lot, normalize_url

BASE_DIR = Path(__file__).resolve().parent
DEMO_INTERNAL_URL = os.getenv("DEMO_INTERNAL_URL", "http://localhost:8001").rstrip("/")
DEMO_PUBLIC_URL = os.getenv("DEMO_PUBLIC_URL", "http://localhost:8001").rstrip("/")
DEMO_LIST_URL = os.getenv(
    "DEMO_LIST_URL", f"{DEMO_PUBLIC_URL}/notices?page=1"
)
# The checker's own browser-visible address, used only to recognise URLs that
# point at this app's /demo proxy.  It is not used to rewrite other sites.
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "http://localhost:8000").rstrip("/")

# Hosts that unambiguously mean "this machine" in a browser.  They are only
# treated as demo aliases when they use the configured demo port.
LOCAL_HOST_ALIASES = frozenset({"localhost", "127.0.0.1", "::1"})


app = FastAPI(
    title="采购标段截标时间核验",
    description="跨页核对原公告、延期公告和终止公告中的最新投标截止时间",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    """Canonical (scheme, host, port), filling in the scheme's default port.

    An invalid explicit port yields ``-1`` so a malformed address can never be
    mistaken for a configured demo origin.
    """

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        return scheme, host, -1
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return scheme, host, port


def _same_origin(a: str, b: str) -> bool:
    return _origin(a) == _origin(b)


def _local_alias_origin(value: str) -> tuple[str, str, int | None] | None:
    """Return the canonical origin of a local alias URL, if it is one."""

    origin = _origin(value)
    if origin[1] not in LOCAL_HOST_ALIASES:
        return None
    return origin


def _is_local_alias(value: str, reference: str) -> bool:
    """True when both URLs name a local host with the same scheme and port."""

    candidate = _local_alias_origin(value)
    expected = _local_alias_origin(reference)
    return (
        candidate is not None
        and expected is not None
        and candidate[0] == expected[0]
        and candidate[2] == expected[2]
    )


def _demo_translation(
    source: str,
    *,
    app_origins: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Return (crawl_url, input_url) for an address the app may translate.

    Only addresses that explicitly belong to the configured demo deployment
    are translated; everything else keeps its own origin so the crawler and
    the result describe the site the user actually named.
    """

    normalized = normalize_url(source)
    parts = urlsplit(normalized)
    source_origin = _origin(normalized)
    internal_parts = urlsplit(DEMO_INTERNAL_URL)
    internal_origin = f"{internal_parts.scheme.lower()}://{internal_parts.netloc.lower()}"

    def demo_url(path: str, query: str) -> str:
        target = internal_origin + (path or "/")
        return target + ("?" + query if query else "")

    # The container-internal address is already what the crawler needs.
    if _same_origin(normalized, DEMO_INTERNAL_URL):
        return normalized, normalized

    # 1. This app's own /demo/... proxy path.  Only the app's configured
    #    origin (or the origin the current request arrived on) may use it;
    #    an external site that happens to have a /demo/ path is left alone.
    app_origin_values = (APP_PUBLIC_URL, *app_origins)
    on_app_origin = any(
        _same_origin(normalized, candidate)
        or _is_local_alias(normalized, candidate)
        for candidate in app_origin_values
        if candidate
    )
    if on_app_origin and (parts.path == "/demo" or parts.path.startswith("/demo/")):
        target_path = parts.path[5:] or "/"
        if not target_path.startswith("/"):
            target_path = "/" + target_path
        return demo_url(target_path, parts.query), normalized

    # The configured public demo origin, verbatim (also accept local aliases
    # on that exact scheme/port).  A different port is a different service and
    # must be fetched from its own address.
    public_origin = _origin(DEMO_PUBLIC_URL)
    accepted = source_origin == public_origin or (
        source_origin[1] in LOCAL_HOST_ALIASES
        and public_origin[1] in LOCAL_HOST_ALIASES
        and source_origin[0] == public_origin[0]
        and source_origin[2] == public_origin[2]
    )
    # A /demo path on the public demo origin is forwarded as-is: it is not
    # this app's proxied path (that was handled above).
    if accepted:
        return demo_url(parts.path, parts.query), normalized

    # Any other address is not the demo site: leave it untouched.
    return normalized, normalized


def demo_crawl_url(url: str, *, app_origins: tuple[str, ...] = ()) -> str:
    """Translate only explicitly recognised demo addresses to the demo site.

    Recognised forms are: the configured container-internal address, the
    configured public demo origin, this app's own ``/demo`` proxy path, and
    localhost/127.0.0.1/::1 using the configured demo public port.  Every other
    address (another host, another port, or an external site with a ``/demo``
    path) is crawled at its own origin.
    """

    return _demo_translation(url, app_origins=app_origins)[0]


def public_url_for(url: str | None) -> str | None:
    """Turn the container-only demo URL into a browser-friendly proxy URL."""

    if not url:
        return None
    try:
        source = urlsplit(url)
        demo = urlsplit(DEMO_INTERNAL_URL)
        if (
            source.scheme in {"http", "https"}
            and source.netloc.lower() == demo.netloc.lower()
        ):
            path = source.path or "/"
            proxied = "/demo" + (path if path.startswith("/") else "/" + path)
            if source.query:
                proxied += "?" + source.query
            return proxied
    except Exception:
        pass
    return url


templates.env.filters["public_url"] = public_url_for


@app.middleware("http")
async def disable_stale_results(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/check", "/api/check"} or request.url.path.startswith("/demo"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


class CheckPayload(BaseModel):
    list_url: str = Field(..., description="公告列表页地址")
    project_no: str = Field(..., description="项目编号")
    section_no: str = Field(..., description="标段号")


def _demo_presets() -> list[dict[str, str]]:
    presets = [
        {
            "name": "投标进行中（已延期）",
            "list_url": DEMO_LIST_URL,
            "project_no": "DEMO-2026-001",
            "section_no": "02",
            "note": "原公告第 1 页，延期公告第 2 页",
        },
        {
            "name": "已终止",
            "list_url": DEMO_LIST_URL,
            "project_no": "DEMO-2026-001",
            "section_no": "01",
            "note": "原公告、延期、终止分处 1/2/3 页；同项目 02 标段不得混入",
        },
        {
            "name": "不同编号格式",
            "list_url": DEMO_LIST_URL,
            "project_no": "DEMO-2026-002",
            "section_no": "A",
            "note": "A 包；结果公告不应改变截止时间",
        },
    ]
    for preset in presets:
        preset["href"] = "/check?" + urlencode(
            {
                "list_url": preset["list_url"],
                "project_no": preset["project_no"],
                "section_no": preset["section_no"],
            }
        )
    return presets


def _base_context(request: Request) -> dict:
    return {
        "request": request,
        "demo_list_url": DEMO_LIST_URL,
        "demo_public_url": "/demo/notices?page=1",
        "presets": _demo_presets(),
        "demo_available": bool(DEMO_INTERNAL_URL),
    }


def _request_origins(request: Request) -> tuple[str, ...]:
    """Origins this request arrived on, so its own /demo proxy is recognised.

    Reverse proxies may rewrite the host; the request base URL is the most
    faithful browser-visible origin.  A trailing slash is removed because
    origin comparison works on scheme + host + port.
    """

    try:
        return (str(request.base_url).rstrip("/"),)
    except Exception:
        return ()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        _base_context(request) | {"result": None, "form": {}, "form_error": None},
    )


@app.get("/check", response_class=HTMLResponse)
async def check_page(
    request: Request,
    list_url: str = "",
    project_no: str = "",
    section_no: str = "",
):
    form = {
        "list_url": list_url,
        "project_no": project_no,
        "section_no": section_no,
    }
    if not list_url.strip() or not project_no.strip() or not section_no.strip():
        return templates.TemplateResponse(
            request,
            "index.html",
            _base_context(request)
            | {
                "result": None,
                "form": form,
                "form_error": "请填写公告列表页地址、项目编号和标段号。",
            },
            status_code=400,
        )

    try:
        crawl_url, input_url = _demo_translation(
            list_url, app_origins=_request_origins(request)
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            _base_context(request)
            | {"result": None, "form": form, "form_error": str(exc)},
            status_code=400,
        )

    outcome = await check_lot(crawl_url, project_no, section_no)
    result = _result_payload(outcome, input_url)
    return templates.TemplateResponse(
        request,
        "index.html",
        _base_context(request) | {"result": result, "form": form, "form_error": None},
    )


def _result_payload(outcome, input_url: str) -> dict:
    """Serialise an outcome while keeping provenance tied to the input."""

    result = outcome.as_dict(public_url_for)
    result["input_url"] = input_url
    return result


async def _json_result(
    list_url: str, project_no: str, section_no: str, request: Request
) -> JSONResponse:
    try:
        crawl_url, input_url = _demo_translation(
            list_url, app_origins=_request_origins(request)
        )
    except ValueError:
        # Let check_lot produce its normal conservative "unknown" shape and
        # failed-address details for a malformed address.
        result = _result_payload(
            await check_lot(list_url, project_no, section_no), list_url
        )
        return JSONResponse(result, status_code=400)
    result = _result_payload(
        await check_lot(crawl_url, project_no, section_no), input_url
    )
    return JSONResponse(result)


@app.get("/api/check")
async def api_check(
    request: Request,
    list_url: Annotated[str, Query(description="公告列表页地址")],
    project_no: Annotated[str, Query(description="项目编号")],
    section_no: Annotated[str, Query(description="标段号")],
):
    return await _json_result(list_url, project_no, section_no, request)


@app.post("/api/check")
async def api_check_post(payload: CheckPayload, request: Request):
    return await _json_result(
        payload.list_url, payload.project_no, payload.section_no, request
    )


_ABSOLUTE_LINK_RE = re.compile(r'((?:href|src|action)=["\'])(/(?!/))', re.I)


def _rewrite_demo_html(html: str) -> str:
    """Make the demo site's root-absolute links work behind /demo."""

    return _ABSOLUTE_LINK_RE.sub(r"\1/demo\2", html)


@app.get("/demo", include_in_schema=False)
async def demo_root():
    return RedirectResponse(url="/demo/notices?page=1", status_code=307)


@app.get("/demo/{path:path}", include_in_schema=False)
async def demo_proxy(request: Request, path: str):
    target = DEMO_INTERNAL_URL + "/" + path.lstrip("/")
    if request.url.query:
        target += "?" + request.url.query

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            upstream = await client.get(target)
    except httpx.HTTPError:
        return HTMLResponse(
            "<h1>示例站暂时不可用</h1><p>请稍后再试，或直接访问示例站端口 8001。</p>",
            status_code=502,
        )

    content_type = upstream.headers.get("content-type", "application/octet-stream")
    content: bytes | str = upstream.content
    if "text/html" in content_type.lower():
        content = _rewrite_demo_html(upstream.text)

    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in {"cache-control", "etag", "last-modified"}
    }
    return Response(
        content=content,
        status_code=upstream.status_code,
        media_type=content_type.split(";")[0],
        headers=headers,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "bid-deadline-checker"}
