"""Candidate/record persistence: JSONL append-friendly stores with locking.

A "candidate" becomes a "record" once verified. All stages read and write the
same JSONL files, so the pipeline is resumable: re-running a stage skips work
already recorded.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

import config

_lock = threading.Lock()


def _append(path: Path, obj: dict) -> None:
    with _lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _load(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = obj.get("host") or obj.get("domain") or ""
            if key:
                out[key] = obj
    return out


class Store:
    def __init__(self):
        config.ensure_dirs()
        self.candidates = _load(config.CANDIDATES_FILE)
        self.records = _load(config.STORES_FILE)
        self.rejected = _load(config.REJECTED_FILE)
        # records later discarded (e.g. password page at enrichment) must not
        # resurrect from their earlier JSONL lines on reload
        for host in self.rejected:
            self.records.pop(host, None)

    # -- candidates ----------------------------------------------------

    def add_candidates(self, domains: list[str], source: str) -> int:
        """Register new candidate hosts. Returns count actually added."""
        added = 0
        for dom in domains:
            dom = dom.strip().lower()
            if not dom or "." not in dom or len(dom) < 4:
                continue
            if dom in self.candidates:
                # record provenance of repeat sightings too
                src = self.candidates[dom].setdefault("sources", [])
                if source not in src:
                    src.append(source)
                continue
            self.candidates[dom] = {"host": dom, "sources": [source]}
            added += 1
            _append(config.CANDIDATES_FILE, self.candidates[dom])
        return added

    def unsampled_candidates(self) -> list[str]:
        done = set(self.records) | set(self.rejected)
        return [d for d in self.candidates if d not in done]

    # -- records -------------------------------------------------------

    def get_record(self, host: str) -> Optional[dict]:
        return self.records.get(host)

    def save_record(self, record: dict) -> None:
        host = record["host"]
        self.records[host] = record
        _append(config.STORES_FILE, record)

    def discard_record(self, host: str, reason: str) -> None:
        """Move a verified record back to rejected (e.g. password page found
        at enrichment time)."""
        self.records.pop(host, None)
        self.reject(host, reason)

    def reject(self, host: str, reason: str, meta: dict | None = None) -> None:
        obj = {"host": host, "reason": reason}
        if meta:
            obj.update(meta)
        self.rejected[host] = obj
        _append(config.REJECTED_FILE, obj)

    # -- bulk ----------------------------------------------------------

    def verified(self) -> list[dict]:
        return [r for r in self.records.values() if r.get("is_shopify") and r.get("is_indian")]

    def dedup_by_shop(self) -> list[dict]:
        """One row per myshopify shop, preferring custom domains."""
        best: dict[str, dict] = {}
        for rec in self.verified():
            key = rec.get("myshopify_domain") or rec["host"]
            cur = best.get(key)
            if cur is None or _score(rec) > _score(cur):
                best[key] = rec
        return sorted(best.values(), key=lambda r: r["host"])


def _score(rec: dict) -> int:
    """Rank duplicates: custom domains over myshopify subdomains."""
    s = 0
    host = rec["host"]
    if not host.endswith(".myshopify.com"):
        s += 2
    if rec.get("fields_complete", 0) > 0:
        s += rec["fields_complete"]
    return s
