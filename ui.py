"""
ui.py
The dashboard: extruded block banner, live panels, sparklines, event feed.

Glyph set is chosen at import time. Everything used here is CP437 - the
character set the default Windows console font ships with - so it renders
in plain cmd.exe, not just modern terminals. Braille and geometric shapes
are deliberately avoided: those are the glyphs that come out as empty
boxes. If the console still can't encode them, we drop to pure ASCII.
"""

import itertools
import random
import time
from collections import deque

from rich import box
from rich.align import Align
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---- horror / blood palette -------------------------------------------------
BLOOD        = "#d11414"     # bright blood
BLOOD_BRIGHT = "#ff2323"     # fresh spatter
BLOOD_DEEP   = "#7a0b0b"     # dried, dark
EMBER        = "#ff5a1f"     # hot ember
AMBER_SICK   = "#c79200"     # sick amber
ROT          = "#5f9b2e"     # sickly green - things that are 'alive'
NECRO        = "#7b4a99"     # necrotic violet
BONE         = "#d6cdbf"     # bone-white text
ASH          = "#8a7d76"     # grey ash

# The rest of the file speaks in these NEON_* names; remap them to the blood
# palette so everything reskins from one place. Semantics are preserved
# (green = alive/ok, red = down, amber = verifying, ...).
NEON_GREEN = ROT
NEON_CYAN = BLOOD            # info + logo base + the "live" edge
NEON_PURPLE = NECRO
NEON_ORANGE = EMBER
NEON_PINK = BLOOD_BRIGHT
NEON_YELLOW = AMBER_SICK
DIM_EDGE = "#3a0808"         # logo bevel: near-black dried blood
DEAD = "grey30"

# Blood on the letters: dark at the roots, hot at the tips.
GRADIENT = [BLOOD_DEEP, "#9c1010", BLOOD, "#e81c1c", BLOOD_BRIGHT, EMBER]
_gradient_cycle = itertools.cycle(GRADIENT)
_spinner = itertools.cycle(["|", "/", "-", "\\"])


def _console_can_encode(sample: str) -> bool:
    import sys
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        sample.encode(enc)
        return True
    except Exception:
        return False


BLOCKS = _console_can_encode("█▓▒░╔╗╚╝║═▁▂▃▄▅▆▇")

BOX_MAIN = box.DOUBLE if BLOCKS else box.ASCII2
BOX_PANEL = box.SQUARE if BLOCKS else box.ASCII2

PROJECT_NAME = "W R A I T H"
HEADER_H = 8            # full banner panel, including borders
HEADER_COMPACT_H = 4    # collapsed banner for narrow terminals

# ---------------------------------------------------------------- banner ---
# "ANSI Shadow" style: solid faces in █, bevel in double-box characters.
# Colouring the bevel darker than the face is what gives it depth.
LOGO_BLOCK = [
    "██╗    ██╗██████╗  █████╗ ██╗████████╗██╗  ██╗",
    "██║    ██║██╔══██╗██╔══██╗██║╚══██╔══╝██║  ██║",
    "██║ █╗ ██║██████╔╝███████║██║   ██║   ███████║",
    "██║███╗██║██╔══██╗██╔══██║██║   ██║   ██╔══██║",
    "╚███╔███╔╝██║  ██║██║  ██║██║   ██║   ██║  ██║",
    " ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝   ╚═╝   ╚═╝  ╚═╝",
]

# Properly aligned this time - the old one had mismatched row widths, which
# is why the banner came out looking scrambled.
LOGO_ASCII = [
    "__        ______      _    ___ _____ _   _ ",
    "\\ \\      / /  _ \\    / \\  |_ _|_   _| | | |",
    " \\ \\ /\\ / /| |_) |  / _ \\  | |  | | | |_| |",
    "  \\ V  V / |  _ <  / ___ \\ | |  | | |  _  |",
    "   \\_/\\_/  |_| \\_\\/_/   \\_\\___| |_| |_| |_|",
]

BEVEL = set("╗╝╔╚║═")


# Frame counter drives every animation. Bumped once per rendered frame so
# animation speed follows the real frame rate instead of wall-clock guesses.
_frame = 0


def tick():
    global _frame
    _frame += 1


