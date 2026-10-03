"""CLI entry point."""

from __future__ import annotations

import logging
import asyncio
import os
import sys
from datetime import datetime, timezone

import click

from shadow import brain, memory
from shadow.config import (
    ACTIVE_END, ACTIVE_START, EFFORT, LOG_FILE, MODEL, PROVIDER,
)
from shadow.memory import Msg

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)-16s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


@click.group()
def cli() -> None:
    """Shadow — replies to your Instagram DMs in your voice."""


@cli.command()
@click.option("--once", is_flag=True, help="One cycle then exit.")
@click.option("--dry-run", is_flag=True, help="Print replies instead of sending.")
@click.option("--verbose", is_flag=True)
def run(once: bool, dry_run: bool, verbose: bool) -> None:
    """Start the bot. Disabled since P01; see the message it prints."""
    click.echo(
        "Legacy runtime is disabled since P01: its send path can address the "
        "wrong chat and record unsent replies. The new runtime arrives in P03. "
        "Offline check: `shadow reply`."
    )
    raise SystemExit(2)


@cli.command("observe-once")
def observe_once_command() -> None:
    """Inspect one inbox snapshot with v2; no model call or Instagram send."""
    from shadow.instagram import InstagramBrowser
    from shadow.observe import observe_once
    from shadow.settings import SettingsError, data_dir, load_settings
    from shadow.storage import open_storage

    account_id = os.environ.get("SHADOW_ACCOUNT_ID", "").strip()
    expected_handle = os.environ.get("SHADOW_EXPECTED_ACCOUNT", "").strip()
    if not account_id or not expected_handle:
        raise click.ClickException(
            "Set SHADOW_ACCOUNT_ID and SHADOW_EXPECTED_ACCOUNT privately before observing."
        )
    try:
        directory = data_dir()
        settings = load_settings(directory)
        with open_storage(directory) as storage:
            browser = InstagramBrowser(
                cdp_url=os.environ.get("SHADOW_CDP_URL", "http://127.0.0.1:9222"),
                expected_account=expected_handle,
            )
            report = asyncio.run(observe_once(storage, browser, settings, account_id))
    except (SettingsError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"rows={report.rows} opened={report.opened} ingested={report.ingested} "
        f"held={report.held} status={report.stopped_reason or 'ok'}"
    )


@cli.command("owner-poll-once")
def owner_poll_once_command() -> None:
    """Process one private Telegram poll; never opens Instagram or sends there."""
    from shadow.coordinator import Coordinator
    from shadow.observe import NoDecisionProvider
    from shadow.owner import OwnerCommands, TelegramTransport
    from shadow.settings import SettingsError, data_dir, load_settings
    from shadow.storage import open_storage

    token = os.environ.get("SHADOW_TELEGRAM_TOKEN", "")
    try:
        user_id = int(os.environ.get("SHADOW_TELEGRAM_USER_ID", ""))
        chat_id = int(os.environ.get("SHADOW_TELEGRAM_CHAT_ID", ""))
        with open_storage(data_dir()) as storage:
            runtime = storage.get_runtime()
            if not runtime or not runtime["account_id"]:
                raise ValueError("Observe and bind the Instagram account before owner polling")
            coordinator = Coordinator(storage, None, NoDecisionProvider(),
                                      load_settings(data_dir()), account_id=runtime["account_id"])
            handler = OwnerCommands(storage, coordinator, user_id=user_id, chat_id=chat_id)
            count = TelegramTransport(token).poll_once(handler)
    except (ValueError, SettingsError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"accepted_commands={count}")


