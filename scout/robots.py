"""robots.txt handling: fetch once per host, cache, answer path queries.

Only the `User-agent: *` group is evaluated - we crawl under our own UA and
obey the blanket rules, which is the conservative interpretation.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import config


def _normalize(host: str) -> str:
    host = host.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


class RobotsCache:
    def __init__(self):
        self._rules: dict[str, list[str] | None] = {}
        self._status: dict[str, int] = {}

    async def _rules_for(self, host: str, fetcher) -> list[str] | None:
        if host in self._rules:
            return self._rules[host]
        url = f"https://{host}/robots.txt"
        reply = await fetcher.get(url, use_cache=True)
        self._status[host] = reply.status if reply is not None else 0
        disallows: list[str] | None = []
        if reply is None or not reply.ok:
            # unreachable robots.txt -> crawl anyway (standard behaviour)
            disallows = []
        else:
            in_star = False
            for line in reply.body.splitlines():
                line = line.split("#")[0].strip()
                if not line:
                    continue
                key, _, val = line.partition(":")
                key, val = key.strip().lower(), val.strip()
                if key == "user-agent":
                    in_star = val == "*"
                elif key == "disallow" and in_star:
                    if val:
                        disallows.append(val)
        self._rules[host] = disallows
        return disallows

    async def probe(self, host: str, fetcher) -> int:
        """Ensure robots.txt is loaded for host; return its fetch status.

        0 means the fetch failed at network level (DNS/connect/timeout) -
        a strong signal the whole host is dead.
        """
        await self._rules_for(host, fetcher)
        return self._status.get(host, 0)

    async def allowed(self, url: str, fetcher) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        host = _normalize(parts.netloc)
        rules = await self._rules_for(host, fetcher)
        if rules is None:
            rules = []
        path = parts.path or "/"
        for rule in rules:
            if rule == "/" and path == "/":
                return False
            # prefix match, the common robots.txt semantic
            if path.startswith(rule):
                return False
        return True


def normalize_host(host: str) -> str:
    return _normalize(host.strip().lower().split("/")[0])
