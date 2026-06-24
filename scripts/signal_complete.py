"""Complete the signal scan WITHOUT redoing finished work.

Reuses the existing output/<industry>/company_signals.csv (companies already
scanned) and only does what's missing:
  * FULL scan (job + news) on companies that were wrongly dropped (not in the CSV).
  * NEWS-only on tech/job companies whose news lookup was skipped earlier.
No validation gate (dead domains just yield no job signal). Best signal for
messaging: news > job > tech.

Usage: python scripts/signal_complete.py [industry] [workers]
Writes: output/<industry>/company_signals.csv  (rewritten with the full set)
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

_lock = threading.Lock()
_done = 0
_total = 0


def _log(m):
    with _lock:
        print(m, flush=True)


def main() -> None:
    industry = sys.argv[1] if len(sys.argv) > 1 else "construction"
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    brief = load_brief(industry)
    csv_path = os.path.join("output", industry, "company_signals.csv")

    existing = {}
    if os.path.exists(csv_path):
        for r in csv.DictReader(open(csv_path)):
            existing[r["domain"]] = r
    _log(f"existing scanned companies: {len(existing)}")

    companies = apollo.search_companies(brief, 1400)
    bydom = {c.domain: c for c in companies}
    _log(f"pool: {len(bydom)} companies")
    tech_map = apollo.tag_technologies(brief)
    _log(f"tech-tagged: {len(tech_map)}")

    need_full, need_news = [], []
    for dom, c in bydom.items():
        r = existing.get(dom)
        if r is None:
            need_full.append(c)                       # wrongly dropped -> full scan
        elif (r["tech_signal"] or r["job_signal"]) and not r["news_signal"]:
            need_news.append((c, r))                  # had tech/job, news was skipped
    global _total
    _total = len(need_full) + len(need_news)
    _log(f"to FULL-scan (wrongly dropped): {len(need_full)}")
    _log(f"to NEWS-check (skipped earlier): {len(need_news)}")
    _log(f"already complete (kept as-is): {len(existing) - len(need_news)}\n")

    def full_scan(c):
        global _done
        res = {"domain": c.domain, "name": c.name, "city": c.city, "state": c.state,
               "founded_year": c.founded_year or "",
               "tech_signal": "; ".join(tech_map.get(c.domain, [])),
               "job_signal": "", "news_signal": "", "news_cost": 0.0}
        try:
            p, ev = signals_jobs.has_job_signal(c, brief)
            if p:
                res["job_signal"] = ev
            nr = signals_news.research_news(c, brief)
            res["news_cost"] = nr.get("cost_usd", 0.0)
            if nr["passed"]:
                res["news_signal"] = nr["summary"]
        except Exception as exc:
            res["error"] = str(exc)[:80]
        with _lock:
            _done += 1
            d = _done
        flags = [f for f, k in (("T", "tech_signal"), ("J", "job_signal"),
                                ("N", "news_signal")) if res[k]]
        _log(f"[{d}/{_total} full] {c.name[:34]:34} {'+'.join(flags) or '-'}")
        return res

    def news_only(item):
        global _done
        c, r = item
        out = dict(r)
        out["news_cost"] = 0.0
        try:
            nr = signals_news.research_news(c, brief)
            out["news_cost"] = nr.get("cost_usd", 0.0)
            if nr["passed"]:
                out["news_signal"] = nr["summary"]
        except Exception as exc:
            out["error"] = str(exc)[:80]
        with _lock:
            _done += 1
            d = _done
        _log(f"[{d}/{_total} news] {c.name[:34]:34} {'NEWS!' if out['news_signal'] else '-'}")
        return out

    results = dict(existing)  # keep everything already done
    news_cost = 0.0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(full_scan, c) for c in need_full]
        futs += [ex.submit(news_only, it) for it in need_news]
        for f in as_completed(futs):
            r = f.result()
            news_cost += r.get("news_cost", 0.0)
            results[r["domain"]] = r

    # Rewrite the full CSV + persist to DB (best signal: news > job > tech).
    db = Database(os.environ.get("DATABASE_URL", "sqlite:///outbound.db"))
    db.init()
    n_tech = n_job = n_news = n_any = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["domain", "name", "city", "state", "founded_year",
                    "tech_signal", "job_signal", "news_signal",
                    "has_signal", "segment", "signal_summary"])
        for dom in sorted(results):
            r = results[dom]
            tech, job, news = r.get("tech_signal", ""), r.get("job_signal", ""), r.get("news_signal", "")
            n_tech += bool(tech); n_job += bool(job); n_news += bool(news)
            best = news or job or (f"tech_adoption: uses {tech}" if tech else "")
            has = bool(best)
            n_any += has
            w.writerow([dom, r.get("name", ""), r.get("city", ""), r.get("state", ""),
                        r.get("founded_year", ""), tech, job, news,
                        "yes" if has else "no",
                        "signal" if has else "free_implementation",
                        best or "no signal found -> free_implementation"])
            c = bydom.get(dom)
            if c:
                c.technologies = tech_map.get(dom, [])
                c.signal_summary = best or None
                c.status = CompanyStatus.QUALIFIED
                if not db.insert_company(c):
                    db.update_company(dom, status=CompanyStatus.QUALIFIED,
                                      technologies=json.dumps(c.technologies),
                                      signal_summary=c.signal_summary)

    total = len(results)
    _log("\n=== SIGNAL SCAN COMPLETE (full set) ===")
    _log(f"total companies:                      {total}")
    _log(f"  technology signal:                  {n_tech}")
    _log(f"  job signal:                         {n_job}")
    _log(f"  news signal:                        {n_news}")
    _log(f"  ANY signal (-> signal):             {n_any}")
    _log(f"  NO signal (-> free_implementation): {total - n_any}")
    _log(f"news cost this pass: ${news_cost:.2f}")
    _log(f"CSV: {csv_path}")


if __name__ == "__main__":
    main()
