"""Weekly campaign performance report.

Pulls aggregate statistics from Smartlead for every campaign that belongs to
the given briefs, combines with the local events ledger (last 7 days), and
renders a Rich table. If SLACK_TOKEN + SLACK_CHANNEL_ID are set in the
environment, the summary is also posted to that Slack channel.

Usage:
    outbound report                  # all briefs (+ Slack if env vars set)
    outbound report construction     # one brief
    outbound report --days 14        # wider window for local event counts
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests
from rich.console import Console
from rich.table import Table

from .db import Database
from .load import smartlead
from .models import Brief

console = Console()

SLACK_API = "https://slack.com/api/chat.postMessage"


def _pct(n: int, total: int) -> str:
    if not total:
        return "—"
    return f"{100 * n / total:.1f}%"


def _campaign_stats(campaign_id: str) -> dict:
    """Aggregate sent/opened/replied/bounced/unsub from all leads (incl. completed)."""
    leads = smartlead.fetch_all_leads(campaign_id)
    total = len(leads)

    opened = sum(
        1 for l in leads
        if l.get("is_opened")
        or int(l.get("open_count") or 0) > 0
        or "opened" in str(l.get("lead_status", "")).lower()
    )
    replied = sum(
        1 for l in leads
        if l.get("has_replied")
        or l.get("reply_time")
        or "replied" in str(l.get("lead_status", "")).lower()
    )
    positive = sum(
        1 for l in leads
        if any(
            w in str(l.get("reply_category") or "").lower()
            for w in ("positive", "interested", "meeting")
        )
    )
    bounced = sum(
        1 for l in leads
        if l.get("is_bounced")
        or "bounce" in str(l.get("lead_status", "")).lower()
    )
    unsub = sum(
        1 for l in leads
        if l.get("is_unsubscribed")
        or "unsub" in str(l.get("lead_status", "")).lower()
    )
    return {
        "total": total,
        "opened": opened,
        "replied": replied,
        "positive": positive,
        "bounced": bounced,
        "unsub": unsub,
    }


def _week_events(db: Database, days: int) -> dict[str, int]:
    """Count event types in the local ledger from the last N days."""
    from sqlalchemy import text
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        with db.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT type, COUNT(*) as n FROM events WHERE at >= :s GROUP BY type"),
                {"s": since},
            ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def weekly_report(db: Database, briefs: list[Brief], days: int = 7) -> dict:
    """Build the report dict: one row per Smartlead campaign + local week events."""
    try:
        campaigns = smartlead._list_campaigns()
    except Exception as exc:
        console.print(f"[red]Could not list Smartlead campaigns: {exc}[/red]")
        return {}

    active = {b.industry for b in briefs}
    rows: list[dict] = []
    totals: dict[str, int] = dict(total=0, opened=0, replied=0, positive=0, bounced=0, unsub=0)

    for camp in campaigns:
        name = str(camp.get("name", ""))
        industry = next((ind for ind in active if name.startswith(f"{ind} —")), None)
        if not industry:
            continue

        camp_id = str(camp.get("id"))
        console.print(f"  [dim]fetching {name}…[/dim]")
        stats = _campaign_stats(camp_id)
        rows.append({"campaign": name, "industry": industry, **stats})
        for k in totals:
            totals[k] += stats.get(k, 0)

    week_events = _week_events(db, days)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "rows": rows,
        "totals": totals,
        "week_events": week_events,
        "generated_at": generated_at,
    }


def _slack_blocks(data: dict) -> list[dict]:
    """Build Slack Block Kit blocks for the report."""
    rows = data.get("rows", [])
    totals = data.get("totals", {})
    week_events = data.get("week_events", {})
    generated_at = data.get("generated_at", "")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Outbound Report", "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": generated_at}],
        },
        {"type": "divider"},
    ]

    for r in rows:
        sent = r["total"]
        line = (
            f"*{r['campaign']}*\n"
            f"Sent: {sent}  ·  "
            f"Open: {_pct(r['opened'], sent)}  ·  "
            f"Reply: {_pct(r['replied'], sent)}  ·  "
            f"{'✅ ' + str(r['positive']) + ' positive  ·  ' if r['positive'] else ''}"
            f"{'⚠️ ' + str(r['bounced']) + ' bounced  ·  ' if r['bounced'] else ''}"
            f"Unsub: {r['unsub']}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})

    sent_total = totals.get("total", 0)
    summary = (
        f"*Totals:*  {sent_total} sent  ·  "
        f"Open: {_pct(totals.get('opened', 0), sent_total)}  ·  "
        f"Reply: {_pct(totals.get('replied', 0), sent_total)}  ·  "
        f"✅ {totals.get('positive', 0)} positive  ·  "
        f"Bounced: {totals.get('bounced', 0)}  ·  "
        f"Unsub: {totals.get('unsub', 0)}"
    )
    blocks.append({"type": "divider"})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": summary}})

    if week_events:
        parts = [
            f"{k.replace('_', ' ')}={v}"
            for k in ("positive_reply", "reply", "bounce", "unsubscribe")
            if (v := week_events.get(k, 0))
        ]
        if parts:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Synced this period: " + "  ·  ".join(parts)}],
            })

    return blocks


def post_to_slack(data: dict) -> None:
    """Post the report to Slack using SLACK_TOKEN + SLACK_CHANNEL_ID from env."""
    token = os.environ.get("SLACK_TOKEN", "")
    channel = os.environ.get("SLACK_CHANNEL_ID", "")
    if not token or not channel:
        return

    blocks = _slack_blocks(data)
    resp = requests.post(
        SLACK_API,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"channel": channel, "blocks": blocks},
        timeout=15,
    )
    body = resp.json()
    if not body.get("ok"):
        console.print(f"[yellow]Slack post failed:[/yellow] {body.get('error', resp.text)}")
    else:
        console.print(f"[green]Posted to Slack channel {channel}[/green]")


def print_report(data: dict) -> None:
    if not data:
        console.print("[yellow]No data to display.[/yellow]")
        return

    rows = data.get("rows", [])
    totals = data.get("totals", {})
    week_events = data.get("week_events", {})
    generated_at = data.get("generated_at", "")

    console.rule(f"[bold]Outbound Report[/bold]  [dim]{generated_at}[/dim]")

    if not rows:
        console.print("[dim]No active campaigns found for current briefs.[/dim]\n")
        return

    table = Table(show_header=True, header_style="bold cyan", show_lines=False, pad_edge=True)
    table.add_column("Campaign", min_width=30, no_wrap=True)
    table.add_column("Sent",     justify="right")
    table.add_column("Opened",   justify="right")
    table.add_column("Open %",   justify="right")
    table.add_column("Replied",  justify="right")
    table.add_column("Reply %",  justify="right")
    table.add_column("Positive", justify="right")
    table.add_column("Bounced",  justify="right")
    table.add_column("Unsub",    justify="right")

    for r in rows:
        sent = r["total"]
        table.add_row(
            r["campaign"],
            str(sent),
            str(r["opened"]),
            _pct(r["opened"], sent),
            str(r["replied"]),
            _pct(r["replied"], sent),
            f"[bold green]{r['positive']}[/bold green]" if r["positive"] else "[dim]0[/dim]",
            f"[red]{r['bounced']}[/red]" if r["bounced"] else "[dim]0[/dim]",
            str(r["unsub"]) if r["unsub"] else "[dim]0[/dim]",
        )

    console.print(table)

    # Totals summary line
    sent = totals.get("total", 0)
    console.print(
        f"\n  [bold]Totals:[/bold]  {sent} sent  ·  "
        f"[cyan]{_pct(totals.get('opened', 0), sent)}[/cyan] open  ·  "
        f"[green]{_pct(totals.get('replied', 0), sent)}[/green] reply  ·  "
        f"[bold green]{totals.get('positive', 0)} positive[/bold green]  ·  "
        f"[red]{totals.get('bounced', 0)}[/red] bounced  ·  "
        f"{totals.get('unsub', 0)} unsub"
    )

    # Local ledger events from the last N days
    if week_events:
        parts = []
        for k in ("positive_reply", "reply", "bounce", "unsubscribe"):
            v = week_events.get(k, 0)
            if v:
                parts.append(f"{k.replace('_', ' ')}={v}")
        if parts:
            console.print(f"  [dim]Synced this period:[/dim]  " + "  ·  ".join(parts))

    console.print()
    post_to_slack(data)
