"""Run the bounded Chromium/Decision 1 navigation trial on a synthetic inbox."""

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep

from dotenv import dotenv_values
from PIL import Image, ImageChops, ImageDraw, ImageFont

from websocket import WebSocketException

from shadow.chromium import BrowserError, chromium, find_chromium
from shadow.screen_decision import DecisionError, choose_action


ROOT = Path(__file__).resolve().parents[1]
# shortcut: fixture markers only, replace with real visual target detection before live clicks.
MARKERS = {
    "one": "#f5048d", "two": "#03ba26", "three": "#f57905", "four": "#09bdd3",
    "five": "#d63b26", "six": "#ac0ad6", "seven": "#553afa", "eight": "#99a000",
}
LABELS = {name: str(number) for number, name in enumerate(MARKERS, start=1)}
MIN_CONFIDENCE = 0.80


class TrialError(RuntimeError):
    pass


def fixture_value(browser, expression):
    result = browser.command("Runtime.evaluate", expression=expression, returnByValue=True)
    return result["result"].get("value")


@contextmanager
def local_fixture(filename="browser_inbox.html"):
    html = (ROOT / "tests/fixtures" / filename).read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            thread.join()


def targets(screenshot, allowed):
    image = Image.open(io.BytesIO(screenshot)).convert("RGB")
    positions = {}
    for name in allowed:
        red, green, blue = ImageChops.difference(
            image, Image.new("RGB", image.size, MARKERS[name])
        ).split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        bounds = mask.point(lambda value: 0 if value else 255).getbbox()
        if bounds:
            left, top, right, bottom = bounds
            if 16 <= right - left <= 18 and 16 <= bottom - top <= 18:
                positions[name] = ((left + right) // 2, (top + bottom) // 2)
    return image, positions


def annotated(image, positions):
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=17)
    for name, (x, y) in positions.items():
        draw.text((x, y), LABELS[name], font=font, fill="black", stroke_width=1,
                  stroke_fill="white", anchor="mm")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def ensure(condition, label):
    if not condition:
        raise TrialError(label)


def wait_for(browser, expression, label):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        if fixture_value(browser, expression):
            return
        sleep(0.1)
    raise TrialError(label)


