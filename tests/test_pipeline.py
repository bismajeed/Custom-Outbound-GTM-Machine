"""Pipeline helpers that need no network or API keys."""

import pytest

from outbound.brief import load_brief
from outbound.db import Database
from outbound.models import Company, Contact
from outbound.pipeline.personalize import (
    SEGMENT_NONE, SEGMENT_SIGNAL, pick_hook, segment_for,
)
from outbound.models import CompanyStatus
from outbound.pipeline import signals_jobs, signals_news
from outbound.pipeline.validate import normalize_domain, clean_company_name
from outbound.run import (
    compute_source_limit, reservoir_depth_days, stage_jobs, stage_news,
)


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path / 'test.db'}")
    d.init()
    return d


def test_normalize_domain():
    assert normalize_domain("https://www.Acme.com/") == "acme.com"
    assert normalize_domain("HTTP://foo.io") == "foo.io"


def test_clean_company_name():
    assert clean_company_name("Acme Construction").lower() == "acme"
    assert clean_company_name("Beta LLC").lower() == "beta"


def test_hook_round_robin_is_deterministic():
    b = load_brief("construction")
    c = Contact(email="a@b.com", company_domain="b.com")
    assert pick_hook(c, b.hooks)["id"] == pick_hook(c, b.hooks)["id"]


def test_hook_round_robin_distributes():
    b = load_brief("construction")
    ids = {pick_hook(Contact(email=f"u{i}@x.com", company_domain="x.com"),
                     b.hooks)["id"] for i in range(30)}
    assert len(ids) > 1  # not all the same hook


def test_source_limit_zero_when_full(db, monkeypatch):
    b = load_brief("construction")
    # Pretend reservoir already at target depth.
    monkeypatch.setattr(db, "reservoir_count",
                        lambda brief: b.target_depth_days * b.daily_quota)
    assert compute_source_limit(db, b) == 0


def test_source_limit_positive_when_empty(db):
    b = load_brief("construction")
    assert compute_source_limit(db, b) > 0


def test_reservoir_depth(db, monkeypatch):
    b = load_brief("construction")
    monkeypatch.setattr(db, "reservoir_count", lambda brief: b.daily_quota)
    assert reservoir_depth_days(db, b) == pytest.approx(1.0)


def test_segment_for_routes_by_signal():
    with_signal = Company(domain="a.com", signal_summary="hire: new VP RCM")
    no_signal = Company(domain="b.com", signal_summary=None)
    blank_signal = Company(domain="c.com", signal_summary="   ")
    assert segment_for(with_signal) == SEGMENT_SIGNAL
    assert segment_for(no_signal) == SEGMENT_NONE
    assert segment_for(blank_signal) == SEGMENT_NONE


def test_signal_stages_never_drop_and_route(db, monkeypatch):
    """jobs + news are non-blocking: every company advances (none DROPPED) and
    carries a signal_summary iff a signal was found — which is what segment_for
    routes on."""
    b = load_brief("healthcare-admin")
    for dom in ("hire.com", "news.com", "none.com"):
        db.insert_company(Company(domain=dom, name=dom, brief=b.industry,
                                  status=CompanyStatus.VALID))

    # Only hire.com has a job signal; only news.com has a news signal.
    monkeypatch.setattr(signals_jobs, "has_job_signal",
                        lambda c, brief: (c.domain == "hire.com", "hiring RCM dir"))
    monkeypatch.setattr(signals_news, "research_news", lambda c, brief: {
        "passed": c.domain == "news.com",
        "summary": "tech_adoption: epic go-live" if c.domain == "news.com" else "none",
        "cost_usd": 0.0,
    })

    jobs = stage_jobs(db, b)
    news = stage_news(db, b)
    assert jobs["dropped"] == 0 and news["dropped"] == 0          # nothing dropped
    assert all(c.status == CompanyStatus.QUALIFIED                 # all advanced
               for c in db.companies_by_status(b.industry, CompanyStatus.QUALIFIED))

    summaries = {c.domain: (c.signal_summary or "")
                 for c in db.companies_by_status(b.industry, CompanyStatus.QUALIFIED)}
    assert "hiring" in summaries["hire.com"]                       # job signal kept
    assert "epic" in summaries["news.com"].lower()                # news supersedes
    assert summaries["none.com"].strip() == ""                    # no signal -> free_impl


def test_match_signal_phrases_extracts_snippet():
    """A growth phrase in the description is found and a snippet captured."""
    job = {"title": "Senior Estimator",
           "description": "We are growing fast and breaking ground on a new "
                          "200,000 sq ft distribution center in Austin this fall."}
    phrase, snippet = signals_jobs._match_signal_phrases(
        job, ["new facility", "breaking ground", "expanding into"])
    assert phrase == "breaking ground"
    assert "breaking ground" in snippet.lower()
    assert "austin" in snippet.lower()


def test_match_signal_phrases_none_when_no_match():
    job = {"title": "Office Manager", "description": "General admin duties."}
    assert signals_jobs._match_signal_phrases(job, ["breaking ground"]) == ("", "")


