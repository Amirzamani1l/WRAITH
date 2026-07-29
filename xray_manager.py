"""
xray_manager.py
Auto-downloads Xray-core (if missing), builds config, manages the process.
"""

import hashlib
import ipaddress
import json
import os
import platform
import socket
import subprocess
import zipfile
from pathlib import Path
from typing import Optional

import requests

from scraper import Server

def _app_dir() -> Path:
    """
    Persistent per-user storage location. Using the script/exe's own folder
    would mean re-downloading xray-core on every launch of the compiled
    .exe (PyInstaller --onefile extracts to a fresh temp dir each run), so
    everything lives in the OS's standard per-user data folder instead.
    """
    if platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "WRAITH"
    d.mkdir(parents=True, exist_ok=True)
    return d


BASE_DIR = _app_dir()
BIN_DIR = BASE_DIR / "bin"
CONFIG_PATH = BASE_DIR / "config_active.json"

SOCKS_PORT = 10808
HTTP_PORT = 10809

RELEASES_API = "https://api.github.com/repos/XTLS/Xray-core/releases/latest"


def _xray_binary_path() -> Path:
    return BIN_DIR / ("xray.exe" if platform.system() == "Windows" else "xray")


def _verify_checksum(zip_path: Path, download_url: str) -> None:
    """
    XTLS publishes a <asset>.dgst file alongside every release asset with
    MD5/SHA1/SHA256/SHA512 lines. We fetch it and check our download
    against the SHA256 line before extracting anything - if GitHub or the
    network path in between is ever compromised, this is what catches a
    swapped binary instead of silently running it.
    """
    dgst_url = download_url + ".dgst"
    try:
        r = requests.get(dgst_url, timeout=20)
        r.raise_for_status()
        text = r.text
    except Exception as e:
        raise RuntimeError(f"could not fetch checksum file for verification: {e}")

    expected = None
    for line in text.splitlines():
        line = line.strip()
        key = line.split("=", 1)[0].strip().upper().replace("-", "")
        if key in ("SHA256", "SHA2256"):
            expected = line.split("=", 1)[-1].strip().lower()
            break
    if not expected:
        raise RuntimeError("checksum file did not contain a SHA256 line")

    actual = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"checksum mismatch - expected {expected[:16]}..., got "
            f"{actual[:16]}... The download may be corrupted or tampered "
            "with. Refusing to run it."
        )


