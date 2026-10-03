"""Local-only controls and offline readiness checks. No account network access."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import socket
import sqlite3
import stat
import sys
from pathlib import Path
from urllib.parse import urlparse

from shadow.settings import REPO_ROOT, SettingsError, data_dir, load_settings, validate_for_mode
from shadow.storage import DB_FILENAME, CURRENT_VERSION, StorageError

CONTROL_SOCKET = "control.sock"


def readiness(environ=None):
    """Read-only, redacted readiness. Missing live gates never become healthy."""
    environment = os.environ if environ is None else environ
    checks = {"python": sys.version_info >= (3, 11)}
    for dependency in ("click", "playwright", "boto3", "python-dotenv"):
        try:
            checks[dependency] = importlib.metadata.version(dependency)
        except importlib.metadata.PackageNotFoundError:
            checks[dependency] = False
    try:
        directory = data_dir()
        settings = validate_for_mode(load_settings(directory))
        checks["settings"] = True
        checks["fresh_mode_observe"] = settings.mode == "observe"
        checks["private_permissions"] = (not directory.exists() or os.name == "nt"
                                          or directory.stat().st_mode & 0o077 == 0)
        path = directory / DB_FILENAME
        if path.exists():
            with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as database:
                checks["database_integrity"] = database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                checks["database_version_supported"] = database.execute("PRAGMA user_version").fetchone()[0] <= CURRENT_VERSION
        else:
            checks["database"] = "not_created"
    except (SettingsError, OSError, sqlite3.Error):
        checks["private_settings_or_storage"] = False
    cdp = urlparse(environment.get("SHADOW_CDP_URL", "http://127.0.0.1:9222"))
    checks["cdp_loopback"] = cdp.scheme in ("http", "https") and cdp.hostname in ("127.0.0.1", "localhost", "::1")
    checks["account_binding_configured"] = all(environment.get(key, "").strip() for key in
                                                ("SHADOW_ACCOUNT_ID", "SHADOW_EXPECTED_ACCOUNT"))
    checks["role_models_configured"] = all(environment.get(f"SHADOW_{role}_MODEL", "").strip()
                                           for role in ("RISK", "REPLY", "EXTRACTION"))
    checks["telegram_configured"] = bool(environment.get("SHADOW_TELEGRAM_TOKEN")) and all(
        environment.get(key, "").isdigit() and int(environment[key]) > 0
        for key in ("SHADOW_TELEGRAM_USER_ID", "SHADOW_TELEGRAM_CHAT_ID"))
    return {"checks": checks, "status": "needs_setup" if any(value is False for value in checks.values()) else "offline_ready",
            "live_account": "not_checked", "model_qualification": "pending_external",
            "live_submission": "locked"}


def private_destination(path, *, must_be_new=True):
    target = Path(path).expanduser().resolve()
    if target == REPO_ROOT or REPO_ROOT in target.parents:
        raise ValueError("Private backup/restore destinations must be outside the checkout")
    if must_be_new and target.exists():
        raise ValueError("Destination already exists; refusing to overwrite")
    return target


async def start_controls(directory, coordinator):
    """A mode-0600 Unix socket under the mode-0700 data directory.

    The existing data-directory process lock excludes a second runtime. A stale
    socket can be removed only after that lock was acquired by this caller.
    """
    if not hasattr(asyncio, "start_unix_server"):
        return None
    path = Path(directory) / CONTROL_SOCKET
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise StorageError("Local control path is not a socket; refusing to replace it")
        path.unlink()

    async def client(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2)
            if len(raw) > 128:
                raise ValueError("oversized local command")
            command = raw.decode("ascii").strip()
            if command == "pause":
                coordinator.pause("owner")
                result = {"status": "paused", "submitted_messages": "cannot_be_recalled"}
            elif command == "resume":
                runtime = coordinator.storage.get_runtime() or {}
                if runtime.get("pause_reason") != "owner":
                    result = {"status": "refused", "reason": "health_or_identity_pause"}
                else:
                    coordinator.resume()
                    result = {"status": "owner_pause_cleared", "thread_holds": "unchanged"}
            elif command == "status":
                runtime = coordinator.storage.get_runtime() or {}
                result = {"mode": coordinator.mode, "pause": runtime.get("pause_reason"),
                          "schema_version": CURRENT_VERSION, "submission": "locked"}
            else:
                result = {"status": "refused", "reason": "unsupported_local_control"}
            writer.write((json.dumps(result) + "\n").encode("utf-8"))
            await writer.drain()
        except (ValueError, OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(client, path=path, limit=256)
    os.chmod(path, 0o600)
    return server


def local_control(directory, command):
    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("Local socket controls unavailable on this platform")
    if command not in ("status", "pause", "resume"):
        raise ValueError("Unsupported local command")
    path = Path(directory) / CONTROL_SOCKET
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall((command + "\n").encode("ascii"))
        data = b""
        while b"\n" not in data and len(data) < 4096:
            chunk = client.recv(4096 - len(data))
            if not chunk:
                break
            data += chunk
        return json.loads(data)