def trial(browser, url, api_key, evidence):
    browser.command("Page.navigate", url=url)
    wait_for(browser, "window.fixtureState === 'inbox'", "Fixture inbox did not load.")

    def step(task, allowed, expected, *, change_before_click=False, close_up=False):
        screenshot = browser.screenshot()
        image, positions = targets(screenshot, allowed)
        if evidence and set(positions) != set(allowed):
            (evidence / "missing-target.png").write_bytes(screenshot)
        ensure(set(positions) == set(allowed), "A fixture target was missing or ambiguous.")
        labeled = annotated(image, positions)
        if evidence:
            (evidence / f"{browser.command_id:03}-{expected}.png").write_bytes(labeled)
        start = monotonic()
        if api_key:
            choices = {name: f"Select the visible control marked {LABELS[name]}."
                       for name in positions}
            choices["hold"] = "Hold because the intended control cannot be identified."
            choice, confidence = choose_action(api_key, labeled, task, choices)
            print(f"{task} → {choice} ({confidence:.3f}, {monotonic() - start:.2f}s)", flush=True)
            if close_up and confidence < MIN_CONFIDENCE:
                xs, ys = zip(*positions.values())
                bounds = (max(0, min(xs) - 24), max(0, min(ys) - 30),
                          min(image.width, max(xs) + 230), min(image.height, max(ys) + 90))
                buffer = io.BytesIO()
                image.crop(bounds).save(buffer, format="PNG")
                choice, confidence = choose_action(api_key, buffer.getvalue(), task, choices)
                print(f"  Close-up → {choice} ({confidence:.3f})", flush=True)
            ensure(confidence >= MIN_CONFIDENCE, "Decision confidence was below the trial threshold.")
        else:
            choice = expected
        ensure(choice == expected, "The model picked the wrong action for the fixture.")
        if change_before_click:
            fixture_value(browser, "reorder()")
        if choice == "hold":
            return
        current = browser.screenshot()
        if current != screenshot:
            ensure(change_before_click, "The screen changed before click. No action was sent.")
            ensure(fixture_value(browser, "window.fixtureState") == "Alice", "A stale action changed chats.")
            print("Changed screenshot: stale click blocked.", flush=True)
            return
        ensure(not change_before_click, "The layout update was not detected.")
        browser.click(*positions[choice])

    step("Open Alice's unread chat.", ("one", "two"), "one")
    wait_for(browser, "window.fixtureState === 'Alice'", "Alice did not open.")
    screenshot = browser.screenshot()
    if evidence:
        (evidence / "alice-open.png").write_bytes(screenshot)
    browser.command("Input.dispatchMouseEvent", type="mouseWheel", x=570, y=190, deltaX=0, deltaY=300)
    wait_for(browser, "document.getElementById('history').scrollTop > 0", "Message history did not scroll.")
    # A fixture-only scroll reset keeps the reel target visible for the next step.
    fixture_value(browser, "document.getElementById('history').scrollTop = 0")
    step("Open the shared reel in Alice's chat.", ("one", "two", "three"), "three")
    wait_for(browser, "window.fixtureState === 'reel'", "Reel overlay did not open.")
    first = browser.screenshot()
    wait_for(browser, "document.getElementById('frame').textContent.endsWith('frame 3')",
             "The synthetic reel frames did not advance.")
    last = browser.screenshot()
    ensure(first != last, "The screenshot did not reflect the synthetic frame change.")
    if evidence:
        (evidence / "reel-frame-3.png").write_bytes(last)
    step("Close the reel overlay to return to the chat.", ("four",), "four")
    wait_for(browser, "window.fixtureState === 'Alice'", "Closing the overlay lost the original chat.")
    step("Select Reply to reel to quote that message in Alice's chat.", ("three", "five", "six"),
         "five", close_up=True)
    wait_for(browser, "document.getElementById('quote')?.textContent === 'Replying to shared reel'",
             "The reel quote preview did not appear.")
    step("Select React to reel to open the reaction picker.", ("three", "five", "six"),
         "six", close_up=True)
    step("Choose the Laugh reaction from the open picker.", ("seven", "eight"), "seven",
         close_up=True)
    wait_for(browser, "window.fixtureReaction?.startsWith('Laugh')",
             "The fixture did not record the Laugh reaction.")
    step("Switch to Bob's conversation.", ("one", "two"), "two", change_before_click=True)
    step("Switch to Bob's conversation now.", ("one", "two"), "two")
    wait_for(browser, "window.fixtureState === 'Bob'", "Bob did not open after reordering.")
    step("Open Carol's chat only if a visible row is labeled Carol.", ("one", "two"), "hold")
    ensure(fixture_value(browser, "window.fixtureState") == "Bob", "A hold changed the active chat.")
    width, height = Image.open(io.BytesIO(browser.screenshot())).size
    ensure(fixture_value(browser, f"innerWidth === {width} && innerHeight === {height}"),
           "Screenshot coordinates do not match this viewport.")
    browser.click(480, 373)
    browser.command("Input.insertText", text="draft")
    ensure(fixture_value(browser, "document.getElementById('composer').value") == "draft",
           "Keyboard input did not reach the fixture composer.")
    if evidence:
        (evidence / "bob-draft.png").write_bytes(browser.screenshot())
    fixture_value(browser, "document.activeElement.blur()")
    _, before_resize = targets(browser.screenshot(), ("one", "two"))
    browser.command("Emulation.setDeviceMetricsOverride", width=800, height=600,
                    deviceScaleFactor=1, mobile=False)
    resized, fresh_targets = targets(browser.screenshot(), ("one", "two"))
    ensure(resized.size == (800, 600) and before_resize != fresh_targets,
           "The viewport resize did not relocate the fixture targets.")
    step("Open Alice's chat in the resized window.", ("one", "two"), "one", close_up=True)
    wait_for(browser, "window.fixtureState === 'Alice'", "Alice did not open after resizing.")
    if api_key:
        ready = browser.screenshot()
        fixture_value(browser, "pane.innerHTML = '<h2>Alice</h2><p>Loading messages…</p>'")
        loading = browser.screenshot()
        choices = {"ready": "The message history and message composer are visible.",
                   "hold": "The chat header is visible but the messages or composer are still loading."}
        for frame, expected in ((ready, "ready"), (loading, "hold")):
            choice, confidence = choose_action(
                api_key, frame, "Is this chat pane ready for input? A header alone is insufficient.", choices
            )
            print(f"Chat readiness → {choice} ({confidence:.3f})", flush=True)
            ensure(choice == expected and confidence >= MIN_CONFIDENCE,
                   "Decision 1 misclassified chat readiness or was uncertain.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Path to Chromium or Chrome.")
    parser.add_argument("--with-model", action="store_true", help="Test the live API with synthetic screenshots.")
    parser.add_argument("--screenshots", type=Path, help="A new directory for synthetic screenshots.")
    args = parser.parse_args()
    api_key = ""
    if args.with_model:
        api_key = (os.getenv("GEMINI_API_KEY") or dotenv_values(ROOT / ".env").get("GEMINI_API_KEY") or "").strip()
        if not api_key:
            parser.error("GEMINI_API_KEY is required for --with-model.")
    if args.screenshots:
        args.screenshots.mkdir(mode=0o700)
    try:
        binary = args.browser or find_chromium()
        with TemporaryDirectory(prefix="shadow-chromium-") as storage, local_fixture() as url:
            with chromium(binary, storage) as browser:
                trial(browser, url, api_key, args.screenshots)
            with chromium(binary, storage) as browser:
                browser.command("Page.navigate", url=url)
                wait_for(browser, "document.cookie.includes('shadow_fixture=1')",
                         "The fixture cookie did not survive browser restart.")
                ensure(fixture_value(browser, "localStorage.getItem('shadow_fixture')") == "visited",
                       "Fixture local storage did not survive browser restart.")
    except (DecisionError, BrowserError, OSError, TrialError, WebSocketException) as error:
        parser.exit(status=1, message=f"Trial failed: {error}\n")
    print("PASS: screenshots, coordinate input, scroll, reel overlay, quote, reaction picker, "
          "stale targets, chat switch, draft input, viewport resize, and storage restart.")


if __name__ == "__main__":
    main()
