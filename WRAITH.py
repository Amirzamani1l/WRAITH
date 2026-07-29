"""
python WRAITH.py

Auto-collects public vmess servers from GitHub, continuously pings all of
them, and auto-connects to the best/safest one - no manual intervention,
until you close it.

by Amirzamani1l - https://github.com/Amirzamani1l

Note: this UI uses plain ASCII characters on purpose (no emoji, no unicode
box-drawing / braille glyphs) so it renders correctly in the default
Windows cmd.exe console, not just modern terminals.
"""

import asyncio
import atexit
import itertools
import os
import sys
import time
from collections import deque
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

import ui
from ui import (NEON_GREEN, NEON_CYAN, NEON_PURPLE, NEON_ORANGE,
                NEON_PINK, NEON_YELLOW, log_event,
                BLOOD, BLOOD_BRIGHT, BLOOD_DEEP, EMBER, BONE, ASH, ROT)
from scraper import GitHubScraper, Server, GitHubAuthError
from prober import Prober
from verifier import BatchVerifier
from xray_manager import XrayProcess, HTTP_PORT, SOCKS_PORT, BIN_DIR
import system_proxy
import ghost

# Force rich into VT mode (legacy_windows=False) on Windows so it renders via
# ANSI into the alternate screen buffer instead of the legacy cell-by-cell
# console API - the legacy path is what tears on a full-screen redraw. Set
# WRAITH_LEGACY=1 to fall back if a very old console garbles the output.
console = Console(legacy_windows=(os.environ.get("WRAITH_LEGACY") == "1"))

# --- how aggressively we detect a dead tunnel and heal it ---
# Dashboard frame rate. Kept deliberately low: the adaptive limiter below
# measures how long RICH takes to build a frame, but the tearing on cmd.exe
# comes from how long the CONSOLE takes to *paint* it - which it can't measure.
# A full-height frame at 24 fps is more than a stock cmd.exe can repaint
# cleanly, so it tears. ~12 fps gives the console time to finish each frame.
# Override with WRAITH_FPS if you're on a fast terminal (e.g. Windows Terminal).
FPS_MAX = int(os.environ.get("WRAITH_FPS", "12"))
FPS_MIN = 5
FRAME_LOAD = 0.55              # share of each frame we let drawing consume
HEALTH_INTERVAL = 2.0          # seconds between real connectivity checks
MAX_CONSECUTIVE_FAILS = 2      # failed checks before we give up on a server
VERIFY_TIMEOUT = 3.5           # per-request timeout for a health check
VERIFY_ATTEMPTS = 2            # tries before a candidate is written off
SETTLE_SECONDS = 1.2           # let xray bind + handshake after start
BENCH_SECONDS = 300            # how long a proven-dead server stays benched

# --- parallel scanning: test many candidates at once, not one at a time ---
VERIFY_BATCH = 8               # servers tested simultaneously
POOL_SCAN_LIMIT = 32           # how deep down the ranked list we're willing to go
POOL_TARGET = 4                # stop scanning once we have this many working
POOL_REFRESH_SECONDS = 120     # min gap between background top-ups

# --- how reluctant we are to leave a WORKING connection ---
SWITCH_MARGIN_MS = 120         # candidate must beat current by this much...
SWITCH_CONFIRMATIONS = 5       # ...for this many checks in a row
MIN_SWITCH_INTERVAL = 45       # and never upgrade more often than this

# Several targets: if one is unreachable the tunnel isn't necessarily dead.
HEALTH_CHECK_URLS = [
    "http://cp.cloudflare.com/generate_204",
    "http://detectportal.firefox.com/success.txt",
    "http://www.gstatic.com/generate_204",
]

# Defense in depth: nothing should ever go through an anonymous, untrusted
# tunnel except these fixed connectivity checks. Enforced at the call site
# (not just "these are the only URLs in the list") so a future change that
# accidentally sends something else through the proxy fails loudly instead
# of silently leaking a request to an unintended host.
_ALLOWED_HEALTH_HOSTS = frozenset(urlparse(u).netloc for u in HEALTH_CHECK_URLS)


