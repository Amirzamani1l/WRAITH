"""
prober.py
Concurrent (async) TCP ping test for all servers.
"""

import asyncio
import time
from typing import Dict, List, Optional

from scraper import Server


class ProbeResult:
    def __init__(self):
        self.ping_ms: Optional[float] = None  # None means dead
        self.consecutive_fails: int = 0
        self.last_checked: float = 0.0


class Prober:
    def __init__(self, servers: List[Server], timeout: float = 2.5):
        self.servers = {s.key: s for s in servers}
        self.results: Dict[tuple, ProbeResult] = {k: ProbeResult() for k in self.servers}
        self.timeout = timeout
        self._stop = False

    async def _probe_one(self, server: Server):
        key = server.key
        result = self.results.setdefault(key, ProbeResult())
        start = time.monotonic()
        try:
            fut = asyncio.open_connection(server.add, server.port)
            reader, writer = await asyncio.wait_for(fut, timeout=self.timeout)
            elapsed = (time.monotonic() - start) * 1000
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            result.ping_ms = round(elapsed, 1)
            result.consecutive_fails = 0
        except Exception:
            result.ping_ms = None
            result.consecutive_fails += 1
        result.last_checked = time.time()

    async def probe_all_once(self):
        tasks = [self._probe_one(s) for s in self.servers.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_forever(self, interval: float = 1.0):
        while not self._stop:
            await self.probe_all_once()
            await asyncio.sleep(interval)

    def stop(self):
        self._stop = True

    def ranked_servers(self, exclude_keys=None) -> List[Server]:
        """
        All currently-reachable servers, best first. Same scoring as
        best_server(), but returns the whole list so the caller can fall
        through to the next candidate when one turns out to be a dud.
        """
        exclude_keys = exclude_keys or set()
        candidates = []
        for key, server in self.servers.items():
            if key in exclude_keys:
                continue
            res = self.results.get(key)
            if res and res.ping_ms is not None:
                # Combined score: lower ping is better, higher safety is better
                # Each safety_score point is treated as a 30ms discount
                effective = res.ping_ms - (server.safety_score * 30)
                candidates.append((effective, server))
        candidates.sort(key=lambda x: x[0])
        return [s for _, s in candidates]

    def best_server(self, exclude_keys=None) -> Optional[Server]:
        ranked = self.ranked_servers(exclude_keys)
        return ranked[0] if ranked else None

    def get_ping(self, server: Server) -> Optional[float]:
        res = self.results.get(server.key)
        return res.ping_ms if res else None
