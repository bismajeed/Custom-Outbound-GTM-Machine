"""News / web-research signal — the qualification stage.

Finds ONE recent, specific, dated signal about a company usable as a personalized
first line. Date-strict to the brief's lookback window. Conservative: a missing
signal is fine, a wrong one is not.

How it works (deliberately cheap, per the cost mandate):
  1. Pull the first results from DuckDuckGo (free, no API key) — a couple of
     queries built from the company name, location, and the brief's news themes.
  2. Hand those results (title + snippet + url + date) to Claude **Haiku** and ask
     it to extract at most one qualifying signal as strict JSON. No web_search
     tool, so there is no per-search fee — only Haiku's token cost.

This drops the spend from ~$0.06/company (Claude server-side web_search, $10/1k
searches) to ~$0.003/company — roughly $10-12 for 3k companies. DuckDuckGo does the
finding for free; Haiku only summarizes.

Conservatism is unchanged: Haiku may only cite a URL that appears in the supplied
results (it cannot invent a source), signals older than the lookback window are
discarded, and a specificity floor still applies.

Contract (unchanged, so the runner is untouched):
``research_news(company, brief) -> {passed, summary, first_line, cost_usd, signal}``
``news_signal(company, brief) -> (passed, one_line_summary)``
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..models import Brief, Company

# Haiku summarizes the DuckDuckGo results. No web_search tool — DuckDuckGo does the
# finding for free — so the only cost is Haiku's tokens.
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1200

PRICE_INPUT_PER_MTOK = 0.80
PRICE_OUTPUT_PER_MTOK = 4.00

# DuckDuckGo result limits. Kept small: the boss's mandate is "just pull the first
# results" — depth comes from a couple of targeted queries, not from paging deep.
DDG_RESULTS_PER_QUERY = 6
DDG_MAX_QUERIES = 3
DDG_MAX_RESULTS = 12          # hard cap on snippets handed to Haiku
DDG_THEME_LIMIT = 2           # how many brief themes to spin into extra queries

MEANINGFUL_SPECIFICITY_MIN = 3

VALID_SIGNAL_TYPES = {
    "project_win", "hire", "tech_adoption", "press_coverage",
    "federal_contract", "license_filing", "blog_post", "other",
}


# --- DuckDuckGo search (free; the "finding" half) ----------------------------

def _build_queries(company: Company, brief: Brief) -> list[str]:
    """A few targeted queries: identity-anchored first, then theme-anchored."""
    name = (company.name or "").strip()
    if not name:
        return []
    loc = " ".join(p for p in (company.city, company.state) if p).strip()
    queries: list[str] = [f'"{name}" {loc}'.strip()]

    themes = brief.signals.get("news", {}).get("themes_any", []) or []
    for theme in themes[:DDG_THEME_LIMIT]:
        queries.append(f'"{name}" {theme}'.strip())

    # Dedup, preserve order, cap.
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:DDG_MAX_QUERIES]


def _ddg_results(company: Company, brief: Brief) -> list[dict]:
    """Pull first results from DuckDuckGo across a couple of queries.

    Uses both the dated news index and general text results, dedupes by URL, and
    caps the total. Never raises — a search failure just yields fewer (or no)
    results, which routes the company to free_implementation downstream.
    """
    try:
        from ddgs import DDGS
    except Exception:
        return []

    out: list[dict] = []
    seen: set[str] = set()

    def _add(raw: dict, dated: bool) -> None:
        url = (raw.get("href") or raw.get("url") or raw.get("link") or "").strip()
        if not url or url in seen:
            return
        seen.add(url)
        out.append({
            "title": (raw.get("title") or "").strip(),
            "body": (raw.get("body") or raw.get("excerpt") or "").strip(),
            "url": url,
            # news() supplies an ISO date + source; text() does not.
            "date": (raw.get("date") or "").strip() if dated else "",
            "source": (raw.get("source") or "").strip(),
        })

    for query in _build_queries(company, brief):
        if len(out) >= DDG_MAX_RESULTS:
            break
        # Dated news first — it carries event dates we can hard-filter on.
        try:
            for r in DDGS().news(query, max_results=DDG_RESULTS_PER_QUERY) or []:
                _add(r, dated=True)
        except Exception:
            pass
        try:
            for r in DDGS().text(query, max_results=DDG_RESULTS_PER_QUERY) or []:
                _add(r, dated=False)
        except Exception:
            pass

    return out[:DDG_MAX_RESULTS]


def _format_results(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        date = f" | date: {r['date']}" if r.get("date") else ""
        src = f" | source: {r['source']}" if r.get("source") else ""
        lines.append(
            f"[{i}] {r['title']}{date}{src}\n    url: {r['url']}\n    {r['body']}"
        )
    return "\n\n".join(lines)


# --- Prompt construction (Haiku summarizes; it does NOT search) ---------------

def _build_system_prompt(brief: Brief, today: datetime) -> str:
    today_str = today.strftime("%Y-%m-%d")
    lookback = brief.news_lookback_days
    cutoff_str = (today - timedelta(days=lookback)).strftime("%Y-%m-%d")
    titles = ", ".join(brief.contact_filters.get("titles_any", [])[:5]) or "decision makers"
    themes = brief.signals.get("news", {}).get("themes_any", [])
    themes_str = ", ".join(str(t) for t in themes) if themes else "any material business event"

    return f"""\
