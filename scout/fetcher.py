"""Async HTTP client with per-host rate limiting, retries and a disk cache.

The cache makes every stage idempotent: verification and enrichment can be
re-run, interrupted, or extended without re-fetching a single page.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import aiohttp

import config

log = logging.getLogger("fetcher")


@dataclass
class Reply:
    url: str
    status: int
    body: str
    final_url: str
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def default_resolver():
    """c-ares with explicit public nameservers.

    aiodns (when installed) becomes aiohttp's default resolver but cannot
    read the Windows system resolver config - "Could not contact DNS
    servers" - so every connector must use this explicit resolver.
    """
    try:
        import aiodns  # noqa: F401
        return aiohttp.AsyncResolver(nameservers=["1.1.1.1", "8.8.8.8", "9.9.9.9"],
                                     timeout=2.5, tries=2)
    except Exception:
        return aiohttp.ThreadedResolver()


class _HostLimiter:
    """One in-flight request per host with a minimum interval between them."""

    def __init__(self):
        self._next_ok: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    async def wait(self, host: str) -> None:
        async with self._lock(host):
            now = time.monotonic()
            earliest = self._next_ok.get(host, 0.0)
            if now < earliest:
                await asyncio.sleep(earliest - now)
            self._next_ok[host] = time.monotonic() + config.PER_HOST_INTERVAL


class Fetcher:
    def __init__(self, session: aiohttp.ClientSession, use_cache: bool = True):
        self.session = session
        self.use_cache = use_cache
        self.limiter = _HostLimiter()
        self.global_sem = asyncio.Semaphore(config.GLOBAL_CONCURRENCY)
        self.fetch_count = 0
        self.hit_count = 0

    # ---------------- cache ----------------

    def _read_cache(self, url: str) -> Optional[Reply]:
        path = config.cache_path(url)
        if not path.exists():
            return None
        try:
            raw = json.loads(gzip.decompress(path.read_bytes()).decode())
        except Exception:
            return None
        if time.time() - raw.get("ts", 0) > config.CACHE_TTL:
            return None
        return Reply(url, raw["status"], raw["body"], raw.get("final_url", url), from_cache=True)

    def _write_cache(self, reply: Reply) -> None:
        path = config.cache_path(reply.url)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_bytes(gzip.compress(json.dumps({
                "status": reply.status,
                "body": reply.body,
                "final_url": reply.final_url,
                "ts": time.time(),
            }).encode()))
            tmp.replace(path)
        except OSError:
            pass

    # ---------------- fetching ----------------

    @staticmethod
    def _host_of(url: str) -> str:
        return urlsplit(url).netloc.lower()

    async def get(
        self,
        url: str,
        check_robots: "Optional[object]" = None,
        use_cache: Optional[bool] = None,
    ) -> Optional[Reply]:
        """GET with politeness, retries and caching.

        `check_robots` is a RobotsCache; when given and the path is
        disallowed, returns a synthetic 999 reply (robots-blocked) without a
        network call.
        """
        if use_cache is None:
            use_cache = self.use_cache
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                self.hit_count += 1
                return cached

        if check_robots is not None and not await check_robots.allowed(url, self):
            log.debug("robots-disallowed: %s", url)
            return Reply(url, 999, "", url)

        host = self._host_of(url)
        attempt = 0
        while True:
            await self.limiter.wait(host)
            dns_fail = False
            async with self.global_sem:
                try:
                    async with self.session.get(
                        url, timeout=aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT),
                        allow_redirects=True, max_redirects=6,
                    ) as resp:
                        status = resp.status
                        final_url = str(resp.url)
                        ctype = resp.headers.get("Content-Type", "")
                        if status == 429 or 500 <= status < 600:
                            body = ""
                        elif "html" in ctype or "json" in ctype or "text" in ctype or ctype == "":
                            body = await resp.text(errors="replace")
                        else:
                            body = ""  # binaries are not cached
                except aiohttp.ClientConnectorDNSError:
                    # deterministic failure: the domain does not resolve
                    status, body, final_url, dns_fail = 0, "", url, True
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    # resets / timeouts are often transient (local network
                    # throttling) - they get one retry below
                    status, body, final_url = 0, "", url
                    log.debug("network error %s: %s", url, exc)

            self.fetch_count += 1
            if status == 403 and attempt == 0:
                # one browser-UA retry for CDN-fronted storefronts
                reply = await self._get_with_ua(url, config.BROWSER_UA)
                if reply is not None and reply.ok:
                    if use_cache:
                        self._write_cache(reply)
                    return reply
                status, body, final_url = 403, "", url

            retryable = status == 429 or 500 <= status < 600 or (status == 0 and not dns_fail)
            if retryable and attempt < config.MAX_RETRIES:
                attempt += 1
                await asyncio.sleep(config.RETRY_BACKOFF * attempt)
                continue
            break

        reply = Reply(url, status, body, final_url)
        if use_cache and status not in (0,):  # don't cache network failures
            self._write_cache(reply)
        return reply

    async def _get_with_ua(self, url: str, ua: str) -> Optional[Reply]:
        await self.limiter.wait(self._host_of(url))
        async with self.global_sem:
            try:
                async with self.session.get(
                    url, timeout=aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT),
                    headers={"User-Agent": ua}, allow_redirects=True,
                ) as resp:
                    body = await resp.text(errors="replace") if resp.status == 200 else ""
                    return Reply(url, resp.status, body, str(resp.url))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                return None
