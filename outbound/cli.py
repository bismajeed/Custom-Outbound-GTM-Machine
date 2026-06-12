"""Typer CLI — every command for the outbound engine.

    outbound init                      create DB, scaffold .env
    outbound brief new <industry>      interactive brief wizard
    outbound brief list                list available briefs
    outbound seed <industry> [--depth] one-time large fill of the reservoir
    outbound run <industry>            incremental cascade + reservoir top-up
    outbound preview <industry> [-n]   print sample personalized emails
    outbound load <industry> [--quota] drain reservoir -> Smartlead (the only sender)
    outbound sync                      pull replies/bounces/unsubs -> ledger + suppression
    outbound status [<industry>]       reservoir depth, counts, cost, last run
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import brief as brief_mod
from . import config as config_mod
from . import run as run_mod
from . import sync as sync_mod
from .db import Database
from .export import export_brief

app = typer.Typer(
    add_completion=False,
    help="Replenishing-reservoir cold-email pipeline. Bring your own API keys.",
    no_args_is_help=True,
)
brief_app = typer.Typer(help="Author and list industry briefs.", no_args_is_help=True)
app.add_typer(brief_app, name="brief")

console = Console()


def _db(require_keys: bool = True) -> Database:
    """Open the database. If require_keys, validate API keys first (fail fast)."""
    if require_keys:
        cfg = config_mod.get_config()
        url = cfg["database_url"]
    else:
        url = config_mod.get_database_url()
    db = Database(url)
    db.init()
    return db


def _load_brief(industry: str) -> brief_mod.Brief:
    try:
        return brief_mod.load_brief(industry)
    except brief_mod.BriefError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


# --- init --------------------------------------------------------------------

@app.command()
def init() -> None:
    """Create the database and scaffold a .env file if one is absent."""
    env_path = Path(".env")
    example = Path(".env.example")
    if not env_path.exists() and example.exists():
        shutil.copy(example, env_path)
        console.print("[green]Created .env[/green] from .env.example — fill in your keys.")
    elif env_path.exists():
        console.print("[dim].env already exists — leaving it untouched.[/dim]")
    else:
        console.print("[yellow]No .env.example found; skipping .env scaffold.[/yellow]")

    db = _db(require_keys=False)
    console.print(f"[green]Database ready[/green] at {db.engine.url}")


# --- brief subcommands -------------------------------------------------------

@brief_app.command("new")
def brief_new(
    industry: str = typer.Argument(..., help="Industry name, e.g. construction"),
    private: bool = typer.Option(False, help="Write to briefs/private/ (gitignored)."),
) -> None:
    """Interactive wizard to author briefs/<industry>.yaml."""
    try:
        path = brief_mod.new_brief_interactive(industry, private=private)
    except brief_mod.BriefError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]Wrote {path}[/green]")


@brief_app.command("list")
def brief_list() -> None:
    """List all available briefs (public + private)."""
    names = brief_mod.list_briefs()
    if not names:
        console.print("[dim]No briefs yet. Create one: outbound brief new <industry>[/dim]")
        return
    for name in names:
        console.print(f"  • {name}")


# --- ingest / process (pre-validated lists) ----------------------------------

@app.command()
def ingest(
    industry: str = typer.Argument(...),
    csv_path: str = typer.Argument(..., help="Path to a validated company CSV."),
    offset: int = typer.Option(0, help="Skip the first N rows (for splitting a file)."),
    limit: Optional[int] = typer.Option(None, help="Take at most N rows."),
) -> None:
    """Load pre-validated companies from a CSV as QUALIFIED (skips source/validate)."""
    from . import ingest as ingest_mod
    db = _db()
    brief = _load_brief(industry)
    counts = ingest_mod.ingest(db, brief, csv_path, offset=offset, limit=limit)
    console.print(f"[green]Ingested[/green] {counts['inserted']} companies "
                  f"({counts['skipped']} dedup/skipped) of {counts['considered']} considered.")


@app.command()
def process(
    industry: str = typer.Argument(...),
    signals: bool = typer.Option(
        False, "--signals/--no-signals",
        help="Run Claude news research per company for signal-anchored copy."),
) -> None:
    """Enrich + personalize ingested (QUALIFIED) companies into the reservoir."""
    db = _db()
    brief = _load_brief(industry)
    run_mod.run_ingested(db, brief, use_signals=signals)


# --- seed / run --------------------------------------------------------------

@app.command()
def seed(
    industry: str = typer.Argument(...),
    depth: Optional[int] = typer.Option(None, help="Target reservoir depth in days."),
    limit: Optional[int] = typer.Option(
        None, help="Hard-cap companies to source this run (for a cheap test)."),
) -> None:
    """One-time large source + cascade to fill the reservoir to N days."""
    db = _db()
    brief = _load_brief(industry)
    run_mod.run(db, brief, depth_override=depth or brief.target_depth_days,
                source_limit_override=limit)


@app.command()
def run(
    industry: str = typer.Argument(...),
    limit: Optional[int] = typer.Option(
        None, help="Hard-cap companies to source this run (for a cheap test)."),
) -> None:
    """Incremental cascade + reservoir top-up (idempotent, resumable)."""
    db = _db()
    brief = _load_brief(industry)
    run_mod.run(db, brief, source_limit_override=limit)


# --- preview -----------------------------------------------------------------

@app.command()
def preview(
    industry: str = typer.Argument(...),
    n: int = typer.Option(5, "-n", help="How many sample emails to show."),
) -> None:
    """Print a sample of personalized emails for a human spot-check."""
    db = _db()
    brief = _load_brief(industry)
    contacts = db.contacts_by_status(industry, "QUEUED", limit=n)
    if not contacts:
        console.print("[yellow]No QUEUED contacts to preview. Run the pipeline first.[/yellow]")
        return
    for c in contacts:
        company = db.get_company(c.company_domain)
        console.print("\n[bold cyan]" + "─" * 60 + "[/bold cyan]")
        console.print(f"[bold]{c.first_name} {c.last_name}[/bold] — {c.title}")
        console.print(f"  {c.email}  |  {company.name if company else c.company_domain}")
        console.print(f"  hook: [magenta]{c.cell}[/magenta]")
        if company and company.signal_summary:
            console.print(f"  signal: [dim]{company.signal_summary}[/dim]")
        if brief.personalization_mode == "full" and c.subject:
            console.print(f"\n  subject: {c.subject}")
            console.print(f"  {c.body}")
        else:
            console.print(f"\n  first line: [green]{c.first_line}[/green]")


# --- load --------------------------------------------------------------------

@app.command()
def load(
    industry: str = typer.Argument(...),
    quota: Optional[int] = typer.Option(None, help="Max leads to push this run."),
) -> None:
    """Drain the reservoir into Smartlead. The only command that sends."""
    db = _db()
    brief = _load_brief(industry)
    run_mod.load(db, brief, quota=quota)


# --- sync --------------------------------------------------------------------

@app.command()
def sync(
    industry: Optional[str] = typer.Argument(None, help="Limit to one brief."),
) -> None:
    """Pull replies/bounces/unsubs from Smartlead; update ledger + suppression."""
    db = _db()
    if industry:
        briefs = [_load_brief(industry)]
    else:
        briefs = [_load_brief(name) for name in brief_mod.list_briefs()]
    if not briefs:
        console.print("[yellow]No briefs to sync.[/yellow]")
        return
    sync_mod.sync(db, briefs)


# --- export ------------------------------------------------------------------

@app.command()
def export(
    industry: str = typer.Argument(...),
    out_dir: str = typer.Option("output", "--dir", help="Output directory."),
) -> None:
    """Write CSV snapshots (companies, contacts, runs) for a brief.

    The `status` column shows where each record is in the cascade. Companies
    and contacts that were dropped/suppressed are included with their reason.
    """
    db = _db(require_keys=False)
    written = export_brief(db, industry, out_dir=out_dir)
    for name, path in written.items():
        console.print(f"  {name:10} → {path}")


# --- status ------------------------------------------------------------------

@app.command()
def status(
    industry: Optional[str] = typer.Argument(None, help="Limit to one brief."),
) -> None:
    """Show reservoir depth, pipeline counts, cost-to-date, and last run."""
    db = _db(require_keys=False)
    names = [industry] if industry else (brief_mod.list_briefs() or db.briefs_in_db())
    if not names:
        console.print("[dim]No briefs found.[/dim]")
        return

    for name in names:
        try:
            brief = brief_mod.load_brief(name)
        except brief_mod.BriefError:
            brief = None

        comp_counts = db.count_companies_by_status(name)
        contact_counts = db.count_contacts_by_status(name)
        queued = contact_counts.get("QUEUED", 0)
        depth = run_mod.reservoir_depth_days(db, brief) if brief else 0.0
        cost = db.total_cost(name)
        last = db.last_run(name)

        table = Table(title=f"Status — {name}", show_header=True,
                      header_style="bold")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_row("Reservoir (QUEUED)", str(queued))
        table.add_row("Reservoir depth (days)", f"{depth:.1f}")
        table.add_row("Cost to date (USD)", f"${cost:.2f}")
        if last:
            table.add_row("Last run",
                          f"{last['stage']} / {last['status']} @ {last['started_at']}")
        console.print(table)

        if comp_counts:
            console.print("  Companies: " + ", ".join(
                f"{k}={v}" for k, v in sorted(comp_counts.items())))
        if contact_counts:
            console.print("  Contacts:  " + ", ".join(
                f"{k}={v}" for k, v in sorted(contact_counts.items())))
        console.print()


if __name__ == "__main__":
    app()
