"""CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import sys

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
    """Start the bot. Chrome must already be running with --remote-debugging-port=9222."""
    setup_logging(verbose)
    from shadow.loop import run as _run
    try:
        asyncio.run(_run(once=once, dry_run=dry_run))
    except KeyboardInterrupt:
        click.echo("\nstopped")
    except RuntimeError as e:
        click.echo(f"\n{e}", err=True)
        raise SystemExit(1)


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
