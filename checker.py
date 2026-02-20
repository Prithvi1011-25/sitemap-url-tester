"""
checker.py – Async URL status checker (v4)
============================================
Uses httpx.AsyncClient + asyncio.Semaphore to check many URLs concurrently.

Key features:
  • Uses make_headers() from headers.py for browser-realistic requests
  • Tracks first_status_code (before redirects) AND final_status_code
  • Full redirect chain stored for diagnostics
  • HEAD first; if HEAD returns >= 400, falls back to GET
  • Safari UA retry: if 403/404 with primary UA, retries with Safari UA
  • Soft-404 detection on 200 responses
  • Jitter + exponential backoff on retries
  • Logs user_agent_used and method_used per URL
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from headers import make_headers, SAFARI_UA

# Patterns that indicate a soft-404 page (checked against first 8KB of body)
_SOFT_404_PATTERNS = [
    "page not found",
    "404 not found",
    "404 error",
    "not found</title>",
    "<title>404",
    "does not exist",
    "no longer available",
    "page doesn't exist",
    "page does not exist",
    "we couldn't find",
    "we can't find",
]

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    input_url: str
    first_status_code: str = ""     # Status of the FIRST hop (before redirects)
    final_status_code: str = ""     # Status after following all redirects
    final_url: str = ""
    response_time_ms: float = 0.0
    redirect_count: int = 0
    redirect_chain: str = ""        # e.g. "301→302→200"
    method_used: str = ""           # "HEAD" or "GET"
    user_agent_used: str = ""       # which UA produced the final result
    soft_404: bool = False
    error: str = ""
    # Safari retry fields
    alt_status_code: str = ""       # status from Safari UA retry (if done)
    alt_user_agent_used: str = ""   # "Safari macOS" if retry happened

    def to_dict(self) -> dict:
        return {
            "input_url": self.input_url,
            "first_status_code": self.first_status_code,
            "final_status_code": self.final_status_code,
            "final_url": self.final_url,
            "response_time_ms": round(self.response_time_ms, 1),
            "redirect_count": self.redirect_count,
            "redirect_chain": self.redirect_chain,
            "method_used": self.method_used,
            "user_agent_used": self.user_agent_used,
            "soft_404": self.soft_404,
            "error": self.error,
            "alt_status_code": self.alt_status_code,
            "alt_user_agent_used": self.alt_user_agent_used,
        }


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def _classify_error(exc: Exception) -> str:
    """Map an exception to a human-readable error label."""
    name = type(exc).__name__
    msg = str(exc).lower()
    if "ssl" in name.lower() or "ssl" in msg:
        return "SSL_ERROR"
    if "timeout" in name.lower() or "timeout" in msg:
        return "TIMEOUT"
    if "dns" in msg or "name or service not known" in msg or "nodename nor servname" in msg:
        return "DNS_ERROR"
    if "connect" in name.lower() or "connect" in msg:
        return "CONNECT_ERROR"
    if "read" in name.lower():
        return "READ_ERROR"
    return f"ERROR ({name})"


def _detect_soft_404(content_type: str, body_snippet: str) -> bool:
    """Return True if a 200 response looks like a soft-404."""
    if "text/html" not in content_type:
        return False
    lower = body_snippet.lower()
    return any(pat in lower for pat in _SOFT_404_PATTERNS)


def _build_redirect_chain(resp: httpx.Response) -> str:
    """Build a readable redirect chain string like '301→302→200'."""
    if not resp.history:
        return str(resp.status_code)
    codes = [str(r.status_code) for r in resp.history]
    codes.append(str(resp.status_code))
    return "→".join(codes)


def _get_first_status(resp: httpx.Response) -> str:
    """Return the status code of the first response (before any redirects)."""
    if resp.history:
        return str(resp.history[0].status_code)
    return str(resp.status_code)


# ---------------------------------------------------------------------------
# Core request logic (shared by primary + Safari retry)
# ---------------------------------------------------------------------------

async def _do_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    head_then_get: bool,
) -> httpx.Response:
    """
    Make HEAD (optional) → GET request and return the final response.
    If HEAD returns >= 400, falls back to GET automatically.
    """
    if head_then_get:
        resp = await client.request("HEAD", url, headers=headers)
        if resp.status_code >= 400:
            resp = await client.request("GET", url, headers=headers)
        return resp
    else:
        return await client.request("GET", url, headers=headers)


def _method_from_resp(resp: httpx.Response) -> str:
    """Determine if final request was HEAD or GET."""
    return resp.request.method  # httpx tracks actual method


# ---------------------------------------------------------------------------
# Single-URL check
# ---------------------------------------------------------------------------

async def _check_one(
    client: httpx.AsyncClient,
    url: str,
    sem: asyncio.Semaphore,
    primary_headers: dict,
    primary_ua_label: str,
    head_then_get: bool,
    retries: int,
    safari_retry: bool,
) -> CheckResult:
    """Check a single URL with concurrency control, retries, and Safari fallback."""
    result = CheckResult(input_url=url)

    async with sem:
        last_exc: Exception | None = None
        elapsed: float = 0.0

        for attempt in range(1 + retries):
            t0 = time.perf_counter()
            try:
                resp = await _do_request(client, url, primary_headers, head_then_get)
                elapsed = (time.perf_counter() - t0) * 1000

                result.first_status_code = _get_first_status(resp)
                result.final_status_code = str(resp.status_code)
                result.final_url = str(resp.url)
                result.response_time_ms = elapsed
                result.redirect_count = len(resp.history)
                result.redirect_chain = _build_redirect_chain(resp)
                result.method_used = _method_from_resp(resp)
                result.user_agent_used = primary_ua_label

                # Soft-404 detection (GET only)
                if result.method_used == "GET" and resp.status_code == 200:
                    ct = resp.headers.get("content-type", "")
                    body_snippet = (resp.text or "")[:8000]
                    result.soft_404 = _detect_soft_404(ct, body_snippet)

                # ── Safari UA retry on 403/404 ──
                if safari_retry and resp.status_code in (403, 404):
                    safari_headers = make_headers(SAFARI_UA)
                    try:
                        alt_resp = await _do_request(
                            client, url, safari_headers, head_then_get
                        )
                        result.alt_status_code = str(alt_resp.status_code)
                        result.alt_user_agent_used = "Safari macOS"

                        # If Safari got a better result, use it as the final
                        if alt_resp.status_code < 400:
                            result.first_status_code = _get_first_status(alt_resp)
                            result.final_status_code = str(alt_resp.status_code)
                            result.final_url = str(alt_resp.url)
                            result.redirect_count = len(alt_resp.history)
                            result.redirect_chain = _build_redirect_chain(alt_resp)
                            result.method_used = _method_from_resp(alt_resp)
                            result.user_agent_used = "Safari macOS (retry)"
                    except Exception:
                        pass  # Safari retry is best-effort

                return result

            except Exception as exc:
                last_exc = exc
                elapsed = (time.perf_counter() - t0) * 1000
                if attempt < retries:
                    backoff = (0.5 * (2 ** attempt)) + random.uniform(0, 0.3)
                    await asyncio.sleep(backoff)

        # All attempts exhausted
        error_label = _classify_error(last_exc)  # type: ignore[arg-type]
        result.final_status_code = error_label
        result.error = error_label
        result.response_time_ms = elapsed
        result.user_agent_used = primary_ua_label
        return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def _run(
    urls: list[str],
    *,
    concurrency: int = 10,
    timeout: float = 15,
    follow_redirects: bool = True,
    head_then_get: bool = True,
    retries: int = 1,
    user_agent: str = "",
    safari_retry: bool = True,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """Check all *urls* asynchronously and return a list of result dicts."""
    from headers import UA_PRESETS

    if not user_agent:
        user_agent = UA_PRESETS["Chrome macOS (default)"]

    # Determine a short label for the UA
    ua_label = "Custom"
    for name, val in UA_PRESETS.items():
        if val == user_agent:
            ua_label = name
            break

    primary_headers = make_headers(user_agent)
    sem = asyncio.Semaphore(concurrency)
    transport = httpx.AsyncHTTPTransport(retries=0)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=follow_redirects,
        transport=transport,
        verify=True,
    ) as client:
        done_count = 0

        async def _wrapped(u: str) -> CheckResult:
            nonlocal done_count
            r = await _check_one(
                client, u, sem, primary_headers, ua_label,
                head_then_get, retries, safari_retry,
            )
            done_count += 1
            if progress_callback:
                progress_callback(done_count, len(urls))
            return r

        tasks = [asyncio.create_task(_wrapped(u)) for u in urls]
        results = await asyncio.gather(*tasks)

    return [r.to_dict() for r in results]


def run_checks(
    urls: list[str],
    *,
    concurrency: int = 10,
    timeout: float = 15,
    follow_redirects: bool = True,
    head_then_get: bool = True,
    retries: int = 1,
    user_agent: str = "",
    safari_retry: bool = True,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """Synchronous wrapper – safe to call from Streamlit."""
    import nest_asyncio
    try:
        loop = asyncio.get_running_loop()
        nest_asyncio.apply(loop)
    except RuntimeError:
        pass

    return asyncio.run(
        _run(
            urls,
            concurrency=concurrency,
            timeout=timeout,
            follow_redirects=follow_redirects,
            head_then_get=head_then_get,
            retries=retries,
            user_agent=user_agent,
            safari_retry=safari_retry,
            progress_callback=progress_callback,
        )
    )
