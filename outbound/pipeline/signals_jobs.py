"""Job-posting signal — the cheap, scrape-only cascade signal.

Job descriptions are public confessions of company strategy. This module finds a
company's job postings and reads them for two kinds of signal, both defined
per-industry in the brief (the mechanism here is generic):

  * ROLE relevance  — ``signals.job_postings.keywords_any`` matched against the
    posting TITLE (e.g. estimator, preconstruction). "Are they hiring the roles
    that imply our buyer?"
  * GROWTH/intent   — ``signals.job_postings.signal_phrases_any`` matched against
    the posting DESCRIPTION (e.g. "breaking ground", "new facility", "expanding
    into"). These capture *why* they're hiring, and the matched snippet is reused
    verbatim in the email first line ("saw you're staffing up for the new …").

Discovery is ATS-aware. Rather than guessing an ATS slug from the company name
(brittle), we read the company's careers page, discover which ATS it links to,
and pull structured postings — **with descriptions** — from that ATS's JSON API:

  * Greenhouse / Lever — full descriptions via their public JSON APIs.
  * Workday            — best-effort via the CXS JSON endpoint (list + a capped
                         number of description fetches).
  * iCIMS / Taleo      — detected and noted; deep extraction is best-effort only
                         (JS-rendered portals), falling back to inline HTML.

It is deliberately scrape-only (no LLM) so it stays free and runs on every VALID
company. Signals route, they don't gate: a company with no posting simply carries
no job signal and is researched by the (paid) news stage instead.

Contract: ``has_job_signal(company, brief) -> (passed: bool, evidence: str)``
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from ..models import Brief, Company

JOB_RECENCY_DAYS = 120
MAX_JOBS_PER_COMPANY = 40
WORKDAY_DETAIL_CAP = 8          # how many Workday descriptions to fetch (best-effort)
SNIPPET_WIDTH = 180             # chars of context captured around a matched phrase

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

# ATS link patterns scanned out of the careers/home page HTML. Discovery from the
# real page is far more reliable than guessing a slug from the company name.
_ATS_PATTERNS = {
    "greenhouse": re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)",
        re.I),
    "greenhouse_for": re.compile(r"greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", re.I),
    "workday": re.compile(
        r"https?://([a-z0-9][a-z0-9-]*)\.(wd\d+)\.myworkdayjobs\.com/([^\"'\s?#]+)", re.I),
    "icims": re.compile(r"https?://(?:careers-)?([a-z0-9-]+)\.icims\.com", re.I),
    "taleo": re.compile(r"https?://([a-z0-9-]+)\.taleo\.net", re.I),
}

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
        return True  # unknown date → keep; recency can't be confirmed either way
    return dt >= _cutoff()


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    try:
        return BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


# --- ATS discovery -----------------------------------------------------------

def _discover_pages(domain: str) -> tuple[list[tuple[str, BeautifulSoup, str]], list[dict]]:
    """Fetch the homepage + the first reachable careers page. Returns the pages
    (url, soup, html) for ATS scanning, plus any jobs scraped inline from the
    careers-page HTML as a fallback for sites that list roles on-page."""
    pages: list[tuple[str, BeautifulSoup, str]] = []
    inline_jobs: list[dict] = []

    home_soup, home_resp = _get_soup(f"https://{domain}")
    if home_resp is not None and home_soup is not None:
        pages.append((f"https://{domain}", home_soup, home_resp.text))

    for path in CAREER_PATH_VARIANTS:
        url = f"https://{domain}{path}"
        soup, resp = _get_soup(url)
        if soup is None or resp is None:
            continue
        pages.append((url, soup, resp.text))
        # OurCareerPages is an embedded ATS with its own JSON service.
        ccp = re.search(r"CCPCode\s*:\s*[\"']([^\"']+)[\"']", resp.text)
        if ccp or "ourcareerpages.com" in resp.text.lower():
            inline_jobs.extend(_scrape_ourcareerpages(ccp.group(1) if ccp else ""))
        else:
            inline_jobs.extend(_scrape_inline(soup, url))
        break  # first reachable careers page is enough

    return pages, inline_jobs


def _discover_ats(pages: list[tuple[str, BeautifulSoup, str]]) -> dict[str, dict]:
    """Scan page HTML for ATS references; return {ats: {...locator...}}."""
    found: dict[str, dict] = {}
    for _url, _soup, html in pages:
        if not html:
            continue
        for token in (_ATS_PATTERNS["greenhouse"].findall(html)
                      + _ATS_PATTERNS["greenhouse_for"].findall(html)):
            if token and token.lower() not in {"embed", "job_board"}:
                found.setdefault("greenhouse", {"token": token})
        m = _ATS_PATTERNS["lever"].search(html)
        if m:
            found.setdefault("lever", {"token": m.group(1)})
        m = _ATS_PATTERNS["workday"].search(html)
        if m:
            tenant, dc, path = m.group(1), m.group(2), m.group(3)
            # site = last path segment, after any locale like en-US.
            segs = [s for s in path.split("/") if s and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
            if segs:
                found.setdefault("workday", {"tenant": tenant, "dc": dc, "site": segs[-1]})
        m = _ATS_PATTERNS["icims"].search(html)
        if m:
            found.setdefault("icims", {"tenant": m.group(1)})
        m = _ATS_PATTERNS["taleo"].search(html)
        if m:
            found.setdefault("taleo", {"tenant": m.group(1)})
    return found


# --- ATS fetchers (return jobs with descriptions where available) ------------

def _scrape_greenhouse(token: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = _get(url)
    if r is None or r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "title": (j.get("title") or "").strip(),
            "description": _html_to_text(j.get("content") or ""),
            "posted_date_raw": j.get("updated_at") or j.get("first_published") or "",
            "source_url": j.get("absolute_url", ""),
            "source": "greenhouse",
        })
    return jobs


def _scrape_lever(token: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = _get(url)
    if r is None or r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    jobs = []
    for j in data:
        created = j.get("createdAt")
        if isinstance(created, (int, float)):
            dt = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
        else:
            dt = ""
        desc = j.get("descriptionPlain") or _html_to_text(j.get("description") or "")
        jobs.append({
            "title": (j.get("text") or "").strip(),
            "description": desc,
            "posted_date_raw": dt,
            "source_url": j.get("hostedUrl", ""),
            "source": "lever",
        })
    return jobs


def _scrape_workday(tenant: str, dc: str, site: str) -> list[dict]:
    """Best-effort Workday: list postings via the CXS JSON endpoint, then fetch a
    capped number of descriptions. Workday tenants vary, so this is wrapped to
    fail silently and return whatever it can."""
    base = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    try:
        r = _session.post(f"{base}/jobs",
                          json={"appliedFacets": {}, "limit": 20, "offset": 0,
                                "searchText": ""},
                          timeout=15)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    try:
        postings = r.json().get("jobPostings", []) or []
    except ValueError:
        return []
    jobs = []
    for i, p in enumerate(postings):
        ext = p.get("externalPath", "")
        url = f"https://{tenant}.{dc}.myworkdayjobs.com/{site}{ext}" if ext else ""
        desc = ""
        if i < WORKDAY_DETAIL_CAP and ext:
            try:
                d = _session.get(f"{base}{ext}", timeout=12)
                if d.status_code == 200:
                    info = d.json().get("jobPostingInfo", {}) or {}
                    desc = _html_to_text(info.get("jobDescription") or "")
            except (requests.RequestException, ValueError):
                pass
        jobs.append({
            "title": (p.get("title") or "").strip(),
            "description": desc,
            "posted_date_raw": p.get("postedOn", "") or "",
            "source_url": url,
            "source": "workday",
        })
    return jobs


def _scrape_ourcareerpages(ccp_code: str) -> list[dict]:
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
                "description": _html_to_text(j.get("JobDescription", "") or ""),
                "posted_date_raw": dt.isoformat() if dt else "",
                "source_url": f"https://jobs.ourcareerpages.com/job/{j.get('ID')}/{ccp_code}",
                "source": "ourcareerpages",
            })
    return jobs


def _scrape_inline(soup: BeautifulSoup, url: str) -> list[dict]:
    """Fallback: scrape job titles listed directly on the careers-page HTML.

    No descriptions are available this way (titles only), so growth-phrase
    matching won't fire — but role-keyword matching on the title still works.
    """
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
                "description": "",
                "posted_date_raw": date_elem.strip() if date_elem else None,
                "source_url": link,
                "source": "career_page",
            })
    return jobs


def _discover_jobs(company: Company) -> tuple[list[dict], list[str]]:
    """Discover postings across the company's ATS. Returns (jobs, ats_detected).

    ats_detected names every ATS we recognized (even iCIMS/Taleo we can't deeply
    read) so the evidence/logs reflect true coverage.
    """
    domain = company.domain
    pages, inline_jobs = _discover_pages(domain)
    ats = _discover_ats(pages)
    detected = sorted(ats.keys())

    all_jobs: list[dict] = []
    if "greenhouse" in ats:
        all_jobs.extend(_scrape_greenhouse(ats["greenhouse"]["token"]))
    if "lever" in ats:
        all_jobs.extend(_scrape_lever(ats["lever"]["token"]))
    if "workday" in ats:
        w = ats["workday"]
        all_jobs.extend(_scrape_workday(w["tenant"], w["dc"], w["site"]))
    # iCIMS / Taleo: detected but not deeply scrapeable (JS portals). The inline
    # HTML scrape below is the best-effort fallback for any titles on the page.

    if not all_jobs:
        all_jobs = inline_jobs
    else:
        all_jobs.extend(inline_jobs)

    seen: set[str] = set()
    unique: list[dict] = []
    for j in all_jobs:
        key = (j.get("title") or "").lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(j)
    return unique[:MAX_JOBS_PER_COMPANY], detected


# --- phrase / keyword matching (pure; unit-tested offline) -------------------

def _clean_snippet(text: str, phrase: str, width: int = SNIPPET_WIDTH) -> str:
    """Return a short, single-line window of ``text`` around ``phrase``."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    low = text.lower()
    idx = low.find(phrase.lower())
    if idx == -1:
        return ""
    start = max(0, idx - width // 3)
    end = min(len(text), idx + len(phrase) + (2 * width // 3))
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _match_signal_phrases(job: dict, phrases: list[str]) -> tuple[str, str]:
    """If any growth/intent phrase appears in the job's title+description, return
    (phrase, snippet). Empty strings if none match."""
    if not phrases:
        return "", ""
    text = f"{job.get('title', '')}. {job.get('description', '')}"
    low = text.lower()
    for p in phrases:
        if p and p.lower() in low:
            return p, (_clean_snippet(text, p) or job.get("title", ""))
    return "", ""


# --- public API --------------------------------------------------------------

def has_job_signal(company: Company, brief: Brief) -> tuple[bool, str]:
    """Cheap signal: is the company hiring, and does a posting reveal a growth
    signal we can name in the email?

    Passes if the company has ANY recent (in-window) posting — active hiring is
    itself a signal. The evidence is built richest-first:
      1. a GROWTH phrase from a description (reused verbatim in the first line),
      2. else role-keyword-matched titles,
      3. else the count of recent postings.
    Returns (passed, one-line evidence).
    """
    jp = brief.signals.get("job_postings", {}) or {}
    keywords = [k.lower() for k in jp.get("keywords_any", []) if k]
    phrases = [p for p in jp.get("signal_phrases_any", []) if p]

    jobs, ats = _discover_jobs(company)
    if not jobs:
        note = f"no job postings found" + (f" (ATS: {', '.join(ats)})" if ats else "")
        return False, note

    recent: list[dict] = []
    for j in jobs:
        if _is_within_window(_parse_date(j.get("posted_date_raw"))):
            recent.append(j)
    if not recent:
        return False, f"{len(jobs)} postings found, none within {JOB_RECENCY_DAYS}d"

    # 1) Growth/intent phrase from a description — the richest signal.
    for j in recent:
        phrase, snippet = _match_signal_phrases(j, phrases)
        if phrase:
            role = j.get("title", "").strip()
            ev = f"growth signal — hiring {role}: \"{snippet}\"" if role else \
                 f"growth signal — \"{snippet}\""
            return True, ev[:400]

    # 2) Role-keyword-matched titles.
    keyword_hits = [j["title"] for j in recent
                    if keywords and any(kw in j["title"].lower() for kw in keywords)]
    if keyword_hits:
        sample = ", ".join(keyword_hits[:3])
        return True, (f"{len(recent)} recent postings, {len(keyword_hits)} role-matched "
                      f"(e.g. {sample})")

    # 3) Hiring, but nothing role/growth specific.
    sample = ", ".join(j["title"] for j in recent[:3])
    return True, f"{len(recent)} recent postings (e.g. {sample})"
