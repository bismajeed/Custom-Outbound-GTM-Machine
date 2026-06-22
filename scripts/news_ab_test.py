"""A/B the news-signal stage: Option B (Haiku + Claude agentic web_search) vs
Option C (free DuckDuckGo search snippets + Haiku judgment). Quality + cost.

One-off experiment. Reuses the production prompt builders from signals_news so
Option B is apples-to-apples with what runs today (just on Haiku, capped to 3
searches). Run: python scripts/news_ab_test.py
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import anthropic

from outbound.brief import load_brief
from outbound.models import Company
from outbound.pipeline import signals_news as sn

HAIKU = "claude-haiku-4-5-20251001"
IN_RATE, OUT_RATE, SEARCH_RATE = 0.80, 4.00, 10.00  # $/Mtok in, out; $/1k searches

# 5 real companies pulled from the DB (name, domain).
COMPANIES = [
    ("Golisano Children’s Hospital", "urmc.rochester.edu"),
    ("Professional Physical Therapy", "professionalpt.com"),
    ("Candela Medical", "candelamedical.com"),
    ("GoFundMe", "gofundme.com"),
    ("Millennium Physician Group", "millenniumphysician.com"),
]


def load_env(p=".env"):
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _haiku_cost(usage) -> float:
    n_in = getattr(usage, "input_tokens", 0) or 0
    n_out = getattr(usage, "output_tokens", 0) or 0
    n_search = sn._web_search_count(usage)
    return n_in / 1e6 * IN_RATE + n_out / 1e6 * OUT_RATE + n_search / 1000 * SEARCH_RATE


def _pick_signal(parsed, cutoff):
    sigs = [s for s in (parsed.get("signals") or [])
            if isinstance(s, dict) and sn._is_meaningful(s, cutoff)]
    if not sigs:
        return None
    sigs.sort(key=lambda s: -int(s.get("specificity_score", 0) or 0))
    return sigs[0]


# ---- Option B: Haiku + Claude agentic web_search (capped to 3) --------------

def option_b(client, brief, company, today, cutoff):
    sys_prompt = sn._build_system_prompt(brief, today)
    user_prompt = sn._build_user_prompt(company)
    t0 = time.time()
    resp = client.messages.create(
        model=HAIKU, max_tokens=1500, system=sys_prompt,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": user_prompt}],
    )
    dt = time.time() - t0
    cost = _haiku_cost(resp.usage)
    try:
        parsed = sn._parse_json_object(sn._extract_text(resp))
    except Exception:
        return {"signal": None, "cost": cost, "secs": dt, "searches": sn._web_search_count(resp.usage)}
    return {"signal": _pick_signal(parsed, cutoff), "cost": cost, "secs": dt,
            "searches": sn._web_search_count(resp.usage)}


# ---- Option C: free DuckDuckGo snippets + Haiku judgment --------------------

def ddg_snippets(company, max_results=8):
    from ddgs import DDGS
    out = []
    queries = [f"{company.name} {company.city} news 2026",
               f"{company.name} expansion OR acquisition OR hiring 2026"]
    with DDGS() as d:
        for q in queries:
            try:
                for r in d.text(q, max_results=max_results):
                    out.append({"title": r.get("title", ""), "url": r.get("href", ""),
                                "snippet": r.get("body", "")})
            except Exception:
                continue
    # dedup by url
    seen, uniq = set(), []
    for r in out:
        if r["url"] and r["url"] not in seen:
            seen.add(r["url"]); uniq.append(r)
    return uniq[:12]


def option_c(client, brief, company, today, cutoff):
    t0 = time.time()
    snippets = ddg_snippets(company)
    if not snippets:
        return {"signal": None, "cost": 0.0, "secs": time.time() - t0, "n_snip": 0}
    sys_prompt = sn._build_system_prompt(brief, today).replace(
        "Use web search.", "Judge ONLY the search results provided below.")
    blob = "\n".join(f"- {s['title']} | {s['url']}\n  {s['snippet']}" for s in snippets)
    user_prompt = (
        f"{sn._build_user_prompt(company)}\n\nDo NOT call any tool. Judge ONLY these "
        f"search results (use their real URLs as source_url):\n{blob}"
    )
    resp = client.messages.create(
        model=HAIKU, max_tokens=1200, system=sys_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    cost = _haiku_cost(resp.usage)
    try:
        parsed = sn._parse_json_object(sn._extract_text(resp))
    except Exception:
        return {"signal": None, "cost": cost, "secs": time.time() - t0, "n_snip": len(snippets)}
    return {"signal": _pick_signal(parsed, cutoff), "cost": cost,
            "secs": time.time() - t0, "n_snip": len(snippets)}


def _fmt(res):
    s = res.get("signal")
    if not s:
        return "— no qualifying signal"
    return (f"[{s.get('signal_type')}] spec={s.get('specificity_score')} "
            f"{(s.get('summary') or '')[:90]}")


def main():
    load_env()
    brief = load_brief("healthcare-admin")
    today = datetime.now(timezone.utc)
    cutoff = today.replace() - __import__("datetime").timedelta(days=brief.news_lookback_days)
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    tot_b = tot_c = 0.0
    hit_b = hit_c = 0
    for name, domain in COMPANIES:
        c = Company(domain=domain, name=name, brief="healthcare-admin")
        print("\n" + "=" * 78)
        print(f"{name}  ({domain})")
        b = option_b(client, brief, c, today, cutoff)
        tot_b += b["cost"]; hit_b += 1 if b["signal"] else 0
        print(f"  B (Haiku+agentic, {b.get('searches','?')} searches, {b['secs']:.0f}s, "
              f"${b['cost']:.4f}): {_fmt(b)}")
        cc = option_c(client, brief, c, today, cutoff)
        tot_c += cc["cost"]; hit_c += 1 if cc["signal"] else 0
        print(f"  C (DDG+Haiku, {cc.get('n_snip','?')} snippets, {cc['secs']:.0f}s, "
              f"${cc['cost']:.4f}): {_fmt(cc)}")

    n = len(COMPANIES)
    print("\n" + "=" * 78)
    print("SUMMARY (5 companies)")
    print(f"  Option B (Haiku + agentic search): {hit_b}/{n} signals, ${tot_b:.4f} "
          f"(${tot_b/n:.4f}/co  ->  ${tot_b/n*3000:,.0f} for 3,000)")
    print(f"  Option C (DDG + Haiku snippets):   {hit_c}/{n} signals, ${tot_c:.4f} "
          f"(${tot_c/n:.4f}/co  ->  ${tot_c/n*3000:,.0f} for 3,000)")


if __name__ == "__main__":
    main()