def ensure_xray_binary() -> Path:
    """Downloads xray-core from the project's official GitHub releases if not already present."""
    exe = _xray_binary_path()
    if exe.exists():
        return exe

    BIN_DIR.mkdir(exist_ok=True)
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Windows":
        asset_hint = "windows-64" if "64" in machine or "amd" in machine else "windows-32"
    elif system == "Darwin":
        asset_hint = "macos-arm64-v8a" if "arm" in machine else "macos-64"
    else:
        asset_hint = "linux-64"

    print("[xray_manager] Fetching latest Xray-core release info...")
    r = requests.get(RELEASES_API, timeout=20)
    r.raise_for_status()
    release = r.json()

    asset = None
    for a in release.get("assets", []):
        name = a["name"].lower()
        if asset_hint in name and name.endswith(".zip"):
            asset = a
            break
    if not asset:
        raise RuntimeError(f"No matching release found for {system}/{machine}.")

    print(f"[xray_manager] Downloading {asset['name']} ...")
    zip_path = BIN_DIR / asset["name"]
    with requests.get(asset["browser_download_url"], stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    print("[xray_manager] Verifying checksum...")
    try:
        _verify_checksum(zip_path, asset["browser_download_url"])
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise
    print("[xray_manager] Checksum OK.")

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(BIN_DIR)
    zip_path.unlink(missing_ok=True)

    if system != "Windows":
        os.chmod(exe, 0o755)

    if not exe.exists():
        raise RuntimeError("Download finished but the xray executable was not found.")
    print("[xray_manager] Xray-core is ready.")
    return exe


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """
    True if something is already listening here. Without this check, a
    conflicting v2rayN/other xray instance makes our process fail to bind
    silently - it looks identical to a dead server from the outside, so
    every candidate gets benched for no reason.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def build_config(server: Server, socks_port: int = SOCKS_PORT,
                 http_port: int = HTTP_PORT, loglevel: str = "warning") -> dict:
    net = server.net or "tcp"
    stream_settings = {"network": net}

    if server.tls in ("tls", "reality"):
        stream_settings["security"] = "tls"

        # SNI is the thing that makes or breaks these. Most of what gets
        # scraped is CDN-fronted (Cloudflare ports 2053/2083/2087/8443/8880),
        # where the address is a bare edge IP and the real hostname lives in
        # `host`/`sni`. Sending an IP literal as serverName gets the handshake
        # rejected outright, so only fall back to the address when it's
        # actually a hostname.
        sni = server.sni or server.host
        if not sni and not _is_ip(server.add):
            sni = server.add

        tls_settings = {
            # These are anonymous, crowd-sourced servers with routinely
            # mismatched or self-signed certs. Strict validation just fails
            # the handshake - it was never authenticating anyone you trust.
            # See the security note in the README.
            "allowInsecure": True,
        }
        if sni:
            tls_settings["serverName"] = sni
        if server.alpn:
            tls_settings["alpn"] = [a.strip() for a in server.alpn.split(",") if a.strip()]
        # uTLS fingerprint - makes the handshake look like a normal browser
        tls_settings["fingerprint"] = server.fp or "chrome"
        stream_settings["tlsSettings"] = tls_settings

    if net == "ws":
        # The Host header is what routes you on the far side of a CDN.
        # Omitting it (the old behaviour) means the edge has no idea where
        # to send the upgrade, so the socket opens and then goes nowhere.
        ws_host = server.host or server.sni or (server.add if not _is_ip(server.add) else "")
        stream_settings["wsSettings"] = {
            "path": server.path or "/",
            "headers": {"Host": ws_host} if ws_host else {},
        }
    elif net == "grpc":
        stream_settings["grpcSettings"] = {"serviceName": server.path or ""}
    elif net == "h2":
        stream_settings["httpSettings"] = {
            "path": server.path or "/",
            "host": [server.host] if server.host else [],
        }
    elif net == "tcp" and server.type == "http":
        stream_settings["tcpSettings"] = {
            "header": {
                "type": "http",
                "request": {
                    "path": [server.path or "/"],
                    "headers": {"Host": [server.host] if server.host else []},
                },
            }
        }

    return {
        "log": {"loglevel": loglevel},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
            },
            {
                "listen": "127.0.0.1",
                "port": http_port,
                "protocol": "http",
                "settings": {},
            },
        ],
        "outbounds": [
            {
                "protocol": "vmess",
                "settings": {
                    "vnext": [
                        {
                            "address": server.add,
                            "port": server.port,
                            "users": [
                                {
                                    "id": server.id,
                                    "alterId": int(server.aid or 0),
                                    "security": server.scy or "auto",
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": stream_settings,
            },
            {"protocol": "freedom", "tag": "direct"},
        ],
    }


class XrayProcess:
    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.current_server: Optional[Server] = None
        self.exe_path = ensure_xray_binary()
        self.log_path = BASE_DIR / "xray_stderr.log"
        self.last_error: str = ""
        self._log_file = None

    def start(self, server: Server):
        self.stop()
        self.last_error = ""

        for port, label in ((SOCKS_PORT, "SOCKS"), (HTTP_PORT, "HTTP")):
            if _port_in_use(port):
                self.last_error = (
                    f"port {port} ({label}) is already in use - "
                    f"another WRAITH/v2rayN/xray instance running?"
                )
                raise RuntimeError(self.last_error)

        config = build_config(server)
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

        creationflags = 0
        if platform.system() == "Windows":
            creationflags = subprocess.CREATE_NO_WINDOW

        # stderr goes to a small log file instead of being discarded - if
        # xray fails to bind or the config is bad, there's somewhere to
        # actually look instead of just a silent "server didn't work".
        try:
            self._log_file = open(self.log_path, "w", encoding="utf-8", errors="ignore")
        except Exception:
            self._log_file = subprocess.DEVNULL

        self.proc = subprocess.Popen(
            [str(self.exe_path), "-c", str(CONFIG_PATH)],
            stdout=subprocess.DEVNULL,
            stderr=self._log_file,
            creationflags=creationflags,
        )
        self.current_server = server

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def delete_log(self):
        """
        Called on a clean shutdown. The stderr log can contain the IPs and
        UUIDs of whatever anonymous servers were tried this session - no
        reason for that to sit on disk once the program isn't running.
        """
        try:
            self.log_path.unlink(missing_ok=True)
        except Exception:
            pass

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            except Exception:
                # Process already gone (killed by the OS, crashed, etc.) -
                # nothing left to clean up.
                pass
        if self._log_file and self._log_file is not subprocess.DEVNULL:
            try:
                self._log_file.close()
            except Exception:
                pass
        self._log_file = None
        self.proc = None
        self.current_server = None
