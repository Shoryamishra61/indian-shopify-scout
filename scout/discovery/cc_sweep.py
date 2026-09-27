"""Common Crawl index sweep - the primary discovery source.

Why not the CDX query API?  index.commoncrawl.org supports `*.domain.tld`
patterns but silently fails TLD-level patterns like `*.in/cdn/shop/*`
(unfiltered pages come back). The underlying index files on
data.commoncrawl.org are the robust route:

* `cluster.idx` is a small (about 105 MB) plain-text secondary index. Each
  line is `<surt-key> <timestamp>\\t<cdx-file>\\t<offset>\\t<length>` and
  points at one independently-gzipped block of the primary index.
* SURT keys are sorted, so every `.in` host lives in the contiguous run of
  lines beginning `in,` (and every myshopify host under `com,myshopify,`).
* We download cluster.idx once, collect the block descriptors in those runs,
  fetch exactly those byte ranges, gunzip each block, and filter the URLs
  locally for Shopify-shaped paths.

Only index metadata is read at discovery time - no storefront is contacted.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

import config

log = logging.getLogger("cc")

SKIP_HOST_PREFIXES = ("cdn.", "static.", "assets.", "img.", "images.", "media.", "assets2.")
IGNORE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".css", ".js",
                   ".woff", ".woff2", ".ttf", ".pdf", ".mp4", ".ico", ".webm", ".avif")


@dataclass
class Block:
    cdx_file: str
    offset: int
    length: int


def _host_of(url: str) -> str | None:
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return None
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or host.startswith(SKIP_HOST_PREFIXES):
        return None
    return host


def latest_crawls(session: aiohttp.ClientSession, limit: int) -> list[str]:
    url = config.CC_COLLINFO
    with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        data = json.loads(resp.read())
    return [c["id"] for c in data[:limit]]


async def download_cluster_idx(session: aiohttp.ClientSession, crawl: str) -> Path:
    """Download cluster.idx once per crawl (about 105 MB)."""
    dest = config.CC_CACHE / f"cluster.idx.{crawl}"
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return dest
    config.CC_CACHE.mkdir(parents=True, exist_ok=True)
    log.info("downloading cluster.idx for %s ...", crawl)
    t0 = time.time()
    async with session.get(config.CC_CLUSTER_IDX.format(crawl=crawl),
                           timeout=aiohttp.ClientTimeout(total=None, sock_read=120)) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            async for chunk in resp.content.iter_chunked(1 << 20):
                fh.write(chunk)
    log.info("cluster.idx downloaded: %.1f MB in %.0fs",
             dest.stat().st_size / 1e6, time.time() - t0)
    return dest


def blocks_for_prefix(cluster_path: Path, prefix: str) -> list[Block]:
    """Collect byte-range block descriptors whose SURT key starts with prefix."""
    blocks: list[Block] = []
    seen: set[tuple[str, int]] = set()
    with cluster_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith(prefix):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            try:
                blk = Block(parts[1], int(parts[2]), int(parts[3]))
            except ValueError:
                continue
            key = (blk.cdx_file, blk.offset)
            if key not in seen:
                seen.add(key)
                blocks.append(blk)
    return blocks


class Throttled(Exception):
    """Raised internally when the index server starts rejecting requests."""


async def fetch_block(session: aiohttp.ClientSession, crawl: str, blk: Block,
                      sem: asyncio.Semaphore, retries: int = 2) -> str:
    """Byte-range GET of one gzipped index block, decompressed to text.

    Raises Throttled on HTTP 403 so the caller can back the whole sweep off
    instead of spinning through the remaining blocks.
    """
    url = config.CC_CDX_FILE.format(crawl=crawl, name=blk.cdx_file)
    headers = {"Range": f"bytes={blk.offset}-{blk.offset + blk.length - 1}"}
    delay = 3.0
    last_status = 0
    for attempt in range(retries + 1):
        async with sem:
            await asyncio.sleep(config.CC_BLOCK_STAGGER)
            try:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=90)) as resp:
                    last_status = resp.status
                    if resp.status in (200, 206):
                        raw = await resp.read()
                        try:
                            return gzip.decompress(raw).decode("utf-8", errors="replace")
                        except (OSError, gzip.BadGzipFile):
                            # multi-member gzip: decompress piecewise
                            out = io.BytesIO()
                            bio = io.BytesIO(raw)
                            while True:
                                try:
                                    out.write(gzip.GzipFile(fileobj=bio).read())
                                    break
                                except (OSError, EOFError, gzip.BadGzipFile):
                                    break
                            return out.getvalue().decode("utf-8", errors="replace")
                    if resp.status == 416:  # out of range -> nothing useful
                        return ""
                    if resp.status == 403:
                        raise Throttled()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass
        await asyncio.sleep(delay)
        delay *= 1.6
    log.warning("giving up on block %s@%d (status %s)", blk.cdx_file, blk.offset, last_status)
    return None


SHOPIFY_PATH = re.compile(config.CC_SHOPIFY_PATH_RE, re.I)
MYSHP_INDIA = re.compile(config.CC_MYSHP_INDIA_RE, re.I)


def extract_hosts_from_block(text: str) -> tuple[set[str], set[str]]:
    """Return (in-hosts-with-shopify-paths, myshopify-subdomains) for a block."""
    in_hosts: set[str] = set()
    myshp: set[str] = set()
    for line in text.splitlines():
        # CDXJ: "<surt> <timestamp> <json>" - separated by single SPACES.
        # A tab-split matches nothing and silently yields zero candidates.
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        surt = parts[0]
        try:
            rec = json.loads(parts[2])
        except json.JSONDecodeError:
            continue
        if str(rec.get("status")) != "200":
            continue
        url = rec.get("url", "")
        host = _host_of(url)
        if host is None:
            continue
        path = urlsplit(url).path.lower()
        if path.endswith(IGNORE_SUFFIXES):
            continue
        if surt.startswith("in,"):
            if SHOPIFY_PATH.search(path) or path == "/":
                in_hosts.add(host)
        elif surt.startswith("com,myshopify,"):
            myshp.add(host)
    return in_hosts, myshp


def _done_ledger(crawl: str) -> tuple[Path, set[str]]:
    """Persistent record of already-processed blocks so interrupted sweeps
    resume instead of re-downloading."""
    path = config.CC_CACHE / f"done_blocks.{crawl}.txt"
    seen: set[str] = set()
    if path.exists():
        seen = set(path.read_text(encoding="utf-8").split())
    return path, seen


def _mark_done(path: Path, keys: list[str]) -> None:
    if not keys:
        return
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(keys) + "\n")


async def sweep(store, use_search_sample: bool = True, myshp_only: bool = False) -> dict:
    """Run the .in + myshopify sweep across the newest crawls. Adds candidates.

    Production shape: blocks are processed in waves with cooldowns because
    the index server rate-limits bursts; a done-ledger makes every wave
    incremental; hosts are persisted continuously so nothing is lost.
    """
    import aiohttp as _aio
    summary = {}
    sem = asyncio.Semaphore(config.CC_BLOCK_CONCURRENCY)
    from scout.fetcher import default_resolver
    connector = _aio.TCPConnector(limit=8, resolver=default_resolver())
    async with _aio.ClientSession(
        connector=connector,
        headers={"User-Agent": config.USER_AGENT},
    ) as session:
        crawls = await asyncio.get_event_loop().run_in_executor(
            None, lambda: latest_crawls_sync(session, config.CC_CRAWLS_USED))
        for crawl in crawls:
            cluster = await download_cluster_idx(session, crawl)
            # myshopify run first (tiny) as an early win, then the .in run
            my_blocks = blocks_for_prefix(cluster, "com,myshopify,")
            in_blocks = blocks_for_prefix(cluster, "in,")
            ledger, done = _done_ledger(crawl)
            todo_my = [b for b in my_blocks if f"{b.cdx_file}:{b.offset}" not in done]
            todo_in = [b for b in in_blocks if f"{b.cdx_file}:{b.offset}" not in done]
            log.info("%s: %d .in blocks (%d done), %d myshopify blocks (%d done)",
                     crawl, len(in_blocks), len(in_blocks) - len(todo_in),
                     len(my_blocks), len(my_blocks) - len(todo_my))
            t0 = time.time()

            in_hosts: set[str] = set()
            myshp_hosts: set[str] = set()
            stats = {"fetched": 0, "failed": 0, "throttled_waves": 0}

            async def run_waves(label: str, blks: list[Block], sink_in: bool, sink_my: bool):
                nonlocal in_hosts, myshp_hosts
                """Process a block list in cooldown-spaced waves."""
                pending_in: set[str] = set()
                pending_my: set[str] = set()
                for wave_start in range(0, len(blks), config.CC_WAVE_SIZE):
                    wave = blks[wave_start: wave_start + config.CC_WAVE_SIZE]
                    wave_no = wave_start // config.CC_WAVE_SIZE + 1
                    log.info("  %s wave %d: %d blocks", label, wave_no, len(wave))

                    async def _with_block(blk: Block):
                        return blk, await fetch_block(session, crawl, blk, sem)

                    tasks = [asyncio.create_task(_with_block(b)) for b in wave]
                    fail_streak = 0
                    abort = False
                    for fut in asyncio.as_completed(tasks):
                        try:
                            blk, text = await fut
                        except Throttled:
                            # 403 wall: cancel the rest of the wave, cool down
                            fail_streak += 1
                            if fail_streak >= 3:
                                abort = True
                                break
                            continue
                        if text is None:
                            stats["failed"] += 1
                            continue
                        fail_streak = 0
                        _mark_done(ledger, [f"{blk.cdx_file}:{blk.offset}"])
                        hin, hmy = extract_hosts_from_block(text)
                        if sink_in and hin:
                            in_hosts |= hin
                            pending_in |= hin
                        if sink_my and hmy:
                            myshp_hosts |= hmy
                            pending_my |= hmy
                        stats["fetched"] += 1
                        if stats["fetched"] % 250 == 0:
                            log.info("  %s: fetched=%d failed=%d (in=%d, myshp=%d)",
                                     label, stats["fetched"], stats["failed"],
                                     len(in_hosts), len(myshp_hosts))
                    # flush per wave: an interrupted run keeps its hosts
                    if pending_in:
                        store.add_candidates(sorted(pending_in), f"cc_in_partial:{crawl}")
                        pending_in.clear()
                    if pending_my:
                        store.add_candidates(sorted(pending_my), f"cc_myshp_partial:{crawl}")
                        pending_my.clear()

                    if abort:
                        stats["throttled_waves"] += 1
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                        # failed blocks are simply not in the ledger; the next
                        # run picks them up
                        todo_retry.extend(b for b in wave)
                        log.warning("  throttled mid-wave; cooling down %ds",
                                    config.CC_WAVE_COOLDOWN)
                        await asyncio.sleep(config.CC_WAVE_COOLDOWN)
                    elif wave_start + config.CC_WAVE_SIZE < len(blks):
                        log.info("  wave done; cooling down %ds", config.CC_WAVE_COOLDOWN)
                        await asyncio.sleep(config.CC_WAVE_COOLDOWN)

            todo_retry: list[Block] = []

            if todo_my:
                await run_waves("myshopify", todo_my, False, True)
            if todo_in and not myshp_only:
                await run_waves("in", todo_in, True, False)
            # one retry pass over throttled leftovers
            if todo_retry:
                log.info("  retrying %d throttled blocks", len(todo_retry))
                await asyncio.sleep(config.CC_WAVE_COOLDOWN)
                await run_waves("retry", todo_retry, True, True)

            india_myshp = {h for h in myshp_hosts if MYSHP_INDIA.search(h.split(".")[0])}
            if use_search_sample and len(myshp_hosts) > config.CC_MYSHP_RANDOM_SAMPLE:
                rest = list(myshp_hosts - india_myshp)
                random.seed(42)
                india_myshp |= set(random.sample(rest, config.CC_MYSHP_RANDOM_SAMPLE))

            added_in = store.add_candidates(sorted(in_hosts), f"cc_in:{crawl}")
            added_my = store.add_candidates(sorted(india_myshp), f"cc_myshp:{crawl}")
            summary[crawl] = {
                "in_hosts_seen": len(in_hosts), "myshopify_hosts_seen": len(myshp_hosts),
                "myshopify_india_keyword": len(india_myshp),
                "candidates_added": added_in + added_my,
                "blocks_fetched": stats["fetched"], "blocks_failed": stats["failed"],
                "seconds": round(time.time() - t0),
            }
            log.info("%s: in=%d myshp=%d (india-keyword %d) added=%d (%.0fs)",
                     crawl, len(in_hosts), len(myshp_hosts), len(india_myshp),
                     added_in + added_my, time.time() - t0)
    return summary


# index.commoncrawl.org (the collinfo host) is a community-run service that
# intermittently drops connections; these recent known-good crawl ids are the
# fallback so discovery never hard-depends on it
FALLBACK_CRAWLS = [
    "CC-MAIN-2026-39", "CC-MAIN-2026-34", "CC-MAIN-2026-30", "CC-MAIN-2026-25",
    "CC-MAIN-2026-21", "CC-MAIN-2026-17",
]


def latest_crawls_sync(session: aiohttp.ClientSession, limit: int) -> list[str]:
    import urllib.request
    try:
        req = urllib.request.Request(config.CC_COLLINFO, headers={"User-Agent": config.USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return [c["id"] for c in data[:limit]]
    except Exception:
        log.warning("collinfo unreachable; using fallback crawl ids")
        return FALLBACK_CRAWLS[:limit]
