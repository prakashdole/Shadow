"""Memory — per-person profile.json + chat_log.jsonl + context.md.

On-disk layout is unchanged from the old tree so the 61 imported chat
histories in data/people/ keep working. Do not rename these files.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from shadow.config import PEOPLE_DIR


@dataclass
class Msg:
    sender: str  # "me" | "them"
    content: str


def _safe(name: str) -> str:
    """Directory name for a person. Must match the old _safe() exactly —
    data/people/ was written by it (e.g. "Aditya_shirke_7")."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_")


def _dir(name: str) -> Path:
    d = PEOPLE_DIR / _safe(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve(name: str) -> str | None:
    """Find an existing person folder by fuzzy name. Instagram shows display
    names that don't always match the imported folder, so try exact, then
    case-insensitive, then substring."""
    if not PEOPLE_DIR.exists():
        return None
    dirs = [d.name for d in PEOPLE_DIR.iterdir() if d.is_dir()]
    target = _safe(name)
    if target in dirs:
        return target
    low = target.lower()
    for d in dirs:
        if d.lower() == low:
            return d
    for d in dirs:
        if low in d.lower() or d.lower() in low:
            return d
    return None


def profile(name: str) -> dict:
    """Load profile.json + context.md. Returns defaults for an unknown person."""
    folder = resolve(name)
    d = PEOPLE_DIR / folder if folder else _dir(name)
    p = d / "profile.json"
    out = {"display_name": name, "relationship": "", "closeness": 5}
    if p.exists():
        try:
            out.update(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass  # corrupt profile shouldn't stop a reply
    ctx = d / "context.md"
    out["context"] = ctx.read_text(encoding="utf-8") if ctx.exists() else ""
    return out


# 33% of the imported history is content-free: bare media placeholders,
# reaction notices, attachment notices. Feeding it to the model gives it
# nothing to imitate and it parrots whatever real text is nearest.
_JUNK = re.compile(
    r"^(?:"
    r"\[(?:reel|image|voice\s?note|sticker|gif|video|link|photo|audio)\]"
    r"|Reacted .{0,6} to your message"
    r"|Liked a message"
    r"|(?:You )?sent an attachment\.?"
    r"|This message was unsent\.?"
    r"|You (?:unsent|reacted)\b.*"
    r")\s*$",
    re.I,
)


def is_junk(content: str) -> bool:
    """True if a stored message carries no imitable text."""
    return not content.strip() or bool(_JUNK.match(content.strip()))


def history(name: str, limit: int) -> list[Msg]:
    """Last `limit` *usable* messages from chat_log.jsonl.

    Junk is filtered BEFORE the limit is applied, so a person whose recent
    history is all `[reel]` still yields real conversation.
    """
    folder = resolve(name)
    if not folder:
        return []
    p = PEOPLE_DIR / folder / "chat_log.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        c = d.get("content") or ""
        if is_junk(c):
            continue
        out.append(Msg(d.get("sender", "them"), c))
    return out[-limit:]


def append(name: str, msgs: list[Msg]) -> None:
    """Append messages to chat_log.jsonl and bump the profile counter."""
    folder = resolve(name)
    d = PEOPLE_DIR / folder if folder else _dir(name)
    now = datetime.now().isoformat()
    with open(d / "chat_log.jsonl", "a", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps({
                "ts": now, "sender": m.sender, "content": m.content,
                "type": "text", "platform": "instagram",
            }, ensure_ascii=False) + "\n")

    prof = profile(name)
    prof.pop("context", None)
    prof["total_messages"] = prof.get("total_messages", 0) + len(msgs)
    prof["last_interaction"] = now
    prof.setdefault("created_at", now)
    (d / "profile.json").write_text(
        json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def people() -> list[str]:
    if not PEOPLE_DIR.exists():
        return []
    return sorted(d.name for d in PEOPLE_DIR.iterdir() if d.is_dir())
