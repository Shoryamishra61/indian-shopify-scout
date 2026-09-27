"""CLI entrypoint.

Usage:
    python run.py discover [--no-search] [--no-listicles]
    python run.py verify  [--limit N]
    python run.py enrich
    python run.py export
    python run.py expand          # propose candidates from verified homepages
    python run.py all             # discover -> verify -> enrich -> export

Stages are idempotent: raw responses are cached on disk and stage outputs
are JSONL files that are only appended to.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from pathlib import Path

# some environments (e.g. with editable-install path hooks) don't put the
# script directory on sys.path - make the module imports self-sufficient
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp

import config
from scout.discovery import cc_sweep, expansion, search as search_mod, seeds as seeds_mod
from scout.export import export
from scout.fetcher import Fetcher, default_resolver
from scout.store import Store
from scout.verify import run as verify_run
from scout.enrich import run as enrich_run

log = logging.getLogger("run")


async def make_fetcher() -> tuple[aiohttp.ClientSession, Fetcher]:
    # async DNS with a hard timeout: dead domains would otherwise hang the
    # system resolver for 10-30s each and throttle the whole crawl
    connector = aiohttp.TCPConnector(limit=config.GLOBAL_CONCURRENCY + 8,
                                     ttl_dns_cache=600, resolver=default_resolver())
    session = aiohttp.ClientSession(
        connector=connector,
        headers={"User-Agent": config.USER_AGENT,
                 "Accept-Language": "en-US,en;q=0.9"},
    )
    return session, Fetcher(session)


async def cmd_discover(args) -> None:
    store = Store()
    before = len(store.candidates)
    if not args.no_cc:
        async with (await make_fetcher())[0] as session:
            summary = await cc_sweep.sweep(store, myshp_only=args.myshp_only)
            log.info("cc sweep: %s", summary)
    if not args.no_listicles:
        session, fetcher = await make_fetcher()
        async with session:
            await seeds_mod.discover_from_listicles(store, fetcher)
    if not args.no_seeds:
        added = Store().add_candidates(seeds_mod.load_seed_domains(), "seeds")
        log.info("seeds: %d new", added)
    if not args.no_search:
        session, fetcher = await make_fetcher()
        async with session:
            await search_mod.discover(store, fetcher)
    store2 = Store()
    log.info("discovery: %d -> %d candidates", before, len(store2.candidates))


async def cmd_verify(args) -> None:
    store = Store()
    pending = store.unsampled_candidates()
    if args.limit:
        pending = pending[: args.limit]
    session, fetcher = await make_fetcher()
    async with session:
        await verify_run(store, fetcher, pending)


async def cmd_enrich(args) -> None:
    store = Store()
    session, fetcher = await make_fetcher()
    async with session:
        await enrich_run(store, fetcher)


async def cmd_expand(args) -> None:
    store = Store()
    session, fetcher = await make_fetcher()
    async with session:
        await expansion.discover(store, fetcher)


async def cmd_export(args) -> None:
    store = Store()
    report = export(store)
    log.info("exported %d stores -> %s", report["total_stores"], config.CSV_FILE)


async def cmd_all(args) -> None:
    store = Store()
    if len(store.candidates) < 500 or not args.reuse:
        await cmd_discover(args)
    await cmd_verify(args)
    await cmd_enrich(args)
    await cmd_export(args)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["discover", "verify", "enrich", "export", "expand", "all"])
    parser.add_argument("--limit", type=int, default=0, help="cap candidates for verify")
    parser.add_argument("--no-search", action="store_true")
    parser.add_argument("--no-cc", action="store_true")
    parser.add_argument("--no-seeds", action="store_true")
    parser.add_argument("--no-listicles", action="store_true")
    parser.add_argument("--reuse", action="store_true", help="skip discovery if pool large enough")
    parser.add_argument("--myshp-only", action="store_true", help="CC sweep: only the myshopify run (skip .in)")
    args = parser.parse_args()

    config.ensure_dirs()
    # Windows: aiodns (c-ares) requires a SelectorEventLoop - under the
    # default proactor loop DNS resolution deadlocks intermittently. The
    # selector loop's 512-socket cap is not a constraint at this concurrency.
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    runner = {
        "discover": cmd_discover, "verify": cmd_verify, "enrich": cmd_enrich,
        "export": cmd_export, "expand": cmd_expand, "all": cmd_all,
    }[args.stage]
    asyncio.run(runner(args))


if __name__ == "__main__":
    main()