def _assert_safe_url(url: str):
    host = urlparse(url).netloc
    if host not in _ALLOWED_HEALTH_HOSTS:
        raise ValueError(f"refusing to send a request to unlisted host: {host}")


def ascii_bar(current: int, total: int, width: int = 28) -> str:
    total = max(total, 1)
    filled = int(width * min(current / total, 1.0))
    return "[" + ui.FILL * filled + ui.EMPTY * (width - filled) + "]"


def print_gradient_banner():
    ui.print_banner(console)


async def boot_sequence():
    await ui.boot_sequence(console, asyncio.sleep)


def load_token() -> str:
    load_dotenv()
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        console.print("[bold red][ERROR][/bold red] GITHUB_TOKEN not found in .env file.")
        console.print("Create a .env file (copy .env.example) and put your token in it.")
        sys.exit(1)
    return token


def _blocking_health_check(timeout: float) -> bool:
    """
    Runs in a worker thread. A fresh Session every time on purpose: a pooled
    connection can outlive the xray process we're testing and report a stale
    'success'. trust_env=False keeps this check pointed at OUR proxy and stops
    it looping back through the system proxy we just switched on.
    """
    proxies = {
        "http": f"http://127.0.0.1:{HTTP_PORT}",
        "https": f"http://127.0.0.1:{HTTP_PORT}",
    }
    session = requests.Session()
    session.trust_env = False
    try:
        for url in HEALTH_CHECK_URLS:
            _assert_safe_url(url)
            try:
                r = session.get(url, proxies=proxies, timeout=timeout)
                if r.status_code in (200, 204):
                    return True
            except Exception:
                continue
        return False
    finally:
        session.close()


async def real_health_check(timeout: float = VERIFY_TIMEOUT) -> bool:
    """
    Does traffic ACTUALLY flow through the tunnel right now?

    Offloaded to a thread - requests is blocking, and calling it directly on
    the event loop froze the pinger and the UI for up to 4s at a time.
    """
    return await asyncio.to_thread(_blocking_health_check, timeout)


# --- the bench: servers that pinged fine but failed a real tunnel test ---
# A TCP port being open proves nothing. Plenty of scraped configs point at
# ordinary web servers or have expired UUIDs: they answer the ping instantly,
# score great, get picked first, and never carry a single byte. Without this,
# the picker just kept handing back the same top-ranked dud forever.
_bench: dict = {}


def _benched_keys() -> set:
    now = time.time()
    for key in [k for k, until in _bench.items() if until <= now]:
        _bench.pop(key, None)
    return set(_bench)


def _bench_server(server: Server, seconds: float = BENCH_SECONDS):
    if server:
        _bench[server.key] = time.time() + seconds


async def try_server(xray: XrayProcess, state: dict, server: Server) -> bool:
    """Point xray at a server and only call it connected once traffic proves it."""
    state["trying"] = server
    state["current"] = server
    state["connected_ok"] = False
    try:
        xray.start(server)
    except Exception as e:
        log_event("WARN", str(e) or "failed to start xray")
        return False
    await asyncio.sleep(SETTLE_SECONDS)
    for _ in range(VERIFY_ATTEMPTS):
        if not xray.is_alive():
            return False
        if await real_health_check():
            state["connected_ok"] = True
            state["trying"] = None
            state["last_switch"] = time.time()
            state["connected_since"] = time.time()
            state["latency_history"].clear()
            log_event("LINK", f"connected -> {server.add}:{server.port}")
            return True
        await asyncio.sleep(0.7)
    return False


