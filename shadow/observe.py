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
                       decisions=None, *, once: bool = False) -> TickReport:
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
        await browser.close()
