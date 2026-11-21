"""Open a local viewer for a persistent Obscura Instagram session."""

import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from threading import Lock, Thread

from websocket import WebSocketException

from shadow.obscura import ObscuraError, obscura


WIDTH, HEIGHT = 1280, 720
INSTAGRAM = "https://www.instagram.com"
PAGES = {"inbox": "/direct/inbox/", "reels": "/reels/"}
VIEWER = b"""<!doctype html>
<meta charset="utf-8">
<title>Shadow Obscura viewer</title>
<style>
body { margin: 16px; font: 16px system-ui; background: #172033; color: #f1f4f8; }
img { display: block; max-width: 100%; height: auto; background: white; cursor: crosshair; }
</style>
<p><button data-page="inbox">Inbox</button> <button data-page="reels">Reels</button>
<button id="finish">Save session and close viewer</button></p>
<p role="status" id="status">Connecting to Obscura...</p>
<img id="screen" width="1280" height="720" tabindex="0" alt="Interactive view of Instagram">
<script>
const screen = document.querySelector('#screen');
const statusLine = document.querySelector('#status');
let pending = Promise.resolve();
let frame;
let done = false;
async function refresh() {
    await pending;
    if (done) return;
    try {
        const response = await fetch('frame');
        if (!response.ok) throw Error();
        const next = URL.createObjectURL(await response.blob());
        screen.src = next;
        if (frame) URL.revokeObjectURL(frame);
        frame = next;
        statusLine.textContent = 'Live Obscura viewport';
    } catch {
        statusLine.textContent = 'Obscura is unavailable. Restart the viewer if the connection closed.';
    }
    setTimeout(refresh, 750);
}
function input(data) {
    pending = pending.then(async () => {
        const response = await fetch('input', {method: 'POST',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
        if (!response.ok) throw Error();
    }).catch(() => { statusLine.textContent = 'The browser did not accept that input.'; });
}
for (const button of document.querySelectorAll('[data-page]')) {
    button.addEventListener('click', () => input({action: 'navigate', page: button.dataset.page}));
}
screen.addEventListener('click', event => {
    if (!frame) return;
    const rect = screen.getBoundingClientRect();
    input({action: 'click', x: Math.floor((event.clientX - rect.left) * screen.naturalWidth / rect.width),
           y: Math.floor((event.clientY - rect.top) * screen.naturalHeight / rect.height)});
});
screen.addEventListener('keydown', event => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key.length === 1) input({action: 'text', value: event.key});
    else if (['Backspace', 'Tab', 'Enter', 'Escape'].includes(event.key))
        input({action: 'key', value: event.key});
    else return;
    event.preventDefault();
});
screen.addEventListener('paste', event => {
    event.preventDefault();
    input({action: 'text', value: event.clipboardData.getData('text')});
});
document.querySelector('#finish').addEventListener('click', async event => {
    await pending;
    const response = await fetch('finish', {method: 'POST'});
    if (response.ok) {
        done = true;
        event.target.disabled = true;
        screen.hidden = true;
        statusLine.textContent = 'Viewer closed. You can close this tab.';
    }
});
refresh();
</script>
"""


def navigate(browser, site_origin, page):
    # Obscura can wait for Instagram's redirect before replying to Page.navigate.
    timeout = browser.connection.gettimeout()
    browser.connection.settimeout(20)
    try:
        browser.command("Page.navigate", url=site_origin + PAGES[page], waitUntil="domcontentloaded")
    finally:
        browser.connection.settimeout(timeout)


def operate(browser, event):
    action = event.get("action")
    if action == "click":
        x, y = event.get("x"), event.get("y")
        if type(x) is int and type(y) is int and 0 <= x < WIDTH and 0 <= y < HEIGHT:
            browser.click(x, y)
            return
    elif action == "text":
        text = event.get("value")
        if isinstance(text, str) and 0 < len(text) <= 512 and text.isprintable():
            browser.command("Input.dispatchKeyEvent", type="char", text=text, key=text[0])
            return
    elif action == "key":
        key = event.get("value")
        keys = {"Backspace": 8, "Tab": 9, "Enter": 13, "Escape": 27}
        if isinstance(key, str) and key in keys:
            for kind in ("keyDown", "keyUp"):
                browser.command("Input.dispatchKeyEvent", type=kind, key=key, code=key,
                                windowsVirtualKeyCode=keys[key])
            return
    raise ValueError("Invalid input.")


def viewer(browser, site_origin):
    prefix = "/" + secrets.token_urlsafe(24) + "/"
    browser_lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *_):
            pass

        def reply(self, status, content_type="text/plain", payload=b""):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; img-src blob:; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.headers.get("Host") != host:
                self.reply(403)
            elif self.path == prefix:
                self.reply(200, "text/html; charset=utf-8", VIEWER)
            elif self.path == prefix + "frame":
                try:
                    with browser_lock:
                        image = browser.screenshot()
                except (ObscuraError, WebSocketException, OSError):
                    self.reply(502)
                else:
                    self.reply(200, "image/png", image)
            else:
                self.reply(404)

        def do_POST(self):
            if (self.path not in (prefix + "input", prefix + "finish") or self.headers.get("Host") != host
                    or self.headers.get("Origin") != "http://" + host):
                self.reply(403)
                return
            if self.path == prefix + "finish":
                self.reply(204)
                Thread(target=server.shutdown, daemon=True).start()
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError("Invalid request size.")
                event = json.loads(self.rfile.read(size))
                if not isinstance(event, dict):
                    raise ValueError("Invalid input.")
                with browser_lock:
                    if event.get("action") == "navigate":
                        destination = event.get("page")
                        if not isinstance(destination, str) or destination not in PAGES:
                            raise ValueError("Unknown Instagram page.")
                        navigate(browser, site_origin, destination)
                        allowed = True
                    else:
                        page = browser.command("Runtime.evaluate", expression="location.origin", returnByValue=True)
                        allowed = page["result"].get("value") == site_origin
                        if allowed:
                            operate(browser, event)
            except (ValueError, TypeError):
                self.reply(400)
            except (ObscuraError, WebSocketException, OSError):
                self.reply(502)
            else:
                self.reply(204 if allowed else 403)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.daemon_threads = False  # Let in-flight handlers finish before closing the browser.
        host = f"127.0.0.1:{server.server_port}"
        print(f"http://{host}{prefix}", flush=True)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default=str(Path.home() / ".local/opt/obscura-0.2.7/obscura"))
    args = parser.parse_args()
    profile = Path.home() / "Library/Application Support/Shadow/obscura-instagram"
    os.umask(0o077)
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile.chmod(0o700)
    try:
        with (profile / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(1, "This Obscura profile is already open. Stop that viewer first.\n")
            with obscura(args.browser, profile) as browser:
                browser.command("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT,
                                deviceScaleFactor=1, mobile=False)
                navigate(browser, INSTAGRAM, "inbox")
                print("Open this local viewer to inspect Obscura's Instagram session.", flush=True)
                viewer(browser, INSTAGRAM)
    except KeyboardInterrupt:
        print("\nObscura closed. The profile remains available for next time.")
    except (ObscuraError, WebSocketException, OSError):
        parser.exit(1, "The Obscura viewer could not start or lost its browser connection.\n")


if __name__ == "__main__":
    main()
