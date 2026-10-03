"""Private Telegram owner control. No command grants model or send authority."""

from __future__ import annotations

import json
import asyncio
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from shadow.coordinator import Coordinator
from shadow.storage import Storage

_THREAD = re.compile(r"[0-9]+\Z")


class OwnerCommands:
    def __init__(self, storage: Storage, coordinator: Coordinator, *,
                 user_id: int, chat_id: int):
        if type(user_id) is not int or type(chat_id) is not int or user_id <= 0 or chat_id <= 0:
            raise ValueError("positive numeric owner user and private chat IDs required")
        self.storage, self.coordinator = storage, coordinator
        self.user_id, self.chat_id = user_id, chat_id

    def process(self, update: dict) -> str | None:
        """Persist one authorized result and its effect atomically. None means ignore."""
        if type(update) is not dict or type(update.get("update_id")) is not int:
            return None
        update_id = update["update_id"]
        if update_id < 0:
            return None
        with self.storage.transaction():
            if self.storage.owner_update(update_id):
                return None
            message = update.get("message")
            if not self._authorized(message):
                self.storage.record_owner_update(update_id, "ignored", "ignored")
                return None
            text = message["text"].strip()
            if not text:
                self.storage.record_owner_update(update_id, "ignored", "ignored")
                return None
            command = text.split(maxsplit=1)[0]
            outcome = self._execute(command, text[len(command):].strip())
            self.storage.record_owner_update(update_id, command, outcome)
            return outcome

    def _authorized(self, message: object) -> bool:
        if type(message) is not dict or type(message.get("text")) is not str:
            return False
        user, chat = message.get("from"), message.get("chat")
        return (
            type(user) is dict and type(chat) is dict
            and type(user.get("id")) is int and user["id"] == self.user_id
            and type(chat.get("id")) is int and chat["id"] == self.chat_id
            and chat.get("type") == "private"
            and not any(key in message for key in
                        ("forward_origin", "forward_from", "sender_chat", "via_bot", "is_automatic_forward"))
            and len(message["text"]) <= 4096
        )

    def _conversation(self, arg: str):
        if not _THREAD.fullmatch(arg):
            return None
        return self.storage.get_conversation_by_thread(self.coordinator.account_id, arg)

    def _execute(self, command: str, arg: str) -> str:
        if command == "/help":
            return ("/status /pause /resume /mode observe /approve <thread-id> "
                    "/ignore <thread-id> /alerts /digest. Sending and autonomous mode are locked.")
        if command == "/status":
            runtime = self.storage.get_runtime() or {}
            pending = len(self.storage.work_in_states(("pending", "due", "awaiting_approval")))
            held = len(self.storage.work_in_states(("held", "owner_active")))
            return (f"mode={runtime.get('mode', 'observe')} "
                    f"pause={runtime.get('pause_reason') or 'none'} "
                    f"pending={pending} held={held}")
        if command == "/pause":
            if arg:
                return "Use /pause without arguments; per-thread pause is not installed."
            self.coordinator.pause("owner")
            return "Paused; in-flight browser submission may remain uncertain."
        if command == "/resume":
            if arg:
                return "Use /resume without arguments."
            if (self.storage.get_runtime() or {}).get("pause_reason") != "owner":
                return "Refused: only an owner pause can be cleared here; recheck browser health."
            self.coordinator.resume()
            return "Owner pause cleared; identity and per-thread holds remain."
        if command == "/mode":
            if arg != "observe":
                return "Refused: assisted/autonomous require qualification and controlled live gates."
            self.coordinator.set_mode("observe")
            return "Mode observe: no Instagram sending."
        if command in ("/approve", "/ignore"):
            conversation = self._conversation(arg)
            if not conversation:
                return "No exact observed thread ID in the bound account; no change."
            cid = conversation["id"]
            if command == "/approve":
                if conversation["contact_status"] != "unknown_pending":
                    return "No change: contact is not pending approval."
                self.storage.set_contact_status(cid, "approved_unknown", "owner_command")
                return f"Thread {arg} approved for future decisions; no message queued."
            self.storage.set_contact_status(cid, "excluded", "owner_command")
            self.storage.cancel_outbox_items(cid, "owner_ignore")
            self.storage.cancel_decisions(cid, ("recorded", "awaiting_approval", "approved", "queued"))
            work = self.storage.get_work(cid)
            if work:
                self.storage.update_work(cid, state="held", held_reason="excluded",
                                         cancel_generation=work["cancel_generation"] + 1)
            return f"Thread {arg} excluded; pending work cancelled."
        if command == "/alerts":
            counts = {tier: 0 for tier in ("urgent", "action", "info")}
            for alert in self.storage.alerts(state="pending"):
                counts[alert["tier"]] += 1
            return "Pending alerts: " + ", ".join(f"{tier}={count}" for tier, count in counts.items())
        if command == "/digest":
            sent = sum(item["state"] == "confirmed" for item in self.storage.outbox_items())
            return f"Confirmed sends (all time)={sent}; " + self._execute("/alerts", "")
        return "Unsupported command. /help lists installed controls; no Instagram send performed."


class TelegramTransport:
    """Minimal stdlib long polling; transport failures never expose the token."""

    def __init__(self, token: str):
        if not token or "/" in token or "?" in token:
            raise ValueError("Telegram bot token required")
        self._url = "https://api.telegram.org/bot" + token + "/"

    def _post(self, method: str, **fields):
        try:
            request = Request(self._url + method, data=urlencode(fields).encode("utf-8"))
            with urlopen(request, timeout=40) as reply:
                raw = reply.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("oversized response")
            result = json.loads(raw)
            if result.get("ok") is not True:
                raise ValueError("Telegram API refused request")
            return result["result"]
        except Exception:
            raise RuntimeError("Telegram transport unavailable; no command acknowledgement") from None

    def poll_once(self, handler: OwnerCommands) -> int:
        cursor = handler.storage.last_owner_update_id()
        updates = self._post("getUpdates", offset=0 if cursor is None else cursor + 1,
                             timeout=20, allowed_updates=json.dumps(["message"]))
        count = 0
        for update in updates:
            outcome = handler.process(update)
            if outcome is not None:
                count += 1
        for response in handler.storage.pending_owner_responses():
            self._post("sendMessage", chat_id=handler.chat_id, text=response["outcome"])
            handler.storage.mark_owner_response_delivered(response["update_id"])
        return count

    async def poll_once_async(self, handler: OwnerCommands) -> int:
        """Only HTTP runs in worker threads. SQLite and commands stay on the
        coordinator's event-loop thread, so pause can interrupt model waits."""
        cursor = handler.storage.last_owner_update_id()
        updates = await asyncio.to_thread(
            self._post, "getUpdates", offset=0 if cursor is None else cursor + 1,
            timeout=20, allowed_updates=json.dumps(["message"]),
        )
        if type(updates) is not list:
            raise RuntimeError("Telegram returned invalid updates")
        count = sum(handler.process(update) is not None for update in updates)
        for response in handler.storage.pending_owner_responses():
            await asyncio.to_thread(self._post, "sendMessage", chat_id=handler.chat_id,
                                    text=response["outcome"])
            handler.storage.mark_owner_response_delivered(response["update_id"])
        return count
