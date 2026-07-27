"""
scraper.py
Searches public GitHub code for vmess:// links and parses them into
a usable Server structure.
"""

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import List, Optional

import requests

GITHUB_API = "https://api.github.com"
VMESS_RE = re.compile(r"vmess://[A-Za-z0-9+/=_-]{20,}")

# Search terms used for the public GitHub code search
SEARCH_QUERIES = [
    "vmess:// in:file",
    "v2ray config in:file",
    "vmess subscription in:file",
]


@dataclass
class Server:
    add: str
    port: int
    id: str
    aid: str = "0"
    net: str = "tcp"
    tls: str = ""
    host: str = ""
    path: str = ""
    sni: str = ""
    scy: str = "auto"   # vmess encryption
    type: str = ""      # header obfuscation type (tcp/kcp/quic)
    alpn: str = ""
    fp: str = ""        # uTLS fingerprint
    ps: str = ""  # label/name
    source_repo: str = ""
    raw: str = ""  # original vmess link

    @property
    def key(self):
        return (self.add, self.port, self.id)

    @property
    def safety_score(self) -> int:
        """
        Simple heuristic: more encryption/obfuscation layers = higher score.
        This is NOT a guarantee of safety, just a reasonable priority ranking.
        """
        score = 0
        if self.tls in ("tls", "reality"):
            score += 2
        if self.net in ("ws", "grpc", "h2"):
            score += 1
        if self.port in (443, 8443):
            score += 1
        return score


def _decode_vmess(link: str, source_repo: str = "") -> Optional[Server]:
    try:
        b64 = link[len("vmess://"):]
        b64 += "=" * (-len(b64) % 4)  # fix base64 padding
        data = json.loads(base64.b64decode(b64).decode("utf-8", errors="ignore"))
        return Server(
            add=str(data.get("add", "")).strip(),
            port=int(str(data.get("port", 0)).strip() or 0),
            id=str(data.get("id", "")).strip(),
            aid=str(data.get("aid", "0")),
            net=str(data.get("net", "tcp")).strip().lower(),
            tls=str(data.get("tls", "")).strip().lower(),
            host=str(data.get("host", "")).strip(),
            path=str(data.get("path", "")).strip(),
            sni=str(data.get("sni", "")).strip(),
            scy=str(data.get("scy", "auto")).strip().lower() or "auto",
            type=str(data.get("type", "")).strip().lower(),
            alpn=str(data.get("alpn", "")).strip(),
            fp=str(data.get("fp", "")).strip().lower(),
            ps=str(data.get("ps", "")).strip(),
            source_repo=source_repo,
            raw=link,
        )
    except Exception:
        return None


class GitHubAuthError(Exception):
    """Raised when the token itself is the problem, not rate limiting."""


class GitHubScraper:
    # GitHub's Search API has its own, stricter limit than the general API
    # (30 req/min authenticated) - separate from the 5,000/hr core limit.
    # Pacing our own requests below that keeps us from ever tripping GitHub's
    # abuse-detection mechanism, which can temporarily block the token.
    _MIN_SEARCH_INTERVAL = 2.2  # seconds between search requests (~27/min)
    _last_search_at = 0.0

    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        })

    def _pace_search(self):
        elapsed = time.time() - GitHubScraper._last_search_at
        wait = self._MIN_SEARCH_INTERVAL - elapsed
        if wait > 0:
            time.sleep(wait)
        GitHubScraper._last_search_at = time.time()

    def _search_code(self, query: str, max_pages: int = 3) -> List[dict]:
        items = []
        for page in range(1, max_pages + 1):
            try:
                self._pace_search()
                r = self.session.get(
                    f"{GITHUB_API}/search/code",
                    params={"q": query, "per_page": 100, "page": page},
                    timeout=15,
                )
                if r.status_code == 401:
                    raise GitHubAuthError(
                        "GitHub rejected the token (401 Unauthorized) - "
                        "it's invalid, expired, or was revoked."
                    )
                if r.status_code == 403:
                    remaining = r.headers.get("X-RateLimit-Remaining")
                    if remaining == "0":
                        reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
                        wait = max(reset - time.time(), 5)
                        print(f"[scraper] rate limited, waiting {int(wait)}s...")
                        time.sleep(wait)
                        continue
                    # 403 without a rate-limit signal usually means the
                    # token's scope/permissions are the problem, not volume.
                    raise GitHubAuthError(
                        "GitHub returned 403 (not a rate limit) - check the "
                        "token's permissions."
                    )
                if r.status_code != 200:
                    break
                data = r.json()
                batch = data.get("items", [])
                items.extend(batch)
                if len(batch) < 100:
                    break
            except GitHubAuthError:
                raise
            except requests.RequestException:
                break
        return items

    def _fetch_raw(self, item: dict) -> str:
        """Fetches the actual file contents."""
        try:
            r = self.session.get(item["url"], timeout=15)
            if r.status_code != 200:
                return ""
            meta = r.json()
            download_url = meta.get("download_url")
            if not download_url:
                content_b64 = meta.get("content", "")
                if content_b64:
                    return base64.b64decode(content_b64).decode("utf-8", errors="ignore")
                return ""
            raw = self.session.get(download_url, timeout=15)
            if raw.status_code == 200:
                return raw.text
        except Exception:
            pass
        return ""

    def collect(
        self,
        max_pages_per_query: int = 1,
        max_items_per_query: int = 40,
        max_total_servers: int = 60,
        progress_callback=None,
    ) -> List[Server]:
        """
        progress_callback(stage, query, current, total, found_count) is called
        repeatedly during collection if provided, for live progress display.

        Stops early once max_total_servers is reached, or once
        max_items_per_query files have been checked per query - keeps things
        fast instead of grinding through thousands of files.
        """
        found: dict = {}
        for q in SEARCH_QUERIES:
            if len(found) >= max_total_servers:
                break
            if progress_callback:
                progress_callback("searching", q, 0, 0, len(found))
            items = self._search_code(q, max_pages=max_pages_per_query)[:max_items_per_query]
            total = len(items)
            if progress_callback:
                progress_callback("fetching", q, 0, total, len(found))
            for i, item in enumerate(items, 1):
                if len(found) >= max_total_servers:
                    break
                repo = item.get("repository", {}).get("full_name", "")
                content = self._fetch_raw(item)
                if content:
                    for link in VMESS_RE.findall(content):
                        server = _decode_vmess(link, source_repo=repo)
                        if server and server.add and server.port and server.id:
                            found[server.key] = server
                if progress_callback:
                    progress_callback("fetching", q, i, total, len(found))
        return list(found.values())