async def build_pool(prober: Prober, verifier: BatchVerifier, state: dict) -> list:
    """
    Scan down the ranked list in parallel batches and keep the servers that
    actually carry traffic. Ranked by REAL tunnel latency, not TCP ping.
    """
    ranked = prober.ranked_servers(exclude_keys=_benched_keys())[:POOL_SCAN_LIMIT]
    if not ranked:
        _bench.clear()
        ranked = prober.ranked_servers()[:POOL_SCAN_LIMIT]

    working = list(state["pool"])
    known = {s.key for s, _ in working}

    for i in range(0, len(ranked), VERIFY_BATCH):
        chunk = [s for s in ranked[i:i + VERIFY_BATCH] if s.key not in known]
        if not chunk:
            continue
        state["scan"] = (min(i + VERIFY_BATCH, len(ranked)), len(ranked))
        log_event("SCAN", f"testing {len(chunk)} candidates in parallel")
        found = await verifier.verify(chunk)
        alive = {s.key for s, _ in found}
        for s in chunk:
            if s.key not in alive:
                _bench_server(s)  # pinged fine, moved nothing
        if found:
            log_event("SCAN", f"{len(found)}/{len(chunk)} carried real traffic")
        working.extend(found)
        known |= alive
        working.sort(key=lambda pair: pair[1])
        state["pool"] = working
        if len(working) >= POOL_TARGET:
            break

    state["scan"] = (0, 0)
    state["last_pool_build"] = time.time()
    state["pool"] = working
    return working


async def connect_from_pool(xray: XrayProcess, prober: Prober,
                            verifier: BatchVerifier, state: dict) -> bool:
    """
    Bring up a connection. Servers in the pool are already proven, so this is
    usually instant - we only go scanning again if the pool runs dry.
    """
    state["reconnecting"] = True
    state["connected_ok"] = False
    state["connected_since"] = None
    try:
        for _ in range(3):
            benched = _benched_keys()
            pool = [(s, ms) for s, ms in state["pool"] if s.key not in benched]
            for i, (server, ms) in enumerate(pool, 1):
                state["attempt"] = (i, len(pool))
                if await try_server(xray, state, server):
                    # Keep the winner at the front for next time.
                    rest = [(s, m) for s, m in state["pool"] if s.key != server.key]
                    state["pool"] = [(server, ms)] + rest
                    return True
                if xray.last_error:
                    # A local problem (e.g. port already taken by another
                    # instance), not this server's fault - benching every
                    # candidate for it would just be noise, and it'll fail
                    # again next loop anyway. Stop and let the caller retry.
                    return False
                _bench_server(server)
            # Pool exhausted - go find more.
            state["pool"] = [(s, m) for s, m in state["pool"]
                             if s.key not in _benched_keys()]
            await build_pool(prober, verifier, state)
            if not state["pool"]:
                _bench.clear()
        return False
    finally:
        state["reconnecting"] = False
        state["trying"] = None
        state["attempt"] = (0, 0)