You are a research analyst for a B2B outbound campaign in the {brief.industry} industry. \
You are given a set of web search results about a specific company. Your job is to pick ONE \
genuinely useful, recent signal a sales rep can use to write a personalized first line in a \
cold email. You do NOT have a search tool — work only from the results provided.

WHO RECEIVES THE EMAIL
The recipient holds a role such as: {titles}. A good signal proves we actually paid \
attention to THEIR company. Relevant themes for this campaign: {themes_str}.

TODAY'S DATE: {today_str}
HARD DATE FILTER: Only include a signal if its event date is on or after {cutoff_str} \
(within the last {lookback} days). If a result has no date and you cannot establish from its \
text that the event is recent, treat the date as unknown and set date_confidence "low". If a \
signal is clearly older than {cutoff_str}, DISCARD it.

WHAT TO LOOK FOR (rough priority): recent project wins / awards / expansions; named \
technology adoption; leadership hires or promotions; trade-press coverage; contract awards; \
fresh content from the company's own news/blog page.

EXCLUDE: boilerplate/About-Us content; generic directory listings; results about a \
different company with a similar name; press releases that just repeat a tagline.

RULES
BE CONSERVATIVE. If you are not confident a fact is true and about THIS exact company, skip \
it. Never invent or embellish. A missing signal is fine; a wrong signal is not.
SOURCE RULE: every signal's source_url MUST be copied verbatim from one of the result urls \
above. NEVER write a url that does not appear in the results. If you cannot, DROP the signal.
DATE HANDLING: event date as YYYY-MM-DD when known (use the result's date when given); \
"fuzzy" with date_confidence "low" if approximate. date_confidence is high|medium|low.
SPECIFICITY SCORE (1-5): 1 = generic; 3 = a real but broad event; 5 = highly specific with \
names/places/dates. Score honestly.
Return AT MOST 1 signal. Prefer one strong signal over several weak ones.
suggested_first_line: single lowercase sentence, no exclamation marks, no emojis, under 25 \
words, referencing the specific event detail (not just the signal type).
LOWERCASE RULE applies ONLY to suggested_first_line.

OUTPUT FORMAT — STRICT JSON ONLY, no markdown or commentary. Exactly one object:
{{
  "company_name": "string",
  "company_domain": "string",
  "signals": [
    {{
      "signal_type": "project_win | hire | tech_adoption | press_coverage | federal_contract | license_filing | blog_post | other",
      "summary": "one-sentence factual statement",
      "source_url": "string (must match a result url above)",
      "source_title": "string",
      "date": "YYYY-MM-DD or 'fuzzy'",
      "date_confidence": "high | medium | low",
      "specificity_score": 1,
      "suggested_first_line": "lowercase casual one-sentence opener"
    }}
  ],
  "no_signals_found": false,
  "search_notes": "which result you chose and why, or why nothing qualified"
}}
If nothing qualifies, return an empty signals array and set no_signals_found to true."""


def _build_user_prompt(company: Company, results: list[dict]) -> str:
    return f"""\
Pick a signal for this company from the search results below. Confirm the result is about \
this exact company (name + location) before trusting it. Return strict JSON only.

company_name: {company.name}
company_domain: {company.domain}
company_city: {company.city}
company_state: {company.state}

SEARCH RESULTS:
{_format_results(results)}"""


# --- Anthropic call ----------------------------------------------------------

class _Transient(Exception):
    pass


@retry(
    retry=retry_if_exception_type(_Transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _call_claude(client: anthropic.Anthropic, system_prompt: str, user_prompt: str):
    try:
        return client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except (anthropic.APITimeoutError, anthropic.APIConnectionError,
            anthropic.InternalServerError, anthropic.RateLimitError) as exc:
        raise _Transient(str(exc)) from exc
    except anthropic.APIStatusError as exc:
        if getattr(exc, "status_code", 0) >= 500:
            raise _Transient(str(exc)) from exc
        raise


# --- Parsing -----------------------------------------------------------------

def _extract_text(response) -> str:
    return "\n".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()


def _strip_fences(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return fence.group(1).strip() if fence else text


def _parse_json_object(text: str) -> dict:
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return json.loads(cleaned[start:end + 1])
    raise json.JSONDecodeError("No JSON object found", cleaned, 0)


def _is_meaningful(sig: dict, cutoff: datetime, allowed_urls: set[str]) -> bool:
    try:
        spec = int(sig.get("specificity_score", 0))
    except (TypeError, ValueError):
        spec = 0
    if spec < MEANINGFUL_SPECIFICITY_MIN:
        return False
    url = (sig.get("source_url") or "").strip()
    if not url.lower().startswith("http"):
        return False
    # Anti-hallucination: the cited url must be one we actually handed to Haiku.
    if allowed_urls and url not in allowed_urls:
        return False
    date = (str(sig.get("date") or "")).strip()
    if date and date != "fuzzy":
        try:
            d = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
            if d < cutoff:
                return False
        except (ValueError, AttributeError):
            return False
    return True


def _estimate_cost(usage) -> float:
    n_in = getattr(usage, "input_tokens", 0) or 0
    n_out = getattr(usage, "output_tokens", 0) or 0
    return (n_in / 1_000_000 * PRICE_INPUT_PER_MTOK
            + n_out / 1_000_000 * PRICE_OUTPUT_PER_MTOK)


# --- Public API --------------------------------------------------------------

def research_news(company: Company, brief: Brief) -> dict[str, Any]:
    """Find a recent signal via DuckDuckGo results summarized by Haiku.

    Returns {passed, summary, first_line, cost_usd, signal}. Never raises on a
    search or API failure — returns passed=False with the reason noted, so the run
    stays alive and the company simply routes to free_implementation.
    """
    today = datetime.now(timezone.utc)
    cutoff = today - timedelta(days=brief.news_lookback_days)

    results = _ddg_results(company, brief)
    if not results:
        return {"passed": False, "summary": "no search results", "first_line": "",
                "cost_usd": 0.0, "signal": None}
    allowed_urls = {r["url"] for r in results}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = _build_system_prompt(brief, today)
    user_prompt = _build_user_prompt(company, results)

    try:
        response = _call_claude(client, system_prompt, user_prompt)
    except Exception as exc:  # keep the run alive
        return {"passed": False, "summary": f"news research failed: {exc}",
                "first_line": "", "cost_usd": 0.0, "signal": None}

    cost = _estimate_cost(response.usage)
    raw_text = _extract_text(response)

    try:
        parsed = _parse_json_object(raw_text)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {"passed": False, "summary": "could not parse model output",
                "first_line": "", "cost_usd": cost, "signal": None}

    signals = parsed.get("signals") or []
    meaningful = [
        s for s in signals
        if isinstance(s, dict) and _is_meaningful(s, cutoff, allowed_urls)
    ]
    if not meaningful:
        note = (parsed.get("search_notes") or "no qualifying signal").strip()
        return {"passed": False, "summary": note[:200], "first_line": "",
                "cost_usd": cost, "signal": None}

    def _key(s: dict):
        try:
            spec = int(s.get("specificity_score", 0))
        except (TypeError, ValueError):
            spec = 0
        date = (str(s.get("date") or "")).strip()
        try:
            dval = datetime.fromisoformat(date).timestamp() if date and date != "fuzzy" else 0
        except ValueError:
            dval = 0
        return (-spec, -dval)

    meaningful.sort(key=_key)
    best = meaningful[0]
    stype = best.get("signal_type", "other")
    if stype not in VALID_SIGNAL_TYPES:
        stype = "other"
    summary = f"{stype}: {(best.get('summary') or '').strip()}"
    return {
        "passed": True,
        "summary": summary[:300],
        "first_line": (best.get("suggested_first_line") or "").strip(),
        "cost_usd": cost,
        "signal": best,
    }


def news_signal(company: Company, brief: Brief) -> tuple[bool, str]:
    """Contract wrapper: (passed, one_line_summary)."""
    result = research_news(company, brief)
    return result["passed"], result["summary"]
