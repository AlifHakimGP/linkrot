"""
checker.py — the core: check many URLs concurrently and classify each one.

THE FIVE OUTCOMES
-------------------
  OK        responded 2xx directly
  REDIRECT  ended at a 2xx page, but only after being redirected
            (the link works today, but the address has moved — worth
            updating before the redirect itself disappears)
  BROKEN    the server answered and said "no": 404, 410, 5xx, ...
  BLOCKED   401/403/429/etc. The server refused *us* (often because we
            look like a bot). The page may be perfectly alive for a human,
            so we refuse to call it dead — honesty beats a scary red row.
  ERROR     never got an HTTP answer at all: DNS failure, timeout, TLS
            error, redirect loop. The classic "the whole site is gone".

Distinguishing BLOCKED from BROKEN is the single most important design
decision in a link checker. Naive tools lump every non-200 together and
bury you in false alarms from sites that merely dislike scripts.
"""

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx


class Status(str, Enum):
    OK = "ok"
    REDIRECT = "redirect"
    BROKEN = "broken"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass
class Result:
    url: str
    status: Status
    code: Optional[int] = None
    final_url: Optional[str] = None
    detail: str = ""
    permanent_redirect: bool = False
    elapsed: float = 0.0


# Codes that mean "I refuse to talk to you", not "this page doesn't exist".
# 999 is LinkedIn's famous non-standard "go away, bot" response.
_BLOCKED_CODES = {401, 403, 407, 429, 451, 999}

_HEADERS = {
    # Many sites serve errors or nothing to obvious scripts, which would
    # make exactly the interesting sites uncheckable.
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 linkrot-checker"
    ),
    "Accept": "*/*",
}


def classify(code: int, history_codes: list[int]) -> tuple[Status, str, bool]:
    """Turn (final status code, redirect hops) into (Status, detail, permanent?)."""
    if 200 <= code < 300:
        if history_codes:
            permanent = any(c in (301, 308) for c in history_codes)
            kind = "permanently moved" if permanent else "temporary redirect"
            return Status.REDIRECT, kind, permanent
        return Status.OK, "", False
    if code in _BLOCKED_CODES:
        return Status.BLOCKED, "server refused us; may still work in a browser", False
    if code in (404, 410):
        return Status.BROKEN, "not found" if code == 404 else "gone", False
    if code >= 500:
        return Status.BROKEN, "server error, might be temporary", False
    if code >= 400:
        return Status.BROKEN, "client error", False
    return Status.OK, "", False  # odd 1xx/3xx leftovers: not evidence of rot


def _describe_error(exc: Exception) -> str:
    """Turn a scary httpx exception into one plain sentence."""
    if isinstance(exc, httpx.TooManyRedirects):
        return "redirect loop"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.InvalidURL) or isinstance(exc, httpx.UnsupportedProtocol):
        return "invalid URL"
    msg = str(exc).lower()
    if any(s in msg for s in ("name or service not known", "nodename nor servname",
                              "getaddrinfo", "temporary failure in name resolution",
                              "no address associated")):
        return "domain not found, DNS lookup failed"
    if "ssl" in msg or "certificate" in msg:
        return "TLS/certificate error"
    if isinstance(exc, httpx.ConnectError):
        return "connection refused/failed"
    return str(exc) or exc.__class__.__name__


async def _probe(client: httpx.AsyncClient, url: str) -> tuple[int, str, list[int]]:
    """
    HEAD first (asks for headers only, so no page download), falling back
    to GET if HEAD says 4xx/5xx.

    Why the fallback: a surprising number of servers mishandle HEAD —
    answering 405, 403, or even 404 — while the same URL works fine with
    GET. Trusting HEAD alone would report healthy pages as dead.
    """
    resp = await client.head(url)
    if resp.status_code < 400:
        return resp.status_code, str(resp.url), [r.status_code for r in resp.history]

    # stream() so we read the status line and headers without downloading
    # a potentially huge body.
    async with client.stream("GET", url) as resp:
        return resp.status_code, str(resp.url), [r.status_code for r in resp.history]


async def check_url(client: httpx.AsyncClient, url: str, retries: int = 1) -> Result:
    start = time.perf_counter()
    last_exc: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            code, final_url, history = await _probe(client, url)
            status, detail, permanent = classify(code, history)
            return Result(
                url=url,
                status=status,
                code=code,
                final_url=final_url if final_url != url else None,
                detail=detail,
                permanent_redirect=permanent,
                elapsed=time.perf_counter() - start,
            )
        except httpx.TimeoutException as exc:
            last_exc = exc  # worth one retry: timeouts are often transient
            await asyncio.sleep(0.5)
        except Exception as exc:  # noqa: BLE001 - network errors are many-shaped
            last_exc = exc
            break  # DNS failure / refused / TLS: retrying won't change it

    return Result(
        url=url,
        status=Status.ERROR,
        detail=_describe_error(last_exc) if last_exc else "unknown error",
        elapsed=time.perf_counter() - start,
    )


async def check_all(
    urls: list[str],
    *,
    concurrency: int = 20,
    per_host: int = 3,
    timeout: float = 10.0,
    on_result: Optional[Callable[[Result], None]] = None,
) -> list[Result]:
    """
    Check every URL concurrently; return results in the same order as `urls`.

    TWO LIMITS, TWO REASONS
    - `concurrency`: total requests in flight. Checking 2,000 links one at
      a time takes forever; all 2,000 at once exhausts file descriptors.
    - `per_host`: requests in flight to any single website. A README with
      80 links to one domain would otherwise hammer that server with 80
      simultaneous requests — rude, and a fast way to get rate-limited
      (which would then show up as false BLOCKED results).

    `on_result` is called as each check finishes, so a UI can show live
    progress instead of staring at a blank screen until the end.
    """
    global_sem = asyncio.Semaphore(concurrency)
    host_sems: dict[str, asyncio.Semaphore] = {}
    results: list[Optional[Result]] = [None] * len(urls)

    limits = httpx.Limits(max_connections=concurrency)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers=_HEADERS,
        limits=limits,
        max_redirects=10,
    ) as client:

        async def worker(index: int, url: str) -> None:
            host = urlparse(url).hostname or ""
            host_sem = host_sems.setdefault(host, asyncio.Semaphore(per_host))
            async with global_sem, host_sem:
                result = await check_url(client, url)
            results[index] = result
            if on_result:
                on_result(result)

        await asyncio.gather(*(worker(i, u) for i, u in enumerate(urls)))

    return [r for r in results if r is not None]
