"""The staged pipeline runner — the orchestrator.

``run()`` executes the cascade in order, each stage wrapped in a ``runs`` row for
resumability and cost logging. Cheap gates run before expensive ones; the paid
news stage only touches companies that passed the free job-signal gate.

Resumability: each stage reads records in their pre-stage status and writes the
post-stage status. A crash leaves records in their last completed status, so a
re-run resumes cleanly without double-processing or double-spending.

``load()`` is separate and is the ONLY path that pushes leads to Smartlead.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from rich.console import Console

from .db import Database, now_iso
from .export import export_brief
from .models import Brief, Company, CompanyStatus, ContactStatus
from .pipeline import enrich as enrich_mod
from .pipeline import personalize as personalize_mod
from .pipeline import signals_jobs, signals_news, validate as validate_mod
from .sources import apollo
from .load import smartlead

console = Console()

# Average touches per contact (follow-ups). Used to convert quota into the
# number of distinct contacts the reservoir must hold to cover target_depth.
AVG_TOUCHES = 1


def _run_id(brief: str, stage: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{brief}-{stage}-{stamp}-{uuid.uuid4().hex[:6]}"


def _stage(db: Database, brief: str, stage: str, fn: Callable[[], dict]) -> dict:
    """Run a stage function inside a runs row. Returns its counts dict."""
    run_id = _run_id(brief, stage)
    db.start_run(run_id, brief, stage)
    try:
        counts = fn()
        db.finish_run(run_id, "done", counts)
        return counts
    except Exception as exc:
        db.finish_run(run_id, "failed", {"error": str(exc)})
        console.print(f"[red]Stage {stage} failed:[/red] {exc}")
        raise


# --- reservoir math ----------------------------------------------------------

def reservoir_depth_days(db: Database, brief: Brief) -> float:
    queued = db.reservoir_count(brief.industry)
    per_day = max(brief.daily_quota / max(AVG_TOUCHES, 1), 1)
    return queued / per_day


def compute_source_limit(db: Database, brief: Brief,
                         depth_override: Optional[int] = None) -> int:
    """How many companies to source to top the reservoir up to target depth."""
    target_depth = depth_override if depth_override is not None else brief.target_depth_days
    target_contacts = int(target_depth * (brief.daily_quota / max(AVG_TOUCHES, 1)))
    current_queued = db.reservoir_count(brief.industry)
    needed_contacts = max(0, target_contacts - current_queued)
    if needed_contacts == 0:
        return 0
    per_company = max(brief.contacts_per_company, 1)
    return math.ceil(needed_contacts / per_company)


# --- stages ------------------------------------------------------------------

def stage_source(db: Database, brief: Brief, source_limit: int) -> dict:
    if source_limit <= 0:
        console.print("[dim]Reservoir already at target depth — nothing to source.[/dim]")
        return {"in": 0, "out": 0, "dropped": 0, "cost_usd": 0.0}
    companies = apollo.search_companies(brief, source_limit)
    inserted = 0
    skipped = 0
    for company in companies:
        if db.insert_company(company):
            inserted += 1
        else:
            skipped += 1  # dedup / suppression
    console.print(f"  source: {inserted} new companies ({skipped} dedup/suppressed)")
    return {"in": len(companies), "out": inserted, "dropped": skipped, "cost_usd": 0.0}


def stage_validate(db: Database, brief: Brief) -> dict:
    companies = db.companies_by_status(brief.industry, CompanyStatus.SOURCED)
    kept = dropped = 0
    for c in companies:
        if validate_mod.validate(c):
            db.update_company(c.domain, status=CompanyStatus.VALID)
            kept += 1
        else:
            db.update_company(c.domain, status=CompanyStatus.DROPPED,
                              drop_reason="invalid_domain")
            dropped += 1
    console.print(f"  validate: {kept} valid, {dropped} dropped")
    return {"in": len(companies), "out": kept, "dropped": dropped, "cost_usd": 0.0}


def stage_jobs(db: Database, brief: Brief) -> dict:
    companies = db.companies_by_status(brief.industry, CompanyStatus.VALID)
    found = 0
    for c in companies:
        passed, evidence = signals_jobs.has_job_signal(c, brief)
        # Non-blocking: every company advances. A hiring signal is recorded when
        # found, but its absence no longer drops the company — signals route into
        # campaigns, they don't gate. The richer news signal may supersede this.
        if passed:
            db.update_company(c.domain, status=CompanyStatus.JOB_SIGNAL,
                              signal_summary=evidence)
            found += 1
        else:
            db.update_company(c.domain, status=CompanyStatus.JOB_SIGNAL)
    console.print(f"  jobs (non-blocking): {found}/{len(companies)} have a hiring signal")
    return {"in": len(companies), "out": len(companies), "dropped": 0, "cost_usd": 0.0}


def stage_news(db: Database, brief: Brief) -> dict:
    """Paid news research, non-blocking. Short-circuit: a company that already has
    a (free) job signal skips the paid call — it's already in the 'signal' segment,
    so news can't change its routing. Only no-job-signal companies pay for news, to
    try to lift them out of 'free_implementation'. Nothing is dropped; every company
    ends QUALIFIED and the personalize stage routes it by what's on file."""
    companies = db.companies_by_status(brief.industry, CompanyStatus.JOB_SIGNAL)
    researched = found = skipped = 0
    cost = 0.0
    for c in companies:
        if (c.signal_summary or "").strip():
            # Already has a job signal -> already 'signal'; don't spend on news.
            db.update_company(c.domain, status=CompanyStatus.QUALIFIED)
            skipped += 1
            continue
        result = signals_news.research_news(c, brief)
        researched += 1
        cost += result.get("cost_usd", 0.0)
        if result["passed"]:
            db.update_company(c.domain, status=CompanyStatus.QUALIFIED,
                              signal_summary=result["summary"])
            found += 1
        else:
            db.update_company(c.domain, status=CompanyStatus.QUALIFIED)
    console.print(f"  news (Haiku, no-job-signal only): {found}/{researched} found a "
                  f"news signal, {skipped} skipped (already had job signal) (${cost:.2f})")
    return {"in": len(companies), "out": len(companies), "dropped": 0, "cost_usd": cost}


