"""
system_proxy.py
Turns the Windows system proxy ON/OFF so that Chrome, Edge, and most
other apps automatically route their traffic through the local Xray
HTTP proxy (127.0.0.1:10809) - no manual browser configuration needed.

It saves your ORIGINAL proxy settings the first time it runs and restores
them on exit, so closing the app puts your connection back exactly how it
was. Only the current user's settings are touched (HKCU), so no admin
rights are required.

On Linux / macOS these functions are harmless no-ops - there is no single
universal "system proxy" switch there, so those users still point their
app or browser at 127.0.0.1:10809 manually.
"""

import platform
from typing import Optional, Tuple

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    import ctypes
    import winreg

_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# Original state, remembered so disable() can restore it exactly.
_saved: Optional[Tuple[int, str]] = None
_active = False


def supported() -> bool:
    """True only on Windows, where we can flip the system proxy automatically."""
    return _IS_WINDOWS


def is_active() -> bool:
    return _active


def _refresh():
    """Tell WinINet the settings changed so Chrome/Edge pick them up immediately."""
    INTERNET_OPTION_SETTINGS_CHANGED = 39
    INTERNET_OPTION_REFRESH = 37
    try:
        set_option = ctypes.windll.Wininet.InternetSetOption
        set_option(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        set_option(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:
        pass


def _read_current() -> Tuple[int, str]:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as key:
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        except FileNotFoundError:
            enable = 0
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except FileNotFoundError:
            server = ""
    return int(enable), str(server)


def enable(host_port: str = "127.0.0.1:10809") -> bool:
    """
    Route system traffic through host_port. Saves the previous state the
    first time it's called so disable() can restore it. Returns True if the
    system proxy was actually turned on (Windows only).
    """
    global _saved, _active
    if not _IS_WINDOWS:
        return False
    try:
        if _saved is None:
            _saved = _read_current()
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0, winreg.KEY_WRITE
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, host_port)
            # Keep localhost + LAN traffic off the tunnel.
            winreg.SetValueEx(
                key, "ProxyOverride", 0, winreg.REG_SZ,
                "localhost;127.*;10.*;172.16.*;192.168.*;<local>",
            )
        _refresh()
        _active = True
        return True
    except Exception:
        return False


def disable() -> bool:
    """
    Restore the proxy settings to whatever they were before enable() ran
    (or simply switch it off if no previous state was recorded).
    Safe to call multiple times.
    """
    global _saved, _active
    if not _IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0, winreg.KEY_WRITE
        ) as key:
            if _saved is not None:
                prev_enable, prev_server = _saved
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, prev_enable)
                if prev_server:
                    winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, prev_server)
            else:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        _refresh()
        _active = False
        return True
    except Exception:
        return False
