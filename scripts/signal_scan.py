"""Full signal scan over a brief's clean pool — NO enrichment (no Apollo credits).

For every company: technology signal (precomputed per-tool), job signal (scrape),
news signal (DuckDuckGo+Haiku). Writes a per-company CSV showing which signals were
found (or "no signal -> free_implementation"), persists the result to the DB so the
companies are ready for enrichment next, and reports total cost.

Parallelized with threads (network-bound) so ~1,200 companies finish in ~30-60 min.

Usage: python scripts/signal_scan.py [industry] [limit] [workers]
Writes: output/<industry>/company_signals.csv
"""

from __future__ import annotations

import csv
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()

from outbound.brief import load_brief                       # noqa: E402
from outbound.db import Database                            # noqa: E402
from outbound.models import CompanyStatus                   # noqa: E402
from outbound.sources import apollo                         # noqa: E402
from outbound.pipeline import signals_jobs, signals_news    # noqa: E402
from outbound.pipeline import validate as validate_mod      # noqa: E402

_print_lock = threading.Lock()
_done = 0


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def scan_company(c, brief, tech_map, total):
    """Return a result dict for one company. Network only — no DB writes."""
    global _done
    res = {
        "domain": c.domain, "name": c.name, "city": c.city, "state": c.state,
        "founded_year": c.founded_year or "",
        "tech_signal": "; ".join(tech_map.get(c.domain, [])),
        "job_signal": "", "news_signal": "", "news_cost": 0.0, "invalid": False,
    }
    try:
        # No validation gate: concurrent DNS checks produced false negatives that
        # wrongly dropped real companies. The job scraper already handles dead domains
        # gracefully (returns no jobs), so we scan everyone.
        passed, ev = signals_jobs.has_job_signal(c, brief)
        if passed:
            res["job_signal"] = ev
        # News on EVERY company (including tech-signal ones) — a recent, specific
        # event may beat "uses Procore" as a first line. News uses DuckDuckGo, not
        # the company's domain, so it is unaffected by DNS concurrency.
        nr = signals_news.research_news(c, brief)
        res["news_cost"] = nr.get("cost_usd", 0.0)
        if nr["passed"]:
            res["news_signal"] = nr["summary"]
    except Exception as exc:  # never let one company kill the batch
        res["error"] = str(exc)[:120]
    with _print_lock:
        _done += 1
        d = _done
    flags = [f for f, k in (("TECH", "tech_signal"), ("JOB", "job_signal"),
                            ("NEWS", "news_signal")) if res[k]]
    _log(f"[{d}/{total}] {c.name[:38]:38} {'+'.join(flags) or ('INVALID' if res['invalid'] else 'none')}")
    return res


def main() -> None:
    industry = sys.argv[1] if len(sys.argv) > 1 else "construction"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 1400
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    brief = load_brief(industry)

    _log(f"Sourcing pool for '{industry}' (limit {limit})…")
    companies = apollo.search_companies(brief, limit)
    _log(f"  pool: {len(companies)} companies")
    _log("Tagging technologies (per-tool queries)…")
    tech_map = apollo.tag_technologies(brief)
    _log(f"  technology signals: {len(tech_map)} companies use a tool")

    total = len(companies)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(scan_company, c, brief, tech_map, total) for c in companies]
        for f in as_completed(futs):
            results.append(f.result())

    # Tally + classify. No company is dropped now.
    news_cost = sum(r["news_cost"] for r in results)
    valid = results
    n_tech = sum(1 for r in valid if r["tech_signal"])
    n_job = sum(1 for r in valid if r["job_signal"])
    n_news = sum(1 for r in valid if r["news_signal"])
    n_any = sum(1 for r in valid if r["tech_signal"] or r["job_signal"] or r["news_signal"])

    # Write CSV.
    out_dir = os.path.join("output", industry)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "company_signals.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["domain", "name", "city", "state", "founded_year",
                    "tech_signal", "job_signal", "news_signal",
                    "has_signal", "segment", "signal_summary"])
        for r in sorted(valid, key=lambda x: x["domain"]):
            best = (r["news_signal"] or r["job_signal"]
                    or (f"tech_adoption: uses {r['tech_signal']}" if r["tech_signal"] else ""))
            has = bool(best)
            seg = "signal" if has else "free_implementation"
            w.writerow([r["domain"], r["name"], r["city"], r["state"], r["founded_year"],
                        r["tech_signal"], r["job_signal"], r["news_signal"],
                        "yes" if has else "no", seg,
                        best or "no signal found -> free_implementation"])

    # Persist to DB so the companies are ready for enrichment next.
    db = Database(os.environ.get("DATABASE_URL", "sqlite:///outbound.db"))
    db.init()
    for c in companies:
        r = next((x for x in valid if x["domain"] == c.domain), None)
        if r is None:
            continue
        best = (r["news_signal"] or r["job_signal"]
                or (f"tech_adoption: uses {r['tech_signal']}" if r["tech_signal"] else ""))
        c.technologies = tech_map.get(c.domain, [])
        c.signal_summary = best or None
        c.status = CompanyStatus.QUALIFIED
        if not db.insert_company(c):
            db.update_company(c.domain, status=CompanyStatus.QUALIFIED,
                              technologies=json.dumps(c.technologies),
                              signal_summary=c.signal_summary)

    _log("\n=== SIGNAL SCAN COMPLETE ===")
    _log(f"valid companies:        {len(valid)} (of {total} sourced)")
    _log(f"  technology signal:    {n_tech}")
    _log(f"  job signal:           {n_job}")
    _log(f"  news signal:          {n_news}")
    _log(f"  ANY signal (-> signal segment):        {n_any}")
    _log(f"  NO signal (-> free_implementation):    {len(valid) - n_any}")
    _log(f"news cost (Haiku): ${news_cost:.2f}  | enrichment cost so far: $0 (no Apollo reveals)")
    _log(f"CSV: {path}")


if __name__ == "__main__":
    main()
