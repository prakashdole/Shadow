"""Submit a single message in a bound Chromium conversation."""

import json
from time import monotonic, sleep

from shadow.chromium import BrowserError


def _controls(browser, text):
    return browser.command("Runtime.evaluate", returnByValue=True, expression=r"""
        (expected => {
          const bounds = el => el.getBoundingClientRect();
          const visible = el => {
            const r = bounds(el);
            return r.width > 0 && r.height > 0 && r.right > 0 && r.left < innerWidth &&
              r.bottom > 0 && r.top < innerHeight;
          };
          const center = el => {
            const r = bounds(el);
            return [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)];
          };
          const boxes = [...document.querySelectorAll('[contenteditable="true"]')].filter(el =>
            visible(el) && bounds(el).x > 370 && bounds(el).y > 480 && bounds(el).width > 110);
          const composer = boxes.length === 1 ? boxes[0] : null;
          const buttons = [...document.querySelectorAll('button,[role="button"]')].filter(el =>
            visible(el) && bounds(el).x > 370 && bounds(el).y > 480 &&
            !el.disabled && el.getAttribute('aria-disabled') !== 'true' &&
            (el.getAttribute('aria-label') || el.innerText || '').trim() === 'Send');
          const copies = [...document.querySelectorAll('div,span')].filter(el =>
            !el.children.length && visible(el) && bounds(el).x > 370 &&
            !el.closest('[contenteditable="true"]') && el.textContent.trim() === expected).length;
          return {composer: composer ? center(composer) : null,
                  text: composer ? composer.innerText.trim() : null,
                  focused: composer === document.activeElement,
                  send: buttons.length === 1 ? center(buttons[0]) : null, copies};
        })(""" + json.dumps(text) + ")")["result"].get("value") or {}


def deliver(browser, storage, *, thread, decision, conversation, revision, text, authority):
    """Click once, and confirm only after reloading Instagram from the server."""
    if not text or len(text) > 180 or not text.isprintable() or text.strip() != text:
        return {"delivery": "invalid_text", "sends": 0}
    items = storage.outbox_items(conversation)
    if any(item["state"] in {"attempting", "uncertain"} or item["decision_id"] == decision
           for item in items):
        return {"delivery": "prior_attempt", "sends": 0}
    url = f"https://www.instagram.com/direct/t/{thread}/"

    def current():
        if browser.page_url().split("?", 1)[0].rstrip("/") != url.rstrip("/"):
            return {}
        return _controls(browser, text)

    initial = current()
    if not initial.get("composer") or initial.get("text") != "" or initial.get("copies") != 0:
        return {"delivery": "composer_unavailable_or_text_already_present", "sends": 0}
    browser.click(*initial["composer"])
    if not current().get("focused"):
        return {"delivery": "composer_not_focused", "sends": 0}
    browser.command("Input.insertText", text=text)
    ready = current()
    if ready.get("text") != text or not ready.get("send"):
        return {"delivery": "send_control_unverified", "sends": 0}
    item = storage.add_outbox_item(decision_id=decision, item_index=0, conversation_id=conversation,
                                   text=text, authority=authority, revision=revision)
    storage.transition(item, "attempting")
    try:
        browser.click(*ready["send"])
        # The first bubble can be optimistic. Reload before recording delivery.
        deadline = monotonic() + 10
        while monotonic() < deadline:
            if current().get("copies") == 1:
                browser.navigate(url)
                break
            sleep(0.25)
        else:
            raise BrowserError("Submitted bubble did not appear.")
        deadline = monotonic() + 12
        while monotonic() < deadline:
            state = current()
            if state.get("copies") == 1 and state.get("text") == "":
                storage.confirm_send(item, {"text": text, "evidence": {"reloaded_thread": True}})
                return {"delivery": "confirmed", "sends": 1}
            sleep(0.25)
    except (BrowserError, OSError):
        pass
    storage.transition(item, "uncertain")
    return {"delivery": "uncertain", "sends": 0}
