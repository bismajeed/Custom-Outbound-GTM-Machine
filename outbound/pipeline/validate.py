"""Domain validation — cheapest gate in the cascade.

A domain is valid if it is *reachable*: it resolves in DNS and/or its site
responds. This matches the spec's DNS/MX intent and avoids dropping real company
sites that simply have generic page titles. The company-name/metadata match
(ported from legacy/domain_validator.py) is kept as a soft quality signal via
``name_matches_site`` but is NOT a drop condition.

Contract: ``validate(company) -> bool`` — True keeps the company, False drops it
with reason ``invalid_domain``. No paid API spend happens here.
"""

from __future__ import annotations

import socket
from typing import Dict

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..models import Company

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT = 8
ACCEPTABLE_STATUS_CODES = {200, 301, 302}

_BUSINESS_SUFFIXES = [
    " llc", " inc", " corp", " corporation", " company",
    " construction", " general contractor", " co.", " co",
    " group", " builders", " building",
]


def _session() -> requests.Session:
    session = requests.Session()
    retry_strategy = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": CHROME_USER_AGENT})
    return session


def normalize_domain(domain: str) -> str:
    if not domain:
        return ""
    domain = domain.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    return domain.rstrip("/")


def clean_company_name(name: str) -> str:
    if not name:
        return ""
    name_lower = name.lower()
    for suffix in _BUSINESS_SUFFIXES:
        if name_lower.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name


def _extract_metadata(html: str) -> Dict[str, str]:
    try:
        soup = BeautifulSoup(html, "lxml")
        desc = soup.find("meta", {"name": "description"})
        og = soup.find("meta", {"property": "og:site_name"})
        h1 = soup.find("h1")
        return {
            "title": soup.title.string if soup.title and soup.title.string else "",
            "description": desc["content"] if desc and desc.has_attr("content") else "",
            "og_site_name": og["content"] if og and og.has_attr("content") else "",
            "h1": h1.get_text() if h1 else "",
        }
    except Exception:
        return {}


def _name_in_metadata(company_name: str, metadata: Dict[str, str], domain: str) -> bool:
    if not company_name or not metadata:
        return False
    clean_name = clean_company_name(company_name).lower()
    if not clean_name:
        return False

    title = (metadata.get("title") or "").lower()
    if clean_name in title:
        return True

    # Branded/short domain that contains the company's first word.
    domain_stem = normalize_domain(domain).lower()
    for tld in (".com", ".net", ".org", ".co", ".us", ".io"):
        domain_stem = domain_stem.replace(tld, "")
    words = clean_name.split()
    if words and words[0] in domain_stem:
        return True

    og = (metadata.get("og_site_name") or "").lower()
    h1 = (metadata.get("h1") or "").lower()
    description = (metadata.get("description") or "").lower()
    matches = sum([clean_name in og, clean_name in h1, clean_name in description])
    return matches >= 2


def _dns_resolves(domain: str) -> bool:
    try:
        socket.getaddrinfo(domain, None)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


def name_matches_site(company: Company) -> bool:
    """Soft quality signal: does the live site's metadata reference the company?
    Used for spot-checks, never as a drop condition."""
    domain = normalize_domain(company.domain)
    if not domain:
        return False
    session = _session()
    try:
        resp = session.get(f"https://{domain}", timeout=FETCH_TIMEOUT,
                           allow_redirects=True)
    except requests.RequestException:
        return False
    if resp.status_code not in ACCEPTABLE_STATUS_CODES:
        return False
    return _name_in_metadata(company.name, _extract_metadata(resp.text), domain)


def validate(company: Company) -> bool:
    """Return True if the company's domain is reachable (DNS resolves or the site
    responds). False means the domain is unreachable → dropped as invalid_domain.

    Reachability — not name-matching — is the gate, matching the spec's DNS/MX
    intent. A site that returns 403/blocks bots but resolves in DNS still passes.
    """
    domain = normalize_domain(company.domain)
    if not domain:
        return False

    # Cheapest check first: does the hostname resolve at all?
    if not _dns_resolves(domain):
        return False

    # If it resolves, try an HTTP fetch. Any response (even 4xx/5xx) confirms a
    # live host; only a hard connection failure with no DNS would have failed
    # above. Treat DNS-resolvable domains as valid even if HTTP misbehaves.
    session = _session()
    try:
        session.get(f"https://{domain}", timeout=FETCH_TIMEOUT,
                    allow_redirects=True)
    except requests.RequestException:
        # Resolves in DNS but HTTPS failed — still a real domain; keep it.
        pass
    return True