def stage_enrich(db: Database, brief: Brief) -> dict:
    companies = db.companies_by_status(brief.industry, CompanyStatus.QUALIFIED)
    total = 0
    for c in companies:
        contacts = enrich_mod.enrich_company(db, c, brief)
        total += len(contacts)
    console.print(f"  enrich: {total} new contacts across {len(companies)} companies")
    return {"in": len(companies), "out": total, "dropped": 0, "cost_usd": 0.0}


def stage_personalize(db: Database, brief: Brief) -> dict:
    contacts = db.contacts_by_status(brief.industry, ContactStatus.ENRICHED)

    # template mode: no per-lead LLM and the copy is hook-independent, so all
    # contacts go into one campaign (cell="all") rather than splitting by hook.
    if brief.personalization_mode == "template":
        for contact in contacts:
            db.update_contact(contact.email, cell="all",
                              status=ContactStatus.QUEUED)
        console.print(f"  queue (template, no LLM): {len(contacts)} contacts queued")
        return {"in": len(contacts), "out": len(contacts), "dropped": 0, "cost_usd": 0.0}

    done = 0
    cost = 0.0
    # Cache companies to avoid re-fetching per contact.
    company_cache: dict[str, Company] = {}
    for contact in contacts:
        company = company_cache.get(contact.company_domain)
        if company is None:
            company = db.get_company(contact.company_domain)
            company_cache[contact.company_domain] = company
        if company is None:
            continue
        updated, c = personalize_mod.personalize_detailed(contact, company, brief)
        cost += c
        # PERSONALIZED contacts become the reservoir → status QUEUED on write.
        db.update_contact(
            updated.email,
            cell=updated.cell,
            first_line=updated.first_line,
            subject=updated.subject,
            body=updated.body,
            status=ContactStatus.QUEUED,
        )
        done += 1
    console.print(f"  personalize: {done} contacts queued (${cost:.2f})")
    return {"in": len(contacts), "out": done, "dropped": 0, "cost_usd": cost}


# --- top-level entrypoints ---------------------------------------------------

def run(db: Database, brief: Brief, depth_override: Optional[int] = None,
        source_limit_override: Optional[int] = None) -> dict:
    """Execute the full staged cascade. Idempotent and resumable.

    ``seed`` is just ``run`` with a larger source depth; pass depth_override.
    ``source_limit_override`` hard-caps how many companies to source this run —
    useful for a cheap, bounded test (e.g. 3 companies).
    Returns a summary dict of per-stage counts and total cost.
    """
    if source_limit_override is not None:
        source_limit = source_limit_override
    else:
        source_limit = compute_source_limit(db, brief, depth_override)
    console.print(f"[bold]Running pipeline for '{brief.industry}'[/bold] "
                  f"(source target: {source_limit} companies)")

    summary: dict[str, dict] = {}
    summary["source"] = _stage(db, brief.industry, "source",
                               lambda: stage_source(db, brief, source_limit))
    summary["validate"] = _stage(db, brief.industry, "validate",
                                 lambda: stage_validate(db, brief))
    summary["jobs"] = _stage(db, brief.industry, "jobs",
                             lambda: stage_jobs(db, brief))
    summary["news"] = _stage(db, brief.industry, "news",
                             lambda: stage_news(db, brief))
    summary["enrich"] = _stage(db, brief.industry, "enrich",
                               lambda: stage_enrich(db, brief))
    summary["personalize"] = _stage(db, brief.industry, "personalize",
                                    lambda: stage_personalize(db, brief))

    total_cost = sum(s.get("cost_usd", 0.0) for s in summary.values())
    depth = reservoir_depth_days(db, brief)
    console.print(f"[green]Done.[/green] Reservoir depth: {depth:.1f} days. "
                  f"Run cost: ${total_cost:.2f}")

    # Write human-readable CSV snapshots of the post-run state.
    written = export_brief(db, brief.industry)
    console.print(f"[dim]CSV: {written['companies']}, {written['contacts']}, "
                  f"{written['runs']}[/dim]")

    summary["_total_cost_usd"] = total_cost  # type: ignore[assignment]
    return summary