def logo_text() -> Text:
    """
    Faces bright, bevel dark - that contrast is what reads as extrusion.
    The gradient index is offset by the frame counter so a colour wave
    travels down the letters instead of sitting still.
    """
    lines = LOGO_BLOCK if BLOCKS else LOGO_ASCII
    phase = _frame // 3
    t = Text()
    for i, line in enumerate(lines):
        face = GRADIENT[(i + phase) % len(GRADIENT)]
        # Batch runs of identical style instead of appending per character -
        # this runs every frame, so it is worth not making 250 calls of it.
        run, run_style = "", None
        for ch in line:
            style = DIM_EDGE if ch in BEVEL else f"bold {face}"
            if style != run_style:
                if run:
                    t.append(run, style=run_style)
                run, run_style = ch, style
            else:
                run += ch
        if run:
            t.append(run, style=run_style)
        if i < len(lines) - 1:
            t.append("\n")
    return t


def sweep_text(width: int = 22, active: bool = True, color: str = NEON_CYAN) -> Text:
    """A bright head with a fading tail, bouncing across a dim track."""
    track = "\u2591" if BLOCKS else "-"
    head = "\u2588" if BLOCKS else "="
    mid = "\u2592" if BLOCKS else "="
    if width < 4:
        return Text("")
    if not active:
        return Text(track * width, style=DEAD)
    span = width * 2 - 2
    i = (_frame // 2) % span
    pos = i if i < width else span - i
    t = Text()
    for x in range(width):
        d = abs(x - pos)
        if d == 0:
            t.append(head, style=f"bold {color}")
        elif d == 1:
            t.append(mid, style=color)
        elif d == 2:
            t.append(mid, style=DIM_EDGE)
        else:
            t.append(track, style=DEAD)
    return t


def signal_text(ms) -> Text:
    """Four-bar signal meter. Fewer bars means worse latency."""
    levels = "\u2581\u2583\u2585\u2587" if BLOCKS else ".oO0"
    if ms is None:
        return Text(levels, style=DEAD)
    n = 4 if ms < 120 else 3 if ms < 200 else 2 if ms < 320 else 1
    color = (NEON_GREEN, NEON_YELLOW, NEON_ORANGE, NEON_ORANGE)[4 - n]
    t = Text()
    for i, ch in enumerate(levels):
        t.append(ch, style=f"bold {color}" if i < n else DEAD)
    return t


def pulse(base: str) -> str:
    """Alternates bold on a slow cycle so 'live' elements visibly breathe."""
    return f"bold {base}" if (_frame // 4) % 2 == 0 else base


# ------------------------------------------------------------ event feed ---
_events = deque(maxlen=9)
_TAG_STYLES = {
    "LINK": NEON_GREEN, "SWAP": NEON_CYAN, "DEAD": "red",
    "SCAN": NEON_PURPLE, "WARN": NEON_ORANGE, "SYS": "grey62",
}


def log_event(tag: str, message: str):
    _events.append((time.strftime("%H:%M:%S"), tag, message))


# ------------------------------------------------------------- primitives ---
SPARK = "▁▂▃▄▅▆▇█" if BLOCKS else ".:-=+*#%"
FILL, EMPTY = ("█", "░") if BLOCKS else ("#", "-")


def divider(width: int = 60) -> Text:
    # U+25AC looks right but is not in CP437, so it renders as an empty box
    # in a stock Windows console. The half-block below is, and reads the same.
    t = Text()
    ch = "▀" if BLOCKS else "="
    for i in range(width):
        t.append(ch, style=GRADIENT[i % len(GRADIENT)])
    return t


def bar(fraction: float, width: int = 12) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return FILL * filled + EMPTY * (width - filled)


def spark_text(values, width: int = 30) -> Text:
    """Latency history. Gaps are drops - they read as breaks in the line."""
    recent = list(values)[-width:]
    t = Text()
    if not recent:
        return Text(EMPTY * width, style=DEAD)
    live = [v for v in recent if v is not None]
    if not live:
        return Text("x" * len(recent), style="red")
    lo, hi = min(live), max(live)
    span = max(hi - lo, 1.0)
    for v in recent:
        if v is None:
            t.append("x", style="bold red")
            continue
        idx = int((v - lo) / span * (len(SPARK) - 1))
        style = NEON_GREEN if v < 150 else (NEON_YELLOW if v < 250 else NEON_ORANGE)
        t.append(SPARK[max(0, min(idx, len(SPARK) - 1))], style=style)
    return t


def fmt_uptime(since) -> str:
    if not since:
        return "0s"
    s = int(time.time() - since)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


# ----------------------------------------------------------------- panels ---
def logo_width() -> int:
    return len(LOGO_BLOCK[0]) if BLOCKS else len(LOGO_ASCII[0])


def header_height(max_width: int) -> int:
    """Full banner needs room for the logo plus the stats block beside it."""
    return HEADER_H if max_width >= logo_width() + 34 else HEADER_COMPACT_H


def build_header(state, xray_alive, max_width: int = 120) -> Panel:
    spin = next(_spinner)
    current = state["current"]

    if state["scan"][1]:
        done, total = state["scan"]
        badge = Text(f"{spin} SCANNING", style=f"bold {NEON_PURPLE}")
        detail = f"{done}/{total} tested in parallel"
        edge = NEON_PURPLE
    elif state["reconnecting"]:
        i, total = state["attempt"]
        badge = Text(f"{spin} SWITCHING", style=f"bold {NEON_ORANGE}")
        detail = f"trying {i}/{total}" if total else "finding a route"
        edge = NEON_ORANGE
    elif current and xray_alive and state["connected_ok"]:
        badge = Text(f"{spin} LIVE", style=f"bold {NEON_GREEN}")
        detail = f"{current.add}:{current.port}"
        edge = next(_gradient_cycle)
    elif current and xray_alive:
        badge = Text(f"{spin} VERIFYING", style=f"bold {NEON_YELLOW}")
        detail = f"{current.add}:{current.port}"
        edge = NEON_YELLOW
    else:
        badge = Text(f"{spin} DOWN", style="bold red")
        detail = "reconnecting"
        edge = "red"

    right = Table.grid(expand=True, padding=(0, 0))
    right.add_column(justify="right")
    right.add_row(badge)
    right.add_row(Text("A U T O - C O N N E C T", style=f"bold {NEON_PINK}"))
    right.add_row(Text(detail[:26], style="white"))
    right.add_row(Text(f"up {fmt_uptime(state['connected_since'])}", style="grey62"))
    clock_sep = ":" if int(time.time()) % 2 == 0 else " "
    now = time.strftime(f"%H{clock_sep}%M{clock_sep}%S")
    right.add_row(Text(now, style="grey50"))

    # Middle column: fills the dead space between logo and stats with the
    # one number that actually matters - how much traffic is getting through.
    hist = state.get("health_history") or []
    rate = (sum(1 for h in hist if h) / len(hist)) if hist else 0.0
    qstyle = NEON_GREEN if rate > 0.9 else (NEON_YELLOW if rate > 0.6 else "red")
    mid = Table.grid(expand=True, padding=(0, 0))
    # no_wrap: a narrow terminal must crop these, not reflow them - wrapping
    # changes the header's row count and desyncs it from the logo.
    mid.add_column(justify="center", no_wrap=True, overflow="ellipsis")

    # The sweep runs faster while scanning and idles when the link is down,
    # so the animation actually reports something instead of just moving.
    scanning = bool(state["scan"][1]) or state["reconnecting"]
    sweep_color = NEON_PURPLE if scanning else (
        NEON_GREEN if state["connected_ok"] else NEON_ORANGE)

    latency = None
    lat_hist = state.get("latency_history")
    if lat_hist:
        live_vals = [v for v in lat_hist if v is not None]
        latency = live_vals[-1] if live_vals else None

    sig = Text()
    sig.append_text(signal_text(latency if state["connected_ok"] else None))
    sig.append("  self-healing tunnel  ", style=f"italic {NEON_CYAN}")
    sig.append_text(signal_text(latency if state["connected_ok"] else None))

    mid.add_row(Text(""))
    mid.add_row(sig)
    mid.add_row(Text("github.com/Amirzamani1l", style="grey42"))
    mid.add_row(sweep_text(22, active=xray_alive, color=sweep_color))
    mid.add_row(Text(f"LINK {bar(rate, 12)} {rate * 100:3.0f}%",
                     style=qstyle if hist else DEAD))

    logo_w = logo_width()
    if max_width < logo_w + 34:
        # No room for the banner beside the stats. Collapse to two lines
        # rather than letting the columns collide and reflow.
        compact = Table.grid(expand=True, padding=(0, 1))
        compact.add_column(justify="left", no_wrap=True, overflow="ellipsis")
        compact.add_column(justify="right", no_wrap=True, overflow="crop", width=24)
        title = Text(PROJECT_NAME, style=f"bold {GRADIENT[(_frame // 3) % len(GRADIENT)]}")
        compact.add_row(title, badge)
        compact.add_row(
            sweep_text(min(24, max(6, max_width - 30)), active=xray_alive,
                       color=sweep_color),
            Text(detail[:24], style="white"),
        )
        return Panel(compact, border_style=edge, box=BOX_MAIN, padding=(0, 1))

    head = Table.grid(expand=True, padding=(0, 1))
    head.add_column(justify="left", width=logo_w)
    head.add_column(justify="center", ratio=1)
    head.add_column(justify="right", width=26)
    head.add_row(logo_text(), mid, right)
    return Panel(head, border_style=edge, box=BOX_MAIN, padding=(0, 1))


# All three mid-row panels are padded to this many rows. rich sizes each
# panel to its own content, so without this they'd close at different
# heights and the row would look ragged.
PANEL_ROWS = 7


def _pad(grid: Table, used: int, cols: int = 2):
    for _ in range(max(0, PANEL_ROWS - used)):
        grid.add_row(*([""] * cols))


def build_tunnel_panel(state, xray_alive, proxy_on, socks_port, http_port) -> Panel:
    current = state["current"]
    t = Table.grid(padding=(0, 1))
    t.add_column(style="grey62", justify="left", width=9, no_wrap=True)
    t.add_column(justify="left", ratio=1, no_wrap=True, overflow="ellipsis")

    if current and xray_alive and state["connected_ok"]:
        node, node_style = f"{current.add}:{current.port}", f"bold {NEON_GREEN}"
    elif current:
        node, node_style = f"{current.add}:{current.port}", NEON_ORANGE
    else:
        node, node_style = "-", DEAD
    t.add_row("node", Text(node[:24], style=node_style))
    t.add_row("stable", Text(fmt_uptime(state["connected_since"]), style="white"))
    t.add_row("sys proxy", Text("ON", style=f"bold {NEON_GREEN}") if proxy_on
              else Text("manual", style=NEON_YELLOW))

    hist = state.get("health_history")
    if hist:
        rate = sum(1 for h in hist if h) / len(hist)
        style = NEON_GREEN if rate > 0.9 else (NEON_YELLOW if rate > 0.6 else "red")
        t.add_row("quality", Text(f"{bar(rate, 10)} {rate * 100:3.0f}%", style=style))
    else:
        t.add_row("quality", Text(f"{bar(0, 10)}   -", style=DEAD))

    t.add_row("socks5", Text(f"127.0.0.1:{socks_port}", style="white"))
    t.add_row("http", Text(f"127.0.0.1:{http_port}", style="white"))
    _pad(t, 6)

    return Panel(t, title=f"[bold {NEON_CYAN}]TUNNEL[/]", border_style=NEON_CYAN,
                 box=BOX_PANEL, padding=(0, 1))


def build_latency_panel(state) -> Panel:
    hist = state.get("latency_history") or deque()
    live = [v for v in hist if v is not None]
    body = Table.grid(padding=(0, 0))
    body.add_column(no_wrap=True, overflow="crop")
    body.add_row(spark_text(hist, 30))
    body.add_row(Text(""))

    stats = Table.grid(padding=(0, 2))
    stats.add_column(style="grey62", width=5, no_wrap=True)
    stats.add_column(justify="right", ratio=1, no_wrap=True, overflow="crop")
    now = live[-1] if live else None
    stats.add_row("now", Text(f"{now:.0f}ms" if now else "-",
                              style=NEON_GREEN if (now and now < 150) else NEON_ORANGE))
    stats.add_row("avg", Text(f"{sum(live) / len(live):.0f}ms" if live else "-", style="white"))
    stats.add_row("best", Text(f"{min(live):.0f}ms" if live else "-", style=NEON_CYAN))
    drops = sum(1 for v in hist if v is None)
    stats.add_row("drops", Text(str(drops), style="red" if drops else "grey62"))
    body.add_row(stats)
    _pad(body, 6, cols=1)

    return Panel(body, title=f"[bold {NEON_PURPLE}]LATENCY[/]",
                 border_style=NEON_PURPLE, box=BOX_PANEL, padding=(0, 1))


def build_pool_panel(state, server_count, dead_count) -> Panel:
    pool = state["pool"]
    t = Table.grid(padding=(0, 1))
    t.add_column(style="grey62", width=8, no_wrap=True)
    t.add_column(justify="left", ratio=1, no_wrap=True, overflow="crop")

    t.add_row("scraped", Text(str(server_count), style="white"))
    t.add_row("verified", Text(f"{bar(len(pool) / 6, 8)} {len(pool)}",
                               style=NEON_GREEN if pool else DEAD))
    t.add_row("dead", Text(f"{bar(dead_count / max(server_count, 1), 8)} {dead_count}",
                           style="red" if dead_count else "grey62"))
    t.add_row("", "")

    used = 4
    if len(pool) > 1:
        t.add_row(Text("standby", style="grey62"), "")
        used += 1
        for server, ms in pool[1:3]:
            t.add_row("", Text(f"{server.add[:16]:<16}{ms:>6.0f}ms", style=NEON_CYAN))
            used += 1
    else:
        t.add_row("", Text("no verified backups yet", style=DEAD))
        used += 1
    _pad(t, used)

    return Panel(t, title=f"[bold {NEON_YELLOW}]POOL[/]", border_style=NEON_YELLOW,
                 box=BOX_PANEL, padding=(0, 1))


def build_grid(servers, prober, state, benched_keys, rows: int = 10) -> Panel:
    current = state["current"]
    pool_keys = {s.key for s, _ in state["pool"]}
    table = Table(expand=True, box=BOX_PANEL, border_style="grey30",
                  header_style=f"bold {NEON_GREEN}", row_styles=["", "on grey11"],
                  pad_edge=False)
    table.add_column("", justify="center", width=3, no_wrap=True)
    table.add_column("SERVER", style="white", no_wrap=True)
    table.add_column("PING", justify="right", width=7, no_wrap=True)
    table.add_column("", width=12, no_wrap=True)
    table.add_column("PROTO", width=9, no_wrap=True)
    table.add_column("SEC", justify="center", width=5, no_wrap=True)
    table.add_column("SOURCE", style="grey50", no_wrap=True)

    ranked = []
    for s in servers:
        ping = prober.get_ping(s)
        ranked.append(((1 if s.key in benched_keys else 0),
                       ping if ping is not None else float("inf"), s))
    ranked.sort(key=lambda r: (r[0], r[1]))

    emitted = 0
    for is_benched, ping, s in ranked[:rows]:
        is_current = current and s.key == current.key
        if is_current and state["connected_ok"]:
            mark, mstyle = ">>", f"bold {NEON_GREEN}"
        elif is_current:
            mark, mstyle = "..", NEON_ORANGE
        elif s.key in pool_keys:
            mark, mstyle = "ok", NEON_CYAN
        elif is_benched:
            mark, mstyle = "xx", DEAD
        elif ping != float("inf"):
            mark, mstyle = "--", "grey50"
        else:
            mark, mstyle = "!!", "red"

        if is_benched:
            pstyle = DEAD
        elif ping == float("inf"):
            pstyle = "red"
        elif ping < 150:
            pstyle = NEON_GREEN
        elif ping < 250:
            pstyle = NEON_YELLOW
        else:
            pstyle = NEON_ORANGE

        if ping == float("inf"):
            meter = Text(EMPTY * 12, style=DEAD)
        else:
            # shorter bar = faster, so a full bar means a slow link
            meter = Text(bar(min(ping / 400.0, 1.0), 12), style=pstyle)

        if is_current:
            nstyle = f"bold {NEON_GREEN}" if state["connected_ok"] else f"bold {NEON_ORANGE}"
        elif is_benched:
            nstyle = DEAD
        else:
            nstyle = "white"

        table.add_row(
            Text(mark, style=mstyle),
            Text(f"{s.add}:{s.port}"[:30], style=nstyle),
            Text(f"{ping:.0f}ms" if ping != float("inf") else "-", style=pstyle),
            meter,
            Text(s.net + (f"+{s.tls}" if s.tls else ""),
                 style=DEAD if is_benched else "grey62"),
            Text("*" * s.safety_score if s.safety_score else "-",
                 style=DEAD if is_benched else NEON_YELLOW),
            Text(s.source_repo[:26], style=DEAD if is_benched else "grey50"),
        )
        emitted += 1

    # Pad to a constant height. A panel that grows and shrinks between
    # frames forces rich to repaint everything below it, which is the
    # single biggest source of flicker in a live layout.
    for _ in range(rows - emitted):
        table.add_row("", "", "", "", "", "", "")

    return Panel(table, title=f"[bold {NEON_CYAN}]LIVE SERVER GRID[/]",
                 border_style=NEON_CYAN, box=BOX_PANEL, padding=(0, 0))


def build_feed(rows: int = 8) -> Panel:
    t = Table.grid(padding=(0, 1))
    t.add_column(style="grey50", width=8)
    t.add_column(width=6)
    t.add_column(ratio=1, no_wrap=True, overflow="ellipsis")

    recent = list(_events)[-rows:]
    if not recent:
        t.add_row("", "", Text("waiting for events...", style=DEAD))
        recent = [None]
    else:
        # Older lines fade out, so the newest event reads first.
        depth = len(recent)
        for i, (stamp, tag, msg) in enumerate(recent):
            fresh = i >= depth - 2
            t.add_row(
                Text(stamp, style="grey50" if fresh else "grey30"),
                Text(tag, style=f"bold {_TAG_STYLES.get(tag, 'white')}"
                     if fresh else _TAG_STYLES.get(tag, "grey42")),
                Text(msg, style="grey74" if fresh else "grey42"),
            )
    for _ in range(rows - len(recent)):
        t.add_row("", "", "")

    return Panel(t, title=f"[bold {NEON_PINK}]LIVE FEED[/]", border_style=NEON_PINK,
                 box=BOX_PANEL, padding=(0, 1))


# Fixed heights, in terminal rows, including panel borders. Keeping the
# total under the terminal height is what stops the flicker: once the live
# region overflows, rich has to scroll and repaint the whole thing.
MID_H = 9
GRID_CHROME = 6      # panel border + table border + header + rule
FEED_CHROME = 2
GRID_MIN, GRID_MAX = 4, 16
FEED_MIN, FEED_MAX = 3, 9


def render(state, prober, servers, xray_alive, proxy_on,
           socks_port, http_port, benched_keys,
           max_height: int = 60, max_width: int = 120) -> Group:
    tick()

    mid = Table.grid(expand=True, padding=(0, 1))
    mid.add_column(ratio=3)
    mid.add_column(ratio=3)
    mid.add_column(ratio=4)
    mid.add_row(
        build_tunnel_panel(state, xray_alive, proxy_on, socks_port, http_port),
        build_latency_panel(state),
        build_pool_panel(state, len(servers), len(benched_keys)),
    )

    parts = [build_header(state, xray_alive, max_width)]
    # One row of slack: rich treats a live region that exactly fills the
    # screen as an overflow on some terminals.
    left = max_height - header_height(max_width) - 1

    # Panels are dropped from the bottom of the priority list first, so a
    # short terminal loses detail rather than overflowing and tearing.
    if left >= MID_H + GRID_MIN + GRID_CHROME:
        parts.append(mid)
        left -= MID_H

    if left < GRID_MIN + GRID_CHROME:
        return Group(*parts)          # nothing else fits

    feed_rows = 0
    if left >= GRID_MIN + GRID_CHROME + FEED_MIN + FEED_CHROME:
        feed_rows = min(FEED_MAX, max(FEED_MIN,
                                      (left - GRID_CHROME - FEED_CHROME) // 3))
        left -= feed_rows + FEED_CHROME

    grid_rows = max(GRID_MIN, min(GRID_MAX, left - GRID_CHROME))

    parts.append(build_grid(servers, prober, state, benched_keys, rows=grid_rows))
    if feed_rows:
        parts.append(build_feed(rows=feed_rows))
    return Group(*parts)


# --------------------------------------------------------------- startup ---
BOOT_LOG = [
    ("SYS", "initializing runtime", NEON_CYAN),
    ("KEY", "loading credentials from .env", NEON_CYAN),
    ("NET", "establishing uplink to github.com", NEON_PURPLE),
    ("SCAN", "scanning public repositories for vmess relays", NEON_PURPLE),
    ("DEC", "decoding configs", NEON_PINK),
    ("SEC", "scoring transport security", NEON_PINK),
    ("XRAY", "spinning up xray-core", NEON_YELLOW),
    ("OK", "ready", NEON_GREEN),
]


# ------------------------------------------------------------- blood drip ---
def _logo_lines():
    return LOGO_BLOCK if BLOCKS else LOGO_ASCII


def _drip_plan():
    """Pick columns under the letters that bleed, and how far each runs."""
    lines = _logo_lines()
    W = max(len(l) for l in lines)
    rng = random.Random(1313)
    seeds = {}
    for c in range(W):
        ink = any(c < len(lines[r]) and lines[r][c] != " "
                  for r in (len(lines) - 2, len(lines) - 1))
        if ink and rng.random() < 0.45:
            seeds[c] = rng.choice([2, 2, 3, 3, 4, 5])
    return W, seeds


def _drip_rows(W, seeds, lengths):
    body = "\u2588" if BLOCKS else "|"     # full block / pipe
    drop = "\u2584" if BLOCKS else "."     # hanging drop
    rows = max(lengths.values(), default=0)
    out = []
    for r in range(rows):
        t = Text()
        run, run_style = "", None
        for c in range(W):
            L = lengths.get(c, 0)
            if seeds.get(c, 0) and r < L:
                ch = drop if r == L - 1 else body
                style = BLOOD_BRIGHT if r == L - 1 else (BLOOD if r < 2 else BLOOD_DEEP)
            else:
                ch, style = " ", None
            if style == run_style:
                run += ch
            else:
                if run:
                    t.append(run, style=run_style)
                run, run_style = ch, style
        if run:
            t.append(run, style=run_style)
        out.append(t)
    return out


def print_banner(console):
    import time as _t
    console.print()
    logo = logo_text()
    W, seeds = _drip_plan()
    sub = [
        Align.center(Text("A U T O - C O N N E C T", style=f"bold {BLOOD_BRIGHT}")),
        Align.center(Text("self-healing v2ray connection manager", style=f"italic {BLOOD}")),
        Align.center(Text("by Amirzamani1l - github.com/Amirzamani1l", style=ASH)),
    ]

    # Blood wells up out of the letters and runs down. auto_refresh OFF +
    # transient=False: this loop is the only writer, and the final frame is
    # left on screen so the boot log appends under it - no clear-and-reprint
    # flash between the two.
    frames = 15
    try:
        with Live(console=console, auto_refresh=False, transient=False) as live:
            for f in range(frames):
                p = (f + 1) / frames
                lengths = {c: max(0, round(m * p)) for c, m in seeds.items()}
                content = [Align.center(logo)]
                content += [Align.center(r) for r in _drip_rows(W, seeds, lengths)]
                if f == frames - 1:
                    content.append(Text(""))
                    content += sub
                live.update(Group(*content), refresh=True)
                _t.sleep(0.045)
    except Exception:
        console.print(Align.center(logo))
        for r in _drip_rows(W, seeds, dict(seeds)):
            console.print(Align.center(r))
        for s in sub:
            console.print(s)
    console.print()


async def boot_sequence(console, sleep):
    for tag, msg, color in BOOT_LOG:
        line = Text()
        line.append(f"[{tag}]", style=f"bold {color}")
        line.append("  ")
        line.append(msg, style=BONE)
        line.append("   ")
        line.append("OK", style=f"bold {ROT}")
        console.print(Align.center(line))
        await sleep(0.09)
    console.print()
