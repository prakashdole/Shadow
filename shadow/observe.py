"""One bounded, non-sending observation through the v2 browser and store."""

import asyncio

from shadow.contracts import GLOBAL_PAUSE_STATUSES, RoleOutcome
from shadow.coordinator import Coordinator, TickReport
from shadow.settings import Settings, SettingsError
from shadow.storage import Storage


class NoDecisionProvider:
    """Hold new work honestly until the configured P05 decision roles exist."""

    async def assess(self, _data):
        return RoleOutcome(role="risk", failure="not_configured")

    async def draft(self, _data, _assessment):
        return RoleOutcome(role="reply", failure="not_configured")

    async def extract(self, _data):
        return RoleOutcome(role="extraction", failure="not_configured")


async def observe_once(
    storage: Storage, browser, settings: Settings, account_id: str
) -> TickReport:
    """Inspect one inbox snapshot; never create or submit a reply."""
    return await observe_loop(storage, browser, settings, account_id, once=True)


async def observe_loop(storage: Storage, browser, settings: Settings, account_id: str,
                       decisions=None, *, once: bool = False, owner_transport=None,
                       owner_user_id: int | None = None, owner_chat_id: int | None = None,
                       secondary_chat_id: int | None = None) -> TickReport:
    """Keep a dedicated browser bound for read-only observation.

    One sequential worker owns the store and browser. A global pause exits so
    login/challenge recovery remains a deliberate owner action.
    """
    if settings.mode != "observe":
        raise SettingsError("observe requires mode = 'observe'")
    runtime = storage.get_runtime()
    if runtime and runtime["mode"] != "observe":
        raise SettingsError("stored runtime mode is not observe")
    coordinator = Coordinator(storage, browser, decisions or NoDecisionProvider(),
                              settings, account_id=account_id)
    await coordinator.startup()
    from shadow.operations import start_controls

    controls = await start_controls(storage.directory, coordinator)
    owner_task = None
    alert_task = None
    if owner_transport is not None:
        from shadow.owner import OwnerCommands

        handler = OwnerCommands(storage, coordinator, user_id=owner_user_id, chat_id=owner_chat_id)

        async def poll_owner():
            while True:
                try:
                    await owner_transport.poll_once_async(handler)
                except RuntimeError:
                    storage.raise_alert(dedupe_key="telegram_outage", tier="action",
                                        reason="telegram_outage")
                    await asyncio.sleep(settings.inbox_check_interval)

        owner_task = asyncio.create_task(poll_owner())
        from shadow.notifications import Notifications

        notifications = Notifications(storage, owner_transport, settings, chat_id=owner_chat_id,
                                      secondary_chat_id=secondary_chat_id)

        async def deliver_alerts():
            from shadow.approvals import Approvals

            approvals = Approvals(coordinator)
            while True:
                await notifications.deliver_due()
                for request in approvals.pending():
                    if request["message_id"] is not None:
                        continue
                    import json

                    draft = json.loads(request["record_json"])["reply"]["messages"]
                    text = f"Draft #{request['id']} thread ref={request['conversation_id']}\n" + "\n".join(draft)
                    keyboard = {"inline_keyboard": [[
                        {"text": action.title(), "callback_data": f"approval:{request['id']}:{action}"}
                        for action in ("send", "edit", "skip")]]}
                    try:
                        result = await asyncio.to_thread(owner_transport._post, "sendMessage", chat_id=owner_chat_id,
                                                         text=text, reply_markup=json.dumps(keyboard))
                    except RuntimeError:
                        continue
                    if type(result) is dict and type(result.get("message_id")) is int:
                        with storage.transaction() as conn:
                            conn.execute("UPDATE approval_requests SET message_id = ? WHERE id = ?",
                                         (result["message_id"], request["id"]))
                await asyncio.sleep(max(1, settings.pane_check_interval))

        alert_task = asyncio.create_task(deliver_alerts())
    try:
        initial = await browser.start()
        if initial.status in GLOBAL_PAUSE_STATUSES:
            coordinator.pause(initial.status)
            storage.raise_alert(dedupe_key=f"global_pause:{initial.status}",
                                tier="urgent", reason=initial.status)
        if initial.status not in ("ok", "empty_verified"):
            return TickReport(stopped_reason=initial.status)
        while True:
            report = await coordinator.tick_discover()
            if report.stopped_reason or once:
                return report
            await asyncio.sleep(settings.inbox_check_interval)
    finally:
        if controls is not None:
            controls.close()
            await controls.wait_closed()
        tasks = [task for task in (owner_task, alert_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await coordinator.close()
        await browser.close()
