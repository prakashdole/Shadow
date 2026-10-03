"""One bounded, non-sending observation through the v2 browser and store."""

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
    if settings.mode != "observe":
        raise SettingsError("observe-once requires mode = 'observe'")
    runtime = storage.get_runtime()
    if runtime and runtime["mode"] != "observe":
        raise SettingsError("stored runtime mode is not observe; refuse inspection")
    coordinator = Coordinator(
        storage, browser, NoDecisionProvider(), settings, account_id=account_id
    )
    await coordinator.startup()
    try:
        initial = await browser.start()
        if initial.status in GLOBAL_PAUSE_STATUSES:
            coordinator.pause(initial.status)
            storage.raise_alert(
                dedupe_key=f"global_pause:{initial.status}", tier="urgent",
                reason=initial.status,
            )
        if initial.status not in ("ok", "empty_verified"):
            return TickReport(stopped_reason=initial.status)
        return await coordinator.tick_discover()
    finally:
        await browser.close()
