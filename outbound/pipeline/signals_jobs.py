"""Job-posting signal — the cheap cascade gate.

Ported from Method 6 (legacy job-signal extractor). Job descriptions are public
confessions of company strategy. This module scrapes a company's career pages
and the common ATS boards (Greenhouse, Lever, Indeed, OurCareerPages), filters
to recent postings, and checks them against the brief's job keywords.

It is deliberately scrape-only (no LLM) so it stays cheap: it runs on every
VALID company, and only companies that pass it reach the paid news stage.

Contract: ``has_job_signal(company, brief) -> (passed: bool, evidence: str)``
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from ..models import Brief, Company

JOB_RECENCY_DAYS = 120
MAX_JOBS_PER_COMPANY = 25

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

CAREER_PATH_VARIANTS = [
    "/careers", "/jobs", "/careers/openings", "/about/careers",
    "/work-with-us", "/join-us", "/open-positions", "/career-opportunities",
]

_session = requests.Session()
_session.headers.update(_HEADERS)


def _cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=JOB_RECENCY_DAYS)


def _get(url: str, timeout: int = 15):
    try:
        return _session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return None


def _get_soup(url: str, timeout: int = 15):
    r = _get(url, timeout=timeout)
    if r is None or r.status_code != 200:
        return None, r
    return BeautifulSoup(r.text, "lxml"), r


def _parse_date(raw):
    if not raw:
        return None
    try:
        dt = dateparser.parse(str(raw).strip(), fuzzy=True)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_within_window(dt) -> bool:
    if dt is None:
        return True  # unknown date → keep; keyword match still required
    return dt >= _cutoff()


# --- Scrapers (condensed from Method 6) -------------------------------------

def _scrape_career_page(domain: str):
    for path in CAREER_PATH_VARIANTS:
        url = f"https://{domain}{path}"
        soup, resp = _get_soup(url)
        if soup is None:
            continue
        page_text = resp.text
        ccp_match = re.search(r"CCPCode\s*:\s*[\"']([^\"']+)[\"']", page_text)
        if ccp_match or "ourcareerpages.com" in page_text.lower():
            return [], url, f"ourcareerpages:{ccp_match.group(1) if ccp_match else ''}"

        jobs = []
        for elem in soup.find_all(
            ["div", "li", "article"],
            class_=re.compile(r"job|position|opening|listing|role", re.I),
        ):
            title_elem = (
                elem.find(["h2", "h3", "h4", "a", "span"],
                          class_=re.compile(r"title|name|role", re.I))
                or elem.find(["h2", "h3", "h4"])
            )
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)
            if title and len(title) > 4:
                link_elem = elem.find("a", href=True)
                link = urljoin(url, link_elem["href"]) if link_elem else url
                date_elem = elem.find(string=re.compile(r"\d{4}|\bago\b|posted", re.I))
                jobs.append({
                    "title": title,
                    "posted_date_raw": date_elem.strip() if date_elem else None,
                    "source_url": link,
                    "source": "career_page",
                })
        if jobs:
            return jobs, url, "career_page"
    return [], None, None


def _scrape_ourcareerpages(ccp_code: str):
    if not ccp_code:
        return []
    api_url = (
        f"https://jobs.ourcareerpages.com/WebServices/ccp_jobs.aspx"
        f"?AutoGenerate=yes&GroupBy=&CCPCode={ccp_code}&InAccountID=0"
        f"&ElementID=BDHRJobListings&JobOrderBy="
    )
    r = _get(api_url)
    if r is None or r.status_code != 200:
        return []
    m = re.search(r"ccpInfo:\s*(\{.+\})\s*\};\s*bdhr", r.text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    jobs = []
    for category in data.get("CategoryList", []):
        for j in category.get("JobList", []):
            dt = None
            ms_match = re.search(r"/Date\((\d+)\)/", j.get("LastUpdateDate", ""))
            if ms_match:
                dt = datetime.fromtimestamp(int(ms_match.group(1)) / 1000,
                                            tz=timezone.utc)
            jobs.append({
                "title": j.get("JobTitle", "").strip(),
                "posted_date_raw": dt.isoformat() if dt else "",
                "source_url": f"https://jobs.ourcareerpages.com/job/{j.get('ID')}/{ccp_code}",
                "source": "ourcareerpages",
            })
    return jobs


def _scrape_board(base_url: str, name: str, source: str):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    r = _get(f"{base_url}/{base}")
    if r is None or r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    jobs = []
    for row in soup.find_all(["div"], class_=re.compile(r"opening|posting|job", re.I)):
        title_el = (row.find(["a", "h3", "h5", "span"],
                             class_=re.compile(r"title|name", re.I))
                    or row.find("a"))
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if title:
            href = title_el.get("href", "")
            jobs.append({
                "title": title,
                "posted_date_raw": None,
                "source_url": urljoin(f"{base_url}/{base}", href) if href else base_url,
                "source": source,
            })
    return jobs


def _discover_jobs(company: Company) -> list[dict]:
    """Scrape job listings across sources; dedup by title."""
    all_jobs: list[dict] = []
    domain = company.domain

    career_jobs, career_url, career_type = _scrape_career_page(domain)
    if career_type and career_type.startswith("ourcareerpages:"):
        all_jobs.extend(_scrape_ourcareerpages(career_type.split(":", 1)[1]))
    else:
        all_jobs.extend(career_jobs)

    all_jobs.extend(_scrape_board("https://boards.greenhouse.io", company.name,
                                  "greenhouse"))
    all_jobs.extend(_scrape_board("https://jobs.lever.co", company.name, "lever"))

    seen: set[str] = set()
    unique: list[dict] = []
    for j in all_jobs:
        key = j["title"].lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(j)
    return unique[:MAX_JOBS_PER_COMPANY]


def has_job_signal(company: Company, brief: Brief) -> tuple[bool, str]:
    """Cheap gate: is the company actively hiring (recent postings)?

    Passes if the company has ANY recent (in-window) job posting — active hiring
    is itself a growth signal. Postings whose titles match the brief's keywords
    are highlighted in the evidence, but a keyword match is no longer required:
    requiring it dropped genuinely good prospects whose openings were relevant
    but worded differently. Returns (passed, one-line evidence).
    """
    keywords = [
        k.lower()
        for k in brief.signals.get("job_postings", {}).get("keywords_any", [])
    ]

    jobs = _discover_jobs(company)
    if not jobs:
        return False, "no job postings found"

    recent: list[str] = []
    keyword_hits: list[str] = []
    for j in jobs:
        dt = _parse_date(j.get("posted_date_raw"))
        if not _is_within_window(dt):
            continue
        title = j["title"]
        recent.append(title)
        if keywords and any(kw in title.lower() for kw in keywords):
            keyword_hits.append(title)

    if not recent:
        return False, f"{len(jobs)} postings found, none within window"

    # Lead the evidence with keyword-matched roles when present, else any roles.
    highlight = keyword_hits or recent
    sample = ", ".join(highlight[:3])
    evidence = (f"{len(recent)} recent postings, {len(keyword_hits)} keyword-matched "
                f"(e.g. {sample})")
    return True, evidence
