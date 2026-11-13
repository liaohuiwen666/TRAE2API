"""curl_cffi transport helpers for requests made to Trae upstreams.

The desktop/web gateways fingerprint TLS clients.  ``httpx`` is retained for
the relay's own HTTP surface and type compatibility, but Trae-bound requests
go through libcurl with Chrome impersonation.  In particular, a configured
per-account proxy is passed to libcurl rather than falling back to Python's
standard TLS stack.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterator, Mapping

import httpx


IMPERSONATE_PROFILE = "chrome"


def _timeout_seconds(value: Any) -> Any:
    """Convert httpx's timeout object to curl_cffi's scalar timeout form."""

    if value is None or isinstance(value, (int, float)):
        return value
    # ``httpx.Timeout`` exposes individual phases. curl_cffi accepts one
    # deadline; preserve the longest bounded phase rather than silently
    # disabling a proxy's dead-connection protection.
    phases = [
        getattr(value, name, None)
        for name in ("connect", "read", "write", "pool")
    ]
    numeric = [float(item) for item in phases if isinstance(item, (int, float))]
    return max(numeric) if numeric else None


def _is_test_double(factory: Any) -> bool:
    """Keep the project's long-standing httpx monkeypatch seams usable.

    This branch only applies when a test/integration has replaced the httpx
    constructor.  Normal runtime constructors live in the ``httpx`` module,
    and always use curl_cffi below.
    """

    return not str(getattr(factory, "__module__", "")).startswith("httpx")


def is_transport_error(error: BaseException) -> bool:
    """Return whether an exception came from either supported HTTP stack.

    Callers used to catch only ``httpx.RequestError``.  The Chrome transport
    deliberately raises curl_cffi's own request/Curl errors, so treating them
    equivalently preserves proxy retry and candidate-host behavior without
    falling back to a non-impersonating transport.
    """

    if isinstance(error, httpx.RequestError):
        return True
    try:
        from curl_cffi import CurlError
        from curl_cffi.requests.errors import RequestsError
    except ImportError:
        return False
    return isinstance(error, (CurlError, RequestsError))


def _session_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(kwargs)
    values.pop("http2", None)
    values.pop("follow_redirects", None)
    values["timeout"] = _timeout_seconds(values.get("timeout"))
    values["impersonate"] = IMPERSONATE_PROFILE
    # curl_cffi accepts either ``proxy`` or ``proxies``. Existing account
    # routing passes the former, and it is intentionally preserved verbatim.
    return values


class CurlAsyncResponse:
    """Small httpx-shaped wrapper for curl_cffi streaming responses."""

    def __init__(self, response: Any):
        self._response = response
        self._body: bytes | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    async def aread(self) -> bytes:
        if self._body is None:
            self._body = b"".join(
                [chunk async for chunk in self._response.aiter_content()]
            )
        return self._body

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_content():
            yield chunk

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in self.aiter_raw():
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


class CurlAsyncClient:
    """Subset of ``httpx.AsyncClient`` used by the Trae transports."""

    def __init__(self, **kwargs: Any):
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:  # requirements makes this a deploy error.
            raise RuntimeError("curl_cffi is required for Trae TLS impersonation") from exc
        self._session = curl_requests.AsyncSession(**_session_kwargs(kwargs))
        self.impersonate = IMPERSONATE_PROFILE
        self.proxy = kwargs.get("proxy")

    async def __aenter__(self) -> "CurlAsyncClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._session.close()

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self.request("POST", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs["timeout"] = _timeout_seconds(kwargs.get("timeout"))
        return await self._session.request(method, url, **kwargs)

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs: Any) -> AsyncIterator[CurlAsyncResponse]:
        kwargs["timeout"] = _timeout_seconds(kwargs.get("timeout"))
        # curl_cffi 0.17 exposes ``AsyncSession.stream`` as an async context
        # manager (unlike an async generator).  Entering it directly lets
        # curl_cffi own response teardown when the caller leaves our httpx-
        # shaped stream context.
        async with self._session.stream(method, url, **kwargs) as response:
            yield CurlAsyncResponse(response)


@dataclass
class _CurlRequest:
    method: str
    url: str
    kwargs: dict[str, Any]


class CurlSyncResponse:
    """Small httpx-shaped wrapper for curl_cffi streaming responses."""

    def __init__(self, response: Any):
        self._response = response
        self._body: bytes | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def read(self) -> bytes:
        if self._body is None:
            self._body = b"".join(self._response.iter_content())
        return self._body

    def iter_raw(self) -> Iterator[bytes]:
        yield from self._response.iter_content()

    def iter_bytes(self) -> Iterator[bytes]:
        yield from self.iter_raw()


class CurlSyncClient:
    """Subset of ``httpx.Client`` used by raw and IDE Trae transports."""

    def __init__(self, **kwargs: Any):
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:
            raise RuntimeError("curl_cffi is required for Trae TLS impersonation") from exc
        self._session = curl_requests.Session(**_session_kwargs(kwargs))
        self.impersonate = IMPERSONATE_PROFILE
        self.proxy = kwargs.get("proxy")

    def build_request(self, method: str, url: str, **kwargs: Any) -> _CurlRequest:
        return _CurlRequest(method, url, kwargs)

    def send(self, request: _CurlRequest, *, stream: bool = False) -> CurlSyncResponse:
        values = dict(request.kwargs)
        values["stream"] = stream
        return CurlSyncResponse(self._session.request(request.method, request.url, **values))

    def close(self) -> None:
        self._session.close()


def upstream_async_client(**kwargs: Any) -> Any:
    """Build the mandatory Chrome-impersonating async upstream client."""

    # Existing external integrations and this repository's tests replace this
    # constructor to provide an in-memory transport. Keep that explicit seam;
    # it never selects stdlib TLS during ordinary runtime operation.
    if _is_test_double(httpx.AsyncClient):
        return httpx.AsyncClient(**kwargs)
    return CurlAsyncClient(**kwargs)


def upstream_sync_client(**kwargs: Any) -> Any:
    """Build the mandatory Chrome-impersonating sync upstream client."""

    if _is_test_double(httpx.Client):
        return httpx.Client(**kwargs)
    return CurlSyncClient(**kwargs)
