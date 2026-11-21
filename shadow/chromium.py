"""Start a private Chromium profile and drive its page over loopback CDP."""

import base64
from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import monotonic, sleep

from websocket import WebSocketException, create_connection


WIDTH, HEIGHT = 1280, 720


class BrowserError(RuntimeError):
    pass


class Browser:
    def __init__(self, connection):
        self.connection = connection
        self.command_id = 0
        self.session_id = None
        targets = self.command("Target.getTargets")["targetInfos"]
        target = next((target for target in targets
                       if target["type"] == "page" and target["url"] == "about:blank"), None)
        if target is None:
            target = self.command("Target.createTarget", url="about:blank")
        self.session_id = self.command(
            "Target.attachToTarget", targetId=target["targetId"], flatten=True
        )["sessionId"]

    def command(self, method, **params):
        self.command_id += 1
        request = {"id": self.command_id, "method": method, "params": params}
        if self.session_id:
            request["sessionId"] = self.session_id
        self.connection.send(json.dumps(request))
        deadline = monotonic() + 10
        while monotonic() < deadline:
            response = json.loads(self.connection.recv())
            if response.get("id") == self.command_id:
                if "error" in response:
                    raise BrowserError(f"Chromium refused {method}.")
                return response["result"]
        raise BrowserError(f"Chromium timed out on {method}.")

    def screenshot(self):
        result = self.command("Page.captureScreenshot", format="png")
        return base64.b64decode(result["data"], validate=True)

    def click(self, x, y):
        self.command("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                     button="left", clickCount=1)
        self.command("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                     button="left", clickCount=1)


def find_chromium():
    if binary := os.environ.get("CHROMIUM_BINARY"):
        return binary
    for name in ("chromium", "chromium-browser", "google-chrome"):
        if binary := shutil.which(name):
            return binary
    if sys.platform == "darwin":
        installed = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if installed.is_file():
            return str(installed)
        cache = Path.home() / ".cache/puppeteer/chrome"
        cached = sorted(cache.glob("*/chrome-mac-*/*.app/Contents/MacOS/Google Chrome for Testing"))
        if cached:
            return str(cached[-1])
    raise BrowserError("Chromium not found. Pass --browser or set CHROMIUM_BINARY.")


def profile_path():
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Shadow/chromium-instagram"
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return data / "shadow/chromium-instagram"


def chrome_endpoint(process, profile):
    deadline = monotonic() + 15
    while monotonic() < deadline and process.poll() is None:
        try:
            port, path = (profile / "DevToolsActivePort").read_text().splitlines()
            if port.isdecimal() and 0 < int(port) < 65536 and re.fullmatch(r"/devtools/browser/[\da-f-]+", path):
                return f"ws://127.0.0.1:{int(port)}{path}"
        except (FileNotFoundError, ValueError):
            pass
        sleep(0.1)
    raise BrowserError("Chromium's local DevTools endpoint did not start.")


@contextmanager
def chromium(binary, profile, *, headed=False):
    profile = Path(profile).expanduser().resolve()
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile.chmod(0o700)
    with (profile / ".shadow.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BrowserError("This Chromium profile is already open.") from None
        # Chrome leaves this file behind after exiting.
        (profile / "DevToolsActivePort").unlink(missing_ok=True)
        environment = {name: os.environ[name] for name in (
            "HOME", "PATH", "TMPDIR", "USER", "LANG", "LC_ALL", "XDG_RUNTIME_DIR",
            "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
        ) if name in os.environ}
        flags = ["--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0",
                 f"--user-data-dir={profile}"]
        if not headed:
            flags.append("--headless")
        if sys.platform == "darwin" and Path(binary).name == "Google Chrome for Testing":
            flags.append("--use-mock-keychain")
        with subprocess.Popen([binary, *flags, "about:blank"], env=environment,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
            try:
                endpoint = chrome_endpoint(process, profile)
                with closing(create_connection(endpoint, timeout=5, suppress_origin=True,
                                               http_no_proxy=["127.0.0.1"])) as connection:
                    browser = Browser(connection)
                    try:
                        browser.command("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT,
                                        deviceScaleFactor=1, mobile=False)
                        yield browser
                    finally:
                        browser.session_id = None
                        try:
                            browser.command("Browser.close")
                        except (BrowserError, OSError, WebSocketException):
                            pass
            finally:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
