"""
ghost.py
Fixed-size cold-open for WRAITH.

1. lock_terminal() pins the console to a fixed size (wide enough for the WRAITH
   UI, so nothing gets truncated to "...") and, on Windows, strips the maximize
   button and the sizing border so the window can't be resized. Called once from
   WRAITH.py, so the whole app runs at one predictable size.

2. ghost_intro() plays the actual vintage ghost animation the user provided,
   rendered as a live ASCII film that fills the fixed console. The frames live in
   ghost_frames.py (the real GIF, decoded + compressed); this module decompresses
   them once and resamples each frame to the console. Tone is pushed through a
   gamma curve so the flat-white ghost reads brighter than the grey room, then
   coloured grey-scale with a faint old-film flicker. CP437-safe glyphs only, so
   it renders in a stock Windows cmd.exe.

Everything is wrapped in try/except - decoration must never stop the app from
starting. Skip it with  WRAITH_INTRO=0 .
"""

import base64
import random
import zlib

from rich.align import Align
from rich.console import Group
from rich.live import Live
from rich.text import Text

# ---- the one fixed size the whole app runs at (wide enough for the UI) ----
FIXED_COLS = 120
FIXED_ROWS = 42

_RAMP = " .:-=+*oah#%@$"
_NR = len(_RAMP) - 1
_GAMMA = 1.3                      # mild lift so the flat-white ghost reads over the grey room

try:
    from ghost_frames import BASE_W, BASE_H, N_FRAMES, FPS, _B64
    _RAW = zlib.decompress(base64.b64decode(_B64))
    _FL = BASE_W * BASE_H
    _FRAMES = [_RAW[k * _FL:(k + 1) * _FL] for k in range(N_FRAMES)]
except Exception:
    _FRAMES = []
    BASE_W = BASE_H = 1
    N_FRAMES = 0
    FPS = 12

_ASPECT = (BASE_H / BASE_W) * 0.5 if _FRAMES else 0.35

# char per 0-255 sample is flicker-independent (shape stays put) -> precompute
_CHAR = []
for _v in range(256):
    _t = (_v / 255.0) ** _GAMMA
    _CHAR.append(_RAMP[min(_NR, int(_t * _NR))])


def lock_terminal(cols=FIXED_COLS, rows=FIXED_ROWS):
    """Pin the console to cols x rows and forbid resizing where the OS allows it."""
    try:
        import os
        import platform
        if platform.system() == "Windows":
            os.system(f"mode con: cols={cols} lines={rows}")   # size + buffer (kills scrollbar)
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # Turn ON VT processing. Without it, a stock cmd.exe renders
                # cell-by-cell through the legacy Windows console API, and a
                # full-screen redraw of that tears. With it, rich streams one
                # ANSI frame into the alternate screen buffer, which doesn't.
                h = kernel32.GetStdHandle(-11)   # STD_OUTPUT_HANDLE
                mode = ctypes.c_uint32()
                if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            except Exception:
                pass
            try:
                import ctypes
                user32 = ctypes.windll.user32
                hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                if hwnd:
                    GWL_STYLE = -16
                    WS_MAXIMIZEBOX = 0x00010000
                    WS_THICKFRAME = 0x00040000        # drag-to-resize border
                    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
                    style &= ~WS_MAXIMIZEBOX
                    style &= ~WS_THICKFRAME
                    user32.SetWindowLongW(hwnd, GWL_STYLE, style)
                    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED
            except Exception:
                pass
        else:
            import sys
            sys.stdout.write(f"\x1b[8;{rows};{cols}t")         # xterm resize (best effort)
            sys.stdout.flush()
    except Exception:
        pass


def _fit():
    cols = FIXED_COLS - 2
    rows = round(cols * _ASPECT)
    avail = FIXED_ROWS - 3
    if rows > avail:
        rows = avail
        cols = min(cols, round(rows / _ASPECT) if _ASPECT else cols)
    return cols, rows


def _col_lut(bright):
    """256-entry colour LUT for this frame's brightness (gamma already baked into value)."""
    lut = []
    for v in range(256):
        t = (v / 255.0) ** _GAMMA
        lum = t * bright
        if lum > 1.0:
            lum = 1.0
        q = (int(lum * 255) // 16) * 16
        lut.append(None if q == 0 else f"#{int(q*0.86):02x}{int(q*0.93):02x}{q:02x}")
    return lut


def _render(frame, cols, rows, xmap, row_off, top_pad, col_lut):
    lines = [Text("")] * top_pad
    for y in range(rows):
        off = row_off[y]
        txt = Text()
        run = ""
        run_key = None
        for x in range(cols):
            v = frame[off + xmap[x]]
            ch = _CHAR[v]
            key = col_lut[v] if ch != " " else None
            if key == run_key:
                run += ch
            else:
                if run:
                    txt.append(run, style=run_key)
                run = ch
                run_key = key
        if run:
            txt.append(run, style=run_key)
        lines.append(txt)
    return Group(*lines)


async def ghost_intro(console, sleep):
    if not _FRAMES:
        return
    cols, rows = _fit()
    if cols < 40 or rows < 12:
        return
    xmap = [min(BASE_W - 1, (x * BASE_W) // cols) for x in range(cols)]
    ymap = [min(BASE_H - 1, (y * BASE_H) // rows) for y in range(rows)]
    row_off = [yy * BASE_W for yy in ymap]
    top_pad = max(0, (FIXED_ROWS - 1 - (rows + 2)) // 2)

    rng = random.Random(7)
    fps = FPS if FPS else 12
    delay = 1.0 / max(8, fps)
    fade_in = 7
    loops = 1

    def compose(body, vis):
        cv = int(95 * vis)
        cap = Text(">>  W R A I T H  //  a route through the dark  <<",
                   style=f"#{int(cv*0.7):02x}{int(cv*0.4):02x}{int(cv*0.45):02x}")
        return Group(Align.center(body), Text(""), Align.center(cap))

    def flick(step):
        f = 0.90 + 0.12 * rng.random()
        if rng.random() < 0.06:
            f *= 0.6
        return f

    try:
        console.clear()
        with Live(console=console, transient=True, auto_refresh=False,
                  vertical_overflow="crop", screen=True) as live:
            total = N_FRAMES * loops
            for step in range(total):
                idx = step % N_FRAMES
                b = 1.0
                if step < fade_in:
                    b = (step + 1) / fade_in
                b *= flick(step)
                body = _render(_FRAMES[idx], cols, rows, xmap, row_off,
                               top_pad, _col_lut(min(1.0, b)))
                live.update(compose(body, min(1.0, b)), refresh=True)
                await sleep(delay)
            # fade to black, then hand off to the banner
            last = _FRAMES[(total - 1) % N_FRAMES]
            for i in range(8):
                b = max(0.0, 1.0 - (i + 1) / 8)
                body = _render(last, cols, rows, xmap, row_off, top_pad, _col_lut(b))
                live.update(compose(body, b), refresh=True)
                await sleep(0.05)
    except Exception:
        pass
