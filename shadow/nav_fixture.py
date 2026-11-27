"""Synthetic inbox for the navigation pilot. Expected answers stay in the harness."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Shadow nav fixture</title>
<style>
  html, body { margin: 0; width: 1280px; height: 720px; background: #f6f8fa; overflow: hidden; }
  header { height: 56px; background: #23324d; overflow: hidden; }
  header p { margin: 0; padding: 0 0 0 16px; line-height: 56px; color: #d5ddee; font: 16px sans-serif; }
  #threads { position: absolute; top: 56px; left: 0; width: 470px; bottom: 0; border-right: 1px solid #9aa6b5; }
  button, a.row { font: 20px sans-serif; color: #eef2f8; background: #182134; border: 0; text-align: left; text-decoration: none; }
  .row { position: relative; display: block; height: 52px; margin: 14px 16px; width: 438px; box-sizing: border-box; }
  .dot { position: absolute; width: 7px; height: 7px; right: 16px; top: 22px; background: #4a5df9; }
  .blank { display: block; width: 160px; height: 18px; margin: 16px 0 0 12px; background: #c5ced8; }
  #pane { position: absolute; top: 78px; left: 500px; width: 720px; }
  .band { height: 44px; margin: 0 0 18px; background: #182134; }
  .card { display: inline-block; width: 200px; height: 180px; margin: 8px 40px 0 0; vertical-align: top; }
  .msg { width: 460px; margin: 0 0 18px; }
  .msg.out { margin-left: 240px; }
  .line, .caption { margin: 0 0 8px; font: 18px sans-serif; color: #182134; }
  .card.reel { background: #182134; color: #f4f6fb; }
  .card.photo { background: #c9a227; color: #182134; }
  #player { position: fixed; inset: 0; background: #20283d; z-index: 5; }
  #player[hidden], #preview[hidden] { display: none !important; }
  #player p { position: absolute; left: 80px; top: 120px; margin: 0; color: #f4f6fb; font: 28px sans-serif; }
  #player button { position: absolute; left: 80px; top: 400px; width: 180px; height: 48px; background: #f4f6fb; color: #182134; }
  #preview { position: fixed; left: 460px; top: 180px; width: 420px; height: 280px; background: #f7f4ea; z-index: 4; border: 2px solid #182134; }
  #preview button { position: absolute; left: 24px; top: 24px; width: 200px; height: 48px; background: #182134; color: #f4f6fb; }
</style>
<header><p id="banner"></p></header>
<aside id="threads" aria-label="Conversations"></aside>
<article id="pane"></article>
<div id="player" hidden>
  <p id="player-label"></p>
  <button type="button" data-shadow="close" aria-label="Close reel">Close</button>
</div>
<div id="preview" hidden>
  <button type="button" data-shadow="close-photo" aria-label="Close photo">Close photo</button>
</div>
<script>
  window.opened = [];
  window.fixtureState = "inbox";
  window.currentThread = "";
  document.getElementById("player").querySelector("button").onclick = () => {
    window.opened.push("close");
    document.getElementById("player").hidden = true;
    document.getElementById("player-label").textContent = "";
    window.fixtureState = window.currentThread || "";
  };
  document.getElementById("preview").querySelector("button").onclick = () => {
    window.opened.push("close-photo");
    document.getElementById("preview").hidden = true;
    window.fixtureState = window.currentThread || "";
  };
  function openCard(spec) {
    window.opened.push(spec.id);
    if (spec.kind === "photo" || spec.opens === false) {
      window.fixtureState = "photo";
      document.getElementById("preview").hidden = false;
      return;
    }
    window.fixtureState = "reel";
    const title = spec.aria || "Shared reel";
    document.getElementById("player-label").textContent = title;
    document.getElementById("player").querySelector("button").setAttribute("aria-label", "Close reel " + title);
    document.getElementById("player").hidden = false;
  }
  function renderMessages(pane) {
    pane.replaceChildren();
    (window.shadowMessages || []).forEach((spec) => {
      const wrap = document.createElement("div");
      wrap.className = "msg " + (spec.side === "out" ? "out" : "in");
      wrap.setAttribute("role", "group");
      wrap.setAttribute("aria-label", spec.side === "out" ? "Outgoing message" : "Incoming message");
      if (spec.kind === "text" || (spec.kind === "link" && spec.text)) {
        const line = document.createElement("p");
        line.className = "line";
        line.textContent = spec.text || "";
        wrap.appendChild(line);
      }
      if (spec.kind === "reel" || spec.kind === "photo") {
        const card = document.createElement("button");
        card.type = "button";
        card.className = "card " + spec.kind;
        card.dataset.shadow = spec.id;
        card.setAttribute("aria-label", spec.aria || spec.kind);
        card.textContent = spec.kind === "reel" ? "REEL" : "Photo";
        card.onclick = () => openCard(spec);
        wrap.appendChild(card);
        if (spec.caption) {
          const cap = document.createElement("p");
          cap.className = "caption";
          cap.textContent = spec.caption;
          wrap.appendChild(cap);
        }
      } else if (spec.kind === "link" || spec.kind === "attachment") {
        const card = document.createElement("button");
        card.type = "button";
        card.className = "card";
        card.dataset.shadow = spec.id || spec.kind;
        card.setAttribute("aria-label", spec.aria || spec.kind);
        card.textContent = spec.aria || spec.kind;
        card.onclick = () => { window.opened.push(card.dataset.shadow); };
        wrap.appendChild(card);
      }
      pane.appendChild(wrap);
    });
  }
  function showThread(id) {
    const apply = () => {
      window.fixtureState = String(id);
      window.currentThread = String(id);
      history.pushState({}, "", "/direct/t/" + id + "/");
      const pane = document.getElementById("pane");
      pane.replaceChildren();
      document.getElementById("player").hidden = true;
      document.getElementById("preview").hidden = true;
      if (window.shadowMessages) {
        renderMessages(pane);
        return;
      }
      for (let index = 0; index < 3; index += 1) {
        const band = document.createElement("div");
        band.className = "band";
        pane.appendChild(band);
      }
      const cards = window.shadowCards || [{id: "reel", aria: "Shared reel", opens: true}];
      cards.forEach((spec) => {
        const card = document.createElement("button");
        card.className = "card";
        card.dataset.shadow = spec.id;
        card.setAttribute("aria-label", spec.aria);
        card.onclick = () => openCard(Object.assign({kind: spec.opens === false ? "photo" : "reel"}, spec));
        pane.appendChild(card);
      });
    };
    if (window.shadowDelay) window.setTimeout(apply, window.shadowDelay);
    else apply();
  }
  function shadowRender(spec) {
    window.opened = [];
    document.documentElement.dataset.gen = String(spec.gen || 0);
    document.documentElement.dataset.account = spec.account || "pilot";
    window.currentThread = spec.thread || "";
    window.shadowCards = spec.cards || null;
    window.shadowMessages = spec.messages || null;
    window.shadowDelay = spec.delay || 0;
    window.fixtureState = spec.state || "inbox";
    history.pushState({}, "", spec.path || "/direct/inbox/");
    const banner = [spec.account_label, spec.order_label].filter(Boolean).join(" · ");
    document.getElementById("banner").textContent = banner;
    const threads = document.getElementById("threads");
    threads.setAttribute("aria-label", spec.order_label || "Conversations");
    const pane = document.getElementById("pane");
    threads.replaceChildren();
    pane.replaceChildren();
    document.getElementById("player").hidden = true;
    document.getElementById("player-label").textContent = "";
    document.getElementById("preview").hidden = true;
    (spec.rows || []).forEach((row) => {
      const linked = Boolean(spec.links || row.time);
      const button = document.createElement(linked ? "a" : "button");
      button.className = "row";
      button.dataset.shadow = String(row.id);
      if (linked) {
        button.href = "/direct/t/" + row.id + "/";
        button.addEventListener("click", (event) => event.preventDefault());
      }
      const visible = [row.name, row.time].filter(Boolean).join(" ");
      button.setAttribute("aria-label", row.aria || visible || "Conversation");
      button.textContent = visible;
      if (!row.name && !row.time) {
        const mark = document.createElement("i");
        mark.className = "blank";
        mark.setAttribute("aria-hidden", "true");
        button.appendChild(mark);
      }
      if (row.dot) {
        const dot = document.createElement("i");
        dot.className = "dot";
        dot.setAttribute("aria-hidden", "true");
        button.appendChild(dot);
      }
      button.addEventListener("click", () => {
        window.opened.push(button.dataset.shadow);
        showThread(button.dataset.shadow);
      });
      threads.appendChild(button);
    });
    if (spec.screen === "thread" || spec.screen === "player") showThread(spec.thread);
    if (spec.screen === "loading") {
      const title = document.createElement("h2");
      title.textContent = "Chat";
      pane.appendChild(title);
      window.fixtureState = "loading";
    }
    if (spec.screen === "player") {
      window.fixtureState = "reel";
      document.getElementById("player-label").textContent = "Shared reel";
      document.getElementById("player").hidden = false;
      window.opened = [];
    }
    return true;
  }
  function shadowToken() {
    const nodes = [...document.querySelectorAll("[data-shadow]")].filter((el) => !el.closest("[hidden]")).map((el) => {
      const box = el.getBoundingClientRect();
      return {id: el.dataset.shadow, x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.width), h: Math.round(box.height)};
    });
    return JSON.stringify({href: location.pathname, gen: document.documentElement.dataset.gen || "0", nodes});
  }
  function shadowOutcome() {
    const player = document.getElementById("player");
    return JSON.stringify({opened: window.opened.slice(), state: window.fixtureState, href: location.pathname,
      player: !player.hidden, gen: document.documentElement.dataset.gen || "0"});
  }
  function shadowProbe() {
    const dots = [...document.querySelectorAll(".dot")].map((el) => {
      const box = el.getBoundingClientRect();
      return {x: Math.round(box.x + box.width / 2), y: Math.round(box.y + box.height / 2)};
    });
    return JSON.stringify({dots});
  }
  function shadowMutate(kind) {
    document.documentElement.dataset.gen = String(Number(document.documentElement.dataset.gen || "0") + 1);
    const box = document.getElementById("threads");
    if (kind === "reorder" && box.children.length >= 2) box.insertBefore(box.children[1], box.children[0]);
    else if (kind === "replace" && box.children[0]) {
      box.children[0].dataset.shadow = "9999";
      if (box.children[0].tagName === "A") box.children[0].setAttribute("href", "/direct/t/9999/");
    }
    return true;
  }
  function shadowLocate(id) {
    const nodes = [...document.querySelectorAll("[data-shadow]")].filter((el) => el.dataset.shadow === id && !el.closest("[hidden]"));
    if (nodes.length !== 1) return null;
    const el = nodes[0];
    const box = el.getBoundingClientRect();
    if (box.width < 8 || box.height < 8 || box.bottom < 0 || box.right < 0 || box.top > innerHeight || box.left > innerWidth) return null;
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    if (!hit || (hit !== el && !el.contains(hit))) return null;
    return {x: box.x, y: box.y, w: box.width, h: box.height};
  }
</script>
"""


def render_call(spec: dict) -> str:
    return "window.shadowRender(" + json.dumps(spec) + ")"


@contextmanager
def serve():
    page = PAGE.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()