def test_has_job_signal_prefers_growth_phrase(monkeypatch):
    """Evidence leads with the description growth signal, reused in messaging."""
    b = load_brief("construction")
    jobs = [{"title": "Senior Estimator", "description": "joining us as we expand "
             "into the Texas market with a new office in Dallas.",
             "posted_date_raw": "", "source_url": "x", "source": "greenhouse"}]
    monkeypatch.setattr(signals_jobs, "_discover_jobs", lambda c: (jobs, ["greenhouse"]))
    passed, ev = signals_jobs.has_job_signal(Company(domain="gc.com", name="GC"), b)
    assert passed
    assert "growth signal" in ev.lower()
    assert "texas" in ev.lower() or "dallas" in ev.lower()


def test_has_job_signal_falls_back_to_role_keyword(monkeypatch):
    """No growth phrase, but a role-keyword title still yields a signal."""
    b = load_brief("construction")
    jobs = [{"title": "Preconstruction Manager", "description": "standard role.",
             "posted_date_raw": "", "source_url": "x", "source": "lever"}]
    monkeypatch.setattr(signals_jobs, "_discover_jobs", lambda c: (jobs, ["lever"]))
    passed, ev = signals_jobs.has_job_signal(Company(domain="gc.com", name="GC"), b)
    assert passed
    assert "role-matched" in ev.lower()


def test_has_job_signal_none_when_no_jobs(monkeypatch):
    b = load_brief("construction")
    monkeypatch.setattr(signals_jobs, "_discover_jobs", lambda c: ([], []))
    passed, ev = signals_jobs.has_job_signal(Company(domain="gc.com", name="GC"), b)
    assert not passed
    assert "no job postings" in ev.lower()


def test_messaging_for_merges_segment_override():
    b = load_brief("healthcare-admin")
    base = b.messaging_for(SEGMENT_SIGNAL)        # no override -> base messaging
    free = b.messaging_for(SEGMENT_NONE)          # free_implementation override
    # The override changes the opener offer/cta...
    assert free["offer"] != base["offer"]
    assert "shadow mode" in free["offer"].lower()
    # ...but inherits base follow_ups and never leaks the raw segments map.
    assert free["follow_ups"] == base["follow_ups"]
    assert "segments" not in free and "segments" not in base


# --- enrichment credit-guard (early-exit on bad-data companies) ----------------

class _FakeResp:
    def __init__(self, people):
        self._people = people
    def json(self):
        return {"people": self._people}


def _people(n):
    return [{"id": str(i), "first_name": f"A{i}", "last_name": "X", "title": "VP"}
            for i in range(n)]


def test_enrich_early_exit_on_bad_data(monkeypatch):
    """All-catch-all company: stop after MAX_CONSECUTIVE_MISSES reveals, not all 20."""
    from outbound.sources import apollo
    b = load_brief("construction")
    monkeypatch.setattr(apollo, "request_with_retry", lambda *a, **k: _FakeResp(_people(20)))
    calls = {"n": 0}
    def fake_reveal(pid):
        calls["n"] += 1
        return ("x@catchall.com", "unknown")          # never keepable
    monkeypatch.setattr(apollo, "_reveal_email", fake_reveal)
    out = apollo.enrich_contacts(Company(domain="bad.com", name="Bad"), b)
    assert out == []
    assert calls["n"] == apollo.MAX_CONSECUTIVE_MISSES   # stopped early, not 20


def test_enrich_keeps_all_good_leads_no_waste(monkeypatch):
    """Good-data company: get `want` leads with no wasted reveals."""
    from outbound.sources import apollo
    b = load_brief("construction")                       # want = 5
    monkeypatch.setattr(apollo, "request_with_retry", lambda *a, **k: _FakeResp(_people(20)))
    calls = {"n": 0}
    def fake_reveal(pid):
        calls["n"] += 1
        return (f"user{calls['n']}@good.com", "verified")
    monkeypatch.setattr(apollo, "_reveal_email", fake_reveal)
    out = apollo.enrich_contacts(Company(domain="good.com", name="Good"), b)
    assert len(out) == b.contacts_per_company
    assert calls["n"] == b.contacts_per_company          # no reveals beyond what's needed


def test_enrich_miss_counter_resets_on_hit(monkeypatch):
    """A keepable email inside the miss window resets the counter (no lead dropped)."""
    from outbound.sources import apollo
    b = load_brief("construction")
    monkeypatch.setattr(apollo, "request_with_retry", lambda *a, **k: _FakeResp(_people(30)))
    seq = iter([("a@x.com", "verified")] + [("c@catch.com", "unknown")] * 4
               + [("b@x.com", "verified")] + [("c@catch.com", "unknown")] * 20)
    monkeypatch.setattr(apollo, "_reveal_email", lambda pid: next(seq))
    out = apollo.enrich_contacts(Company(domain="mix.com", name="Mix"), b)
    assert len(out) == 2   # both verified kept despite 4 catch-alls between them