def stage_news_nonblocking(db: Database, brief: Brief) -> dict:
    """Like stage_news, but never drops a company — attaches a signal_summary
    when one is found and leaves the company QUALIFIED either way. Used for
    ingested lists where we want every company's contacts, signal or not."""
    companies = db.companies_by_status(brief.industry, CompanyStatus.QUALIFIED)
    found = 0
    cost = 0.0
    for c in companies:
        if c.signal_summary:
            continue  # already researched
        result = signals_news.research_news(c, brief)
        cost += result.get("cost_usd", 0.0)
        if result["passed"]:
            db.update_company(c.domain, signal_summary=result["summary"])
            found += 1
    console.print(f"  news (non-blocking): {found}/{len(companies)} got a signal "
                  f"(${cost:.2f})")
    return {"in": len(companies), "out": found, "dropped": 0, "cost_usd": cost}


def run_ingested(db: Database, brief: Brief, use_signals: bool = False) -> dict:
    """Process pre-qualified (ingested) companies: optional non-blocking news →
    enrich → personalize/queue. No source/validate/jobs gates — the list is
    already validated, and we keep every company to maximize contacts."""
    console.print(f"[bold]Processing ingested companies for '{brief.industry}'[/bold] "
                  f"(signals={'on' if use_signals else 'off'})")
    summary: dict[str, dict] = {}
    if use_signals:
        summary["news"] = _stage(db, brief.industry, "news",
                                 lambda: stage_news_nonblocking(db, brief))
    summary["enrich"] = _stage(db, brief.industry, "enrich",
                               lambda: stage_enrich(db, brief))
    summary["personalize"] = _stage(db, brief.industry, "personalize",
                                    lambda: stage_personalize(db, brief))

    total_cost = sum(s.get("cost_usd", 0.0) for s in summary.values())
    queued = db.reservoir_count(brief.industry)
    console.print(f"[green]Done.[/green] {queued} contacts queued. "
                  f"Cost: ${total_cost:.2f}")
    written = export_brief(db, brief.industry)
    console.print(f"[dim]CSV: {written['companies']}, {written['contacts']}[/dim]")
    summary["_total_cost_usd"] = total_cost  # type: ignore[assignment]
    return summary


def load(db: Database, brief: Brief, quota: Optional[int] = None) -> dict:
    """Drain up to ``quota`` QUEUED contacts into Smartlead.

    This is the ONLY function that sends. Freshest-signal-first. Splits contacts
    by hook (cell) into one campaign each, applies workspace suppression, pushes
    leads with tracking off, then marks them LOADED. Respects daily_quota.
    """
    quota = quota or brief.daily_quota
    contacts = db.contacts_by_status(brief.industry, ContactStatus.QUEUED, limit=quota)
    if not contacts:
        console.print("[yellow]Reservoir empty — nothing to load.[/yellow]")
        return {"loaded": 0, "campaigns": 0}

    # Apply suppression to the workspace before any push.
    supp_keys = db.all_suppression_keys()
    if supp_keys:
        try:
            smartlead.apply_suppression(supp_keys)
        except Exception as exc:
            console.print(f"[yellow]Suppression sync warning:[/yellow] {exc}")

    # Split by cell (hook).
    by_cell: dict[str, list] = {}
    for c in contacts:
        by_cell.setdefault(c.cell or "default", []).append(c)

    total_loaded = 0
    for cell, cell_contacts in by_cell.items():
        # Final per-contact suppression guard (single-touch).
        sendable = [
            c for c in cell_contacts
            if not db.is_suppressed(c.email) and not db.is_suppressed(c.company_domain)
        ]
        if not sendable:
            continue
        campaign_id = smartlead.ensure_campaign(brief, cell)
        pushed = smartlead.push_leads(campaign_id, sendable)
        for c in sendable:
            db.update_contact(c.email, status=ContactStatus.LOADED,
                              loaded_at=now_iso())
            db.add_event(c.email, "sent", {"campaign_id": campaign_id, "cell": cell})
        total_loaded += pushed
        console.print(f"  cell '{cell}': pushed {pushed} leads → campaign {campaign_id}")

    console.print(f"[green]Loaded {total_loaded} leads[/green] across "
                  f"{len(by_cell)} campaign(s).")

    written = export_brief(db, brief.industry)
    console.print(f"[dim]CSV: {written['contacts']}[/dim]")
    return {"loaded": total_loaded, "campaigns": len(by_cell)}