async def supervisor(xray: XrayProcess, prober: Prober,
                     verifier: BatchVerifier, state: dict):
    """
    Background keep-alive loop. Owns every connect/switch decision so the
    dashboard loop stays free to animate at full speed.
    """
    fail_streak = 0
    better_streak = 0

    while True:
        try:
            if state["current"] is None or not xray.is_alive():
                await connect_from_pool(xray, prober, verifier, state)
                fail_streak = better_streak = 0
                await asyncio.sleep(1.0)
                continue

            ok = await real_health_check()
            state["connected_ok"] = ok
            state["health_history"].append(ok)
            state["latency_history"].append(
                prober.get_ping(state["current"]) if ok else None)

            if not ok:
                fail_streak += 1
                state["connected_since"] = None
                log_event("WARN", f"health check failed ({fail_streak}/{MAX_CONSECUTIVE_FAILS})")
                if fail_streak >= MAX_CONSECUTIVE_FAILS:
                    dead = state["current"]
                    log_event("DEAD", f"{dead.add}:{dead.port} stopped carrying traffic")
                    _bench_server(dead)
                    state["pool"] = [(s, m) for s, m in state["pool"]
                                     if s.key != dead.key]
                    await connect_from_pool(xray, prober, verifier, state)
                    fail_streak = better_streak = 0
                await asyncio.sleep(HEALTH_INTERVAL)
                continue

            fail_streak = 0
            if state["connected_since"] is None:
                state["connected_since"] = time.time()

            # Healthy. Top the pool up in the background so a future failure
            # can be healed instantly instead of triggering a fresh scan.
            if (len(state["pool"]) < POOL_TARGET
                    and time.time() - state["last_pool_build"] > POOL_REFRESH_SECONDS):
                await build_pool(prober, verifier, state)

            # Only leave for something clearly AND consistently better - a
            # one-off ping dip isn't worth dropping a working tunnel.
            cur_ping = prober.get_ping(state["current"])
            ranked = prober.ranked_servers(
                exclude_keys=_benched_keys() | {state["current"].key}
            )
            cand = ranked[0] if ranked else None
            cand_ping = prober.get_ping(cand) if cand else None
            clearly_better = (
                cand is not None
                and cur_ping is not None
                and cand_ping is not None
                and (cur_ping - cand_ping) > SWITCH_MARGIN_MS
            )
            better_streak = better_streak + 1 if clearly_better else 0

            if (better_streak >= SWITCH_CONFIRMATIONS
                    and time.time() - state["last_switch"] > MIN_SWITCH_INTERVAL):
                better_streak = 0
                previous, since = state["current"], state["connected_since"]
                log_event("SWAP", f"upgrading to {cand.add}:{cand.port} "
                                  f"({cur_ping:.0f}ms -> {cand_ping:.0f}ms)")
                if not await try_server(xray, state, cand):
                    # The "upgrade" didn't actually carry traffic. Bench it and
                    # fall straight back to the server we know was working.
                    log_event("DEAD", f"{cand.add}:{cand.port} failed - rolling back")
                    _bench_server(cand)
                    if await try_server(xray, state, previous):
                        state["connected_since"] = since

            await asyncio.sleep(HEALTH_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a stray error kill the keep-alive loop.
            await asyncio.sleep(HEALTH_INTERVAL)


def collect_servers_with_progress(scraper: GitHubScraper) -> list:
    """
    A centered "summoning" ritual panel for the scrape phase. Drawn into a
    single fixed-height Live with auto_refresh OFF so this loop is the only
    writer (no flicker), and the block never reflows (fixed width + truncated
    query), so redraws are invisible.
    """
    state = {"stage": "starting", "query": "", "current": 0, "total": 1,
             "found": 0, "f": 0}
    IW = 52   # inner width -> never reflows
    TITLE = "S U M M O N I N G   T H E   V O I D"

    def _specks(seed):
        row = Text()
        for x in range(IW):
            v = (x * 7 + seed * 5) % 29
            if v == 0:
                row.append("*", style=EMBER)
            elif v == 6:
                row.append(".", style=BLOOD)
            elif v == 14:
                row.append(":", style=BLOOD_DEEP)
            elif v == 22:
                row.append("'", style=ASH)
            else:
                row.append(" ")
        return row

    def _title(f):
        # a glow that scans left-to-right across the letters
        t = Text()
        head = f % (len(TITLE) + 16)
        for i, ch in enumerate(TITLE):
            if ch == " ":
                t.append(" ")
                continue
            d = head - i
            if 0 <= d < 2:
                t.append(ch, style=f"bold {BLOOD_BRIGHT}")
            elif 0 <= d < 6:
                t.append(ch, style=f"bold {BLOOD}")
            else:
                t.append(ch, style=BLOOD_DEEP)
        return t

    def render():
        f = state["f"]

        spin = "|/-\\"[f % 4]
        q = state["query"]
        maxq = IW - 18
        if len(q) > maxq:
            q = q[:maxq - 3] + "..."
        cursor = "_" if (f // 3) % 2 == 0 else " "
        line_s = Text()
        line_s.append(f" {spin} ", style=EMBER)
        line_s.append(f"{state['stage'].strip():<9} ", style=f"bold {BLOOD}")
        line_s.append(q, style=BONE)
        line_s.append(cursor, style=f"bold {BLOOD_BRIGHT}")

        # blood-filling bar with a bright, glowing leading edge
        w = 32
        filled = int(w * min(state["current"] / max(state["total"], 1), 1.0))
        line_b = Text()
        line_b.append("[", style=BLOOD_DEEP)
        if filled > 0:
            line_b.append(ui.FILL * (filled - 1), style=BLOOD)
            line_b.append(ui.FILL, style=f"bold {BLOOD_BRIGHT}")
        line_b.append(ui.EMPTY * (w - filled), style="grey23")
        line_b.append("]", style=BLOOD_DEEP)
        line_b.append(f"  {state['current']}/{state['total']}", style=BONE)

        line_c = Text()
        line_c.append("relays found:  ", style=ASH)
        line_c.append(str(state["found"]), style=f"bold {ROT}")

        inner = Group(
            _specks(f),
            Align.center(_title(f)),
            Text(""),
            Align.center(line_s),
            Align.center(line_b),
            Align.center(line_c),
            _specks(f + 7),
        )
        tflick = BLOOD_BRIGHT if (f // 2) % 9 else BLOOD_DEEP
        panel = Panel(
            inner, box=ui.BOX_PANEL, border_style=f"bold {BLOOD}",
            title=Text(" s c a n n i n g ", style=f"bold {tflick}"),
            padding=(0, 2), width=IW + 6,
        )
        return Align.center(panel)

    with Live(console=console, auto_refresh=False, screen=False,
              vertical_overflow="crop", transient=False) as live:
        def cb(stage, query, current, total, found_count):
            state["stage"] = "SEARCHING" if stage == "searching" else "FETCHING"
            state["query"] = f'"{query}"'
            state["current"] = current
            state["total"] = max(total, 1)
            state["found"] = found_count
            state["f"] += 1
            live.update(render(), refresh=True)

        servers = scraper.collect(
            max_pages_per_query=1,
            max_items_per_query=40,
            max_total_servers=60,
            progress_callback=cb,
        )
        live.update(render(), refresh=True)
    return servers


async def type_out(segments, delay=0.026):
    """Reveal a centered, styled line one character at a time (typewriter)."""
    chars = [(ch, style) for text, style in segments for ch in text]
    try:
        with Live(console=console, auto_refresh=False, transient=False) as live:
            for n in range(1, len(chars) + 1):
                t = Text()
                for ch, style in chars[:n]:
                    t.append(ch, style=style)
                if n < len(chars):
                    t.append("_", style=f"bold {BLOOD_BRIGHT}")   # typing cursor
                live.update(Align.center(t), refresh=True)
                await asyncio.sleep(delay)
    except Exception:
        t = Text()
        for ch, style in chars:
            t.append(ch, style=style)
        console.print(Align.center(t))


async def main():
    ghost.lock_terminal()          # pin the console to one fixed, non-resizable size
    console.clear()
    # Cold-open: a wraith drifts in, waves, and dissolves into the banner.
    # Purely cosmetic - set WRAITH_INTRO=0 in the environment to skip it.
    if os.environ.get("WRAITH_INTRO", "1") != "0":
        await ghost.ghost_intro(console, asyncio.sleep)
    print_gradient_banner()
    await boot_sequence()

    token = load_token()
    scraper = GitHubScraper(token)
    try:
        servers = collect_servers_with_progress(scraper)
    except GitHubAuthError as e:
        console.print(f"[bold red][ERROR][/bold red] {e}")
        console.print("Check your GITHUB_TOKEN in .env - "
                      "generate a fresh one at github.com/settings/tokens")
        sys.exit(1)

    if not servers:
        console.print("[bold red][ERROR][/bold red] No servers found. Check your internet connection or token.")
        sys.exit(1)

    console.print()
    await type_out([
        ("[OK] ", f"bold {NEON_GREEN}"),
        (f"{len(servers)} servers acquired. Booting tunnel...", BONE),
    ])
    console.print()

    prober = Prober(servers)
    await prober.probe_all_once()

    xray = XrayProcess()
    verifier = BatchVerifier(xray.exe_path, BIN_DIR / "probes")

    # Single source of truth, shared between the keep-alive supervisor and
    # the dashboard. The supervisor writes it, the render loop only reads it.
    state = {
        "current": None,
        "trying": None,
        "connected_ok": False,
        "reconnecting": False,
        "connected_since": None,
        "last_switch": 0.0,
        "attempt": (0, 0),
        "pool": [],          # [(server, real_latency_ms)] - proven to work
        "scan": (0, 0),
        "last_pool_build": 0.0,
        "latency_history": deque(maxlen=60),   # feeds the sparkline
        "health_history": deque(maxlen=30),    # feeds the quality meter
    }

    # Flip the Windows system proxy ON so Chrome / Edge / other apps route
    # through the tunnel automatically. atexit + the finally block below make
    # sure your original connection is restored when the app closes.
    proxy_on = system_proxy.enable(f"127.0.0.1:{HTTP_PORT}")
    atexit.register(system_proxy.disable)
    log_event("SYS", f"{len(servers)} servers scraped, starting verification")
    if proxy_on:
        log_event("SYS", "windows system proxy enabled")

    prober_task = asyncio.create_task(prober.run_forever(interval=1.0))
    supervisor_task = asyncio.create_task(supervisor(xray, prober, verifier, state))

    try:
        # Full-screen dashboard runs in the ALTERNATE screen buffer (screen=True).
        # On cmd.exe, redrawing a full-height frame in the normal buffer paints
        # line-by-line and you see it sweep = tearing. The alt buffer takes the
        # whole frame per flush and swaps it in, so redraws don't tear. And with
        # auto_refresh=False this loop is the only writer (no background thread
        # racing it). vertical_overflow="crop" stops an over-tall frame scrolling.
        with Live(console=console, auto_refresh=False, screen=True,
                  vertical_overflow="crop", transient=False) as live:
            fps = FPS_MAX
            cost_ema = 0.0
            while True:
                started = time.monotonic()
                live.update(ui.render(
                    state, prober, servers, xray.is_alive(), proxy_on,
                    SOCKS_PORT, HTTP_PORT, _benched_keys(),
                    max_height=console.size.height,
                    max_width=console.size.width,
                ), refresh=True)
                cost = time.monotonic() - started

                # Adaptive pacing. A slow console (cmd.exe is not fast) gets a
                # lower frame rate instead of a backlog of half-drawn frames,
                # so it degrades to "smooth but slower" rather than to tearing.
                cost_ema = cost if cost_ema == 0.0 else cost_ema * 0.9 + cost * 0.1
                affordable = FRAME_LOAD / max(cost_ema, 1e-4)
                fps = max(FPS_MIN, min(FPS_MAX, affordable))
                await asyncio.sleep(max(0.0, (1.0 / fps) - cost))

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        console.print(f"\n[bold {NEON_ORANGE}]shutting down...[/bold {NEON_ORANGE}]")
        system_proxy.disable()  # put the user's original proxy settings back
        prober.stop()
        for task in (prober_task, supervisor_task):
            task.cancel()
        xray.stop()
        xray.delete_log()
        console.print(f"[bold {NEON_GREEN}]closed. bye[/bold {NEON_GREEN}]")
        console.print("[dim]made by Amirzamani1l - github.com/Amirzamani1l[/dim]")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
