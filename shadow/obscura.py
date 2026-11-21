"""Loopback CDP sessions for Obscura."""

import base64
from contextlib import contextmanager
import json
import os
import socket
import subprocess
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from websocket import create_connection


class ObscuraError(RuntimeError):
    pass


class Browser:
    def __init__(self, connection):
        self.connection = connection
        self.command_id = 0
        self.session_id = None
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
                    raise ObscuraError(f"Obscura refused {method}: {response['error'].get('message')}")
                return response["result"]
        raise ObscuraError(f"Obscura timed out on {method}.")

    def screenshot(self):
        result = self.command("Page.captureScreenshot", format="png")
        return base64.b64decode(result["data"], validate=True)

    def click(self, x, y):
        self.command("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                     button="left", clickCount=1)
        self.command("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                     button="left", clickCount=1)


@contextmanager
def obscura(binary, storage, *, allow_private_network=False):
    with socket.socket() as address:
        address.bind(("127.0.0.1", 0))
        port = address.getsockname()[1]
    environment = {name: os.environ[name] for name in ("HOME", "PATH", "TMPDIR") if name in os.environ}
    with subprocess.Popen(
        [binary, "serve", "--host", "127.0.0.1", "--port", str(port), "--storage-dir", str(storage), "--quiet",
         *(["--allow-private-network"] if allow_private_network else [])],
        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ) as process:
        try:
            opener = build_opener(ProxyHandler({}))
            deadline = monotonic() + 10
            while monotonic() < deadline and process.poll() is None:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                        target = json.load(response)["webSocketDebuggerUrl"]
                    break
                except (URLError, TimeoutError):
                    sleep(0.1)
            else:
                raise ObscuraError("Obscura's local CDP server did not start.")
            if target != f"ws://127.0.0.1:{port}/devtools/browser":
                raise ObscuraError("Obscura returned an unexpected browser endpoint.")
            connection = create_connection(target, timeout=5, suppress_origin=True,
                                           http_no_proxy=["127.0.0.1"])
            try:
                yield Browser(connection)
            finally:
                connection.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
