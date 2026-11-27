"""Disposable agent-browser session. Playwright is not used."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from shadow.chromium import BrowserError


AGENT_BROWSER_VERSION = "0.45.0"


class AgentError(BrowserError):
    pass


def agent_binary():
    override = os.environ.get("SHADOW_AGENT_BROWSER")
    path = Path(override) if override else (
        Path.home() / ".cache/shadow/agent-browser-0.45.0/node_modules/.bin/agent-browser")
    if not path.is_file():
        raise AgentError("agent_browser_unavailable")
    try:
        proc = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        raise AgentError("agent_browser_unavailable") from None
    if AGENT_BROWSER_VERSION not in (proc.stdout + proc.stderr):
        raise AgentError("agent_browser_version")
    return str(path)


class AgentSession:
    """Launch and drive one headless Chrome through agent-browser 0.45.0."""

    def __init__(self, profile, executable):
        self.profile = Path(profile)
        self.executable = str(executable)
        self.session = f"shadow-nav-{os.getpid()}"
        self.binary = agent_binary()
        self.config = str(self.profile / "agent-browser.json")
        self._open = False

    def __enter__(self):
        self.profile.mkdir(parents=True, exist_ok=True)
        Path(self.config).write_text(json.dumps({
            "headed": False, "annotate": False, "hideScrollbars": True}) + "\n")
        return self

    def __exit__(self, *_args):
        try:
            self.command(["close"], timeout=10)
        except AgentError:
            pass
        self._open = False

    def command(self, args, *, launch=False, timeout=20):
        cmd = [self.binary, "--session", self.session, "--config", self.config, "--json"]
        if launch:
            cmd += ["--profile", str(self.profile), "--executable-path", self.executable]
        cmd += [str(arg) for arg in args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AgentError("agent_browser_timeout") from None
        except OSError:
            raise AgentError("agent_browser_unavailable") from None
        try:
            body = json.loads(proc.stdout) if proc.stdout else {}
        except json.JSONDecodeError:
            raise AgentError("agent_browser_failed") from None
        if proc.returncode != 0 or not body.get("success", False):
            message = str(body.get("error") or proc.stderr or "agent_browser_failed")
            if "version" in message.lower():
                raise AgentError("agent_browser_version")
            raise AgentError(message[:240] or "agent_browser_failed")
        return body.get("data")

    def navigate(self, url):
        if not self._open:
            self.command(["open", url], launch=True, timeout=30)
            self._open = True
        else:
            self.command(["navigate", url])
        self.command(["set", "viewport", "1280", "720", "1"])

    def page_url(self):
        data = self.command(["get", "url"])
        if isinstance(data, dict) and isinstance(data.get("url"), str):
            return data["url"]
        raise AgentError("bad_capture")

    def snapshot(self, selector):
        data = self.command(["snapshot", "-s", selector, "-c", "-d", "8"])
        if not isinstance(data, dict):
            raise AgentError("bad_capture")
        return data.get("snapshot") or ""

    def screenshot(self):
        path = self.profile / "frame.png"
        self.command(["screenshot", str(path)])
        try:
            return path.read_bytes()
        except OSError:
            raise AgentError("bad_capture") from None

    def box(self, ref):
        data = self.command(["get", "box", f"@{ref}"])
        found = _box(data)
        if found is None:
            raise AgentError(f"bad_box {str(data)[:180]}")
        return found

    def attr(self, ref, name):
        data = self.command(["get", "attr", f"@{ref}", name])
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("value", "attr", name, "result"):
                if isinstance(data.get(key), str):
                    return data[key]
        raise AgentError(f"bad_attr {str(data)[:180]}")

    def click(self, ref):
        self.command(["click", f"@{ref}"])

    def evaluate(self, expression):
        data = self.command(["eval", expression], timeout=20)
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        raise AgentError("bad_capture")


def _box(data):
    if not isinstance(data, dict):
        return None
    inner = data.get("box") if isinstance(data.get("box"), dict) else data
    if not all(key in inner for key in ("x", "y", "width", "height")):
        return None
    try:
        x, y = float(inner["x"]), float(inner["y"])
        width, height = float(inner["width"]), float(inner["height"])
    except (TypeError, ValueError):
        return None
    return (x, y, x + width, y + height)