@cli.command("observe")
@click.option("--once", is_flag=True, help="One snapshot, then stop.")
@click.option("--with-model", is_flag=True, help="Call explicitly configured models; still never send.")
@click.option("--with-telegram", is_flag=True, help="Process owner controls in the observer process.")
def observe_command(once: bool, with_model: bool, with_telegram: bool) -> None:
    """Run the v2 non-sending observer against a dedicated browser profile."""
    from shadow.instagram import InstagramBrowser
    from shadow.observe import observe_loop
    from shadow.settings import SettingsError, data_dir, load_settings
    from shadow.storage import open_storage

    account_id = os.environ.get("SHADOW_ACCOUNT_ID", "").strip()
    expected = os.environ.get("SHADOW_EXPECTED_ACCOUNT", "").strip()
    if not account_id or not expected:
        raise click.ClickException("Set SHADOW_ACCOUNT_ID and SHADOW_EXPECTED_ACCOUNT privately.")
    try:
        directory = data_dir()
        settings = load_settings(directory)
        with open_storage(directory) as storage:
            owner_transport = None
            owner_ids = {}
            if with_telegram:
                from shadow.owner import TelegramTransport

                owner_transport = TelegramTransport(os.environ.get("SHADOW_TELEGRAM_TOKEN", ""))
                owner_ids = {
                    "owner_user_id": int(os.environ.get("SHADOW_TELEGRAM_USER_ID", "")),
                    "owner_chat_id": int(os.environ.get("SHADOW_TELEGRAM_CHAT_ID", "")),
                }
                secondary = os.environ.get("SHADOW_TELEGRAM_SECONDARY_CHAT_ID", "").strip()
                if secondary:
                    owner_ids["secondary_chat_id"] = int(secondary)
            decisions = None
            if with_model:
                from shadow.decisions import StructuredDecisions
                from shadow.model_bedrock import BedrockTransport

                models = {role: os.environ.get(f"SHADOW_{role.upper()}_MODEL", "").strip()
                          for role in ("risk", "reply", "extraction")}
                transport = BedrockTransport(region=os.environ.get("SHADOW_AWS_REGION", ""),
                                             models=models)
                decisions = StructuredDecisions(transport, storage, settings)
            browser = InstagramBrowser(
                cdp_url=os.environ.get("SHADOW_CDP_URL", "http://127.0.0.1:9222"),
                expected_account=expected,
            )
            report = asyncio.run(observe_loop(storage, browser, settings, account_id,
                                              decisions, once=once,
                                              owner_transport=owner_transport, **owner_ids))
    except (SettingsError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"rows={report.rows} opened={report.opened} ingested={report.ingested} "
               f"assessed={report.assessed} held={report.held} "
               f"status={report.stopped_reason or 'ok'}; Instagram sends=0")


@cli.command("import-history")
@click.option("--source", required=True, type=click.Path(exists=True),
              help="Directory holding people/<folder>/chat_log.jsonl.")
@click.option("--dry-run", is_flag=True, help="Import into a throwaway database.")
@click.option("--i-have-a-verified-backup", is_flag=True,
              help="Confirm the originals are backed up before a live import.")
def import_history(source: str, dry_run: bool, i_have_a_verified_backup: bool) -> None:
    """Import legacy history. Reports counts only; never modifies the source."""
    from shadow.legacy_import import run_import
    from shadow.settings import data_dir

    scratch = None
    if not dry_run:
        if not os.environ.get("SHADOW_DATA_DIR"):
            raise SystemExit(
                "SHADOW_DATA_DIR is not set. Point it at the private data "
                "directory before a live import, then re-run."
            )
        if not i_have_a_verified_backup:
            raise SystemExit(
                "Refusing a live import without --i-have-a-verified-backup. "
                "The originals are left in place either way; copy them first, "
                "or run --dry-run to see the counts."
            )
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        scratch = data_dir() / "private" / "p01" / f"dryrun-{stamp}"
    click.echo(run_import(source, None, dry_run=dry_run, scratch_dir=scratch).as_text())


@cli.command()
@click.option("--person", required=True, help="Name, as stored in data/people/.")
@click.option("--message", required=True, help="What they said.")
def reply(person: str, message: str) -> None:
    """Test the model on one message. No browser, nothing sent."""
    setup_logging()
    prof = memory.profile(person)
    folder = memory.resolve(person)
    past = memory.history(person, 20)
    live = [Msg("them", message)]

    click.echo(f"person:    {person}" + (f"  (data/people/{folder})" if folder else "  (NEW)"))
    click.echo(f"closeness: {prof.get('closeness')}/10  rel: {prof.get('relationship') or '-'}")
    click.echo(f"history:   {len(past)} usable stored messages (junk filtered)")
    click.echo(f"model:     {PROVIDER or '(unset)'} / {MODEL or '(unset)'}  effort={EFFORT}\n")

    try:
        d = brain.reply(person, prof, live, past)
    except RuntimeError as e:
        click.echo(f"{e}", err=True)
        raise SystemExit(1)

    if d.action == "freeze":
        click.echo(f"FROZEN [{d.note}] — this would be skipped and logged.")
        return
    if d.action != "reply":
        click.echo(f"SKIP ({d.note})")
        return
    for m in d.messages:
        click.echo(f"  > {m}")


@cli.command()
def status() -> None:
    """Show config, state, and who is in memory."""
    from shadow.loop import in_active_hours, load_state, stories_due

    state = load_state()
    if PROVIDER and MODEL:
        click.echo(f"provider:     {PROVIDER}")
        click.echo(f"model:        {MODEL}")
    else:
        click.echo("provider:     NOT CHOSEN YET  (set SHADOW_PROVIDER + SHADOW_MODEL)")
        click.echo("model:        NOT CHOSEN YET")
    click.echo(f"effort:       {EFFORT}")
    click.echo(f"active hours: {ACTIVE_START:02d}:00-{ACTIVE_END:02d}:00  "
               f"(now: {'INSIDE' if in_active_hours() else 'OUTSIDE'})")
    click.echo(f"people:       {len(memory.people())}")
    click.echo(f"threads seen: {len(state.get('replied', {}))}")
    click.echo(f"stories:      {'due today' if stories_due(state) else 'done today'}")


if __name__ == "__main__":
    cli()
