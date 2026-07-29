"""
verifier.py
Parallel tunnel verification.

A TCP ping only proves a port is open. Plenty of scraped configs point at
ordinary web servers or carry dead UUIDs - those answer a ping instantly,
score well, and never move a single byte. The only honest test is to build
the tunnel and push real traffic through it.

Doing that one server at a time is painfully slow (a dead candidate costs
several seconds of timeouts, and there can be dozens). So this starts a
throwaway xray for each candidate on its own local port and tests the whole
batch at once. What comes back is the set of servers that genuinely work,
ranked by real end-to-end latency instead of raw ping.
"""

import asyncio
import json
import platform
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests

from scraper import Server
from xray_manager import build_config

TEST_URLS = [
    "http://cp.cloudflare.com/generate_204",
    "http://detectportal.firefox.com/success.txt",
]

# Same guarantee as WRAITH.py's health check: nothing but these fixed
# connectivity checks ever goes through one of these throwaway,
# anonymous-server tunnels.
_ALLOWED_TEST_HOSTS = frozenset(urlparse(u).netloc for u in TEST_URLS)


def _assert_safe_url(url: str):
    if urlparse(url).netloc not in _ALLOWED_TEST_HOSTS:
        raise ValueError(f"refusing to send a request to unlisted host: {url}")


def _probe_once(port: int, timeout: float) -> Optional[float]:
    """Real round-trip in ms through this tunnel, or None if nothing flows."""
    proxies = {
        "http": f"http://127.0.0.1:{port}",
        "https": f"http://127.0.0.1:{port}",
    }
    session = requests.Session()
    session.trust_env = False  # never silently fall back to the system proxy
    try:
        for url in TEST_URLS:
            _assert_safe_url(url)
            start = time.monotonic()
            try:
                r = session.get(url, proxies=proxies, timeout=timeout)
                if r.status_code in (200, 204):
                    return (time.monotonic() - start) * 1000
            except Exception:
                continue
        return None
    finally:
        session.close()


class BatchVerifier:
    """
    Tests a batch of servers concurrently, each in its own xray process
    listening on its own pair of local ports. Every process is torn down
    before verify() returns, so nothing is left running.
    """

    def __init__(self, exe_path, work_dir, base_port: int = 11080):
        self.exe_path = Path(exe_path)
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.base_port = base_port

    def _spawn(self, server: Server, slot: int):
        socks_port = self.base_port + slot * 2
        http_port = socks_port + 1
        config = build_config(server, socks_port=socks_port,
                              http_port=http_port, loglevel="none")
        path = self.dir / f"probe_{slot}.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

        flags = 0
        if platform.system() == "Windows":
            flags = subprocess.CREATE_NO_WINDOW

        proc = subprocess.Popen(
            [str(self.exe_path), "-c", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        return proc, http_port

    async def verify(self, servers: List[Server], settle: float = 1.6,
                     timeout: float = 5.0) -> List[Tuple[Server, float]]:
        """
        Returns [(server, real_latency_ms), ...] for the ones that actually
        carried traffic, fastest first. Servers not in the result are dead.
        """
        running = []
        try:
            for slot, server in enumerate(servers):
                try:
                    proc, http_port = self._spawn(server, slot)
                    running.append((server, proc, http_port))
                except Exception:
                    continue

            if not running:
                return []

            # Give every instance a moment to bind its port and handshake.
            await asyncio.sleep(settle)

            results = await asyncio.gather(*[
                asyncio.to_thread(_probe_once, http_port, timeout)
                for _, _, http_port in running
            ], return_exceptions=True)

            working = []
            for (server, _, _), latency in zip(running, results):
                if isinstance(latency, float):
                    working.append((server, latency))
            working.sort(key=lambda pair: pair[1])
            return working

        finally:
            for _, proc, _ in running:
                try:
                    proc.terminate()
                except Exception:
                    pass
            for _, proc, _ in running:
                try:
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
