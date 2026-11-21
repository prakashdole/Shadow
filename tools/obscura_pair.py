"""Sign in to Instagram in a dedicated Chrome window and test that session in Obscura."""

import argparse
from contextlib import closing
import fcntl
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.parse import urlsplit

from websocket import WebSocketException, create_connection

from shadow.obscura import Browser, ObscuraError, obscura
from tools.obscura_login import INSTAGRAM


INBOX = INSTAGRAM + "/direct/inbox/"
PROFILE = Path.home() / "Library/Application Support/Shadow/obscura-instagram"
COOKIE_FIELDS = ("name", "value", "path", "secure", "httpOnly", "sameSite")


def instagram_cookies(chrome):
    result = []
    for cookie in chrome.command("Network.getCookies", urls=[INSTAGRAM + "/", INBOX])["cookies"]:
        domain = cookie["domain"]
        if domain not in (".instagram.com", "instagram.com", ".www.instagram.com", "www.instagram.com"):
            continue
        if cookie.get("partitionKey") or cookie.get("partitionKeyOpaque"):
            continue
        params = {key: cookie[key] for key in COOKIE_FIELDS if key in cookie}
        if domain.startswith("."):
            params["domain"] = domain
        else:
            params["url"] = "https://" + domain + cookie["path"]
        if not cookie["session"] and cookie["expires"] > 0:
            params["expires"] = int(cookie["expires"])
        result.append(params)
    return result


def chrome_endpoint(process, profile):
    for _ in range(80):
        try:
            port, path = (profile / "DevToolsActivePort").read_text().splitlines()
            if port.isdecimal() and path.startswith("/devtools/browser/"):
                return f"ws://127.0.0.1:{int(port)}{path}"
        except (FileNotFoundError, ValueError):
            pass
        if process.poll() is not None:
            break
        sleep(0.25)
    raise ObscuraError("The dedicated Chrome window did not start.")


def import_session(binary, profile, cookies):
    with obscura(binary, profile) as browser:
        browser.command("Network.setCookies", cookies=cookies)
        browser.connection.settimeout(25)
        browser.command("Page.navigate", url=INBOX, waitUntil="domcontentloaded")
        state = browser.command("Runtime.evaluate", returnByValue=True, expression=(
            "location.origin === 'https://www.instagram.com' && "
            "location.pathname.startsWith('/direct/') && "
            "!document.querySelector('input[type=password]')"
        ))
        if not state["result"].get("value"):
            raise ObscuraError("Instagram did not open the inbox route in Obscura after the handoff.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", required=True, type=Path, help="Path to a Chrome executable")
    parser.add_argument("--browser", default=str(Path.home() / ".local/opt/obscura-0.2.7/obscura"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        with TemporaryDirectory(prefix="shadow-instagram-chrome-") as directory:
            profile = Path(directory)
            with subprocess.Popen([
                args.chrome, f"--user-data-dir={profile}", "--no-first-run",
                "--no-default-browser-check", "--use-mock-keychain",
                "--remote-debugging-port=0", "--new-window", INBOX,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
                try:
                    endpoint = chrome_endpoint(process, profile)
                    with closing(create_connection(endpoint, suppress_origin=True, timeout=5,
                                                   http_no_proxy=["127.0.0.1"])) as connection:
                        chrome = Browser(connection)
                        for target in chrome.command("Target.getTargets")["targetInfos"]:
                            if urlsplit(target["url"]).hostname == "www.instagram.com":
                                chrome.command("Target.activateTarget", targetId=target["targetId"])
                                break
                        print("Sign in to Instagram in the dedicated Chrome window. Waiting up to five minutes.", flush=True)
                        deadline = monotonic() + 300
                        while monotonic() < deadline and process.poll() is None:
                            cookies = instagram_cookies(chrome)
                            if any(cookie["name"] == "sessionid" and cookie["value"] and cookie["secure"]
                                   for cookie in cookies):
                                break
                            sleep(2)
                        else:
                            raise ObscuraError("No signed-in Instagram session was seen in Chrome.")
                    print("Chrome established an Instagram session. Checking it in Obscura.", flush=True)
                    with TemporaryDirectory(prefix="shadow-instagram-obscura-") as candidate:
                        import_session(args.browser, candidate, cookies)
                    PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with (PROFILE / ".lock").open("a") as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            raise ObscuraError("Close the old Obscura viewer before saving the session.") from None
                        import_session(args.browser, PROFILE, cookies)
                    print("Obscura opened the inbox route. The session is saved in its profile.", flush=True)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
    except KeyboardInterrupt:
        parser.exit(1, "\nSign-in cancelled.\n")
    except (ObscuraError, OSError, WebSocketException, KeyError):
        # CDP errors may contain cookie or account data.
        parser.exit(1, "Pairing stopped. Check the Chrome sign-in, Obscura inbox, or profile availability.\n")


if __name__ == "__main__":
    main()
