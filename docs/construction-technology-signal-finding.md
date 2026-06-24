# Findings: construction sourcing precision + technology as a signal

Two related issues surfaced while auditing the construction pool against the Apollo
UI. Both are now fixed.

## Issue 1 — the industry filter was loose keyword matching (let banks/junk in)

The brief filtered industry with Apollo's `q_organization_keyword_tags` — a **loose
keyword match**. It returns any company whose Apollo keyword tags merely *mention*
the term: a bank that offers "construction loans", a staffing firm that places
"construction" workers, a software vendor that builds "construction software". So
the pool was polluted with banks, credit unions, staffing agencies, and tech
companies (10Pearls, BuzzFeed, Carta) that are not construction firms at all. It
was never a true industry filter — just a text-tag match.

**Fix:** switch to Apollo's **structured Industry** classification
(`organization_industry_tag_ids`). Every company is assigned exactly one real
industry; a bank is classified "banking", not "construction", so it is excluded by
design. The construction tag id (`5567cd4773696439dd350000`) was verified by
enriching known GCs (`organizations/enrich` returns `industry_tag_id`). Result:
**~1,269 clean construction companies, zero banks.** Fewer than the polluted ~4,840,
but correct. (To find the tag id for a new vertical, enrich a known company in it.)

Two accuracy bugs were fixed alongside this: (a) `founded_before` and
`exclude_keywords` were applied client-side *after* fetch, so counts didn't match
the Apollo UI — now sent server-side; (b) Apollo splits results across two arrays
(`organizations` and `accounts`) and the code read only one, **under-collecting up
to half of every page** — now merged. Counts, sourcing, and the UI now agree.

## Issue 2 — technology was a hard gate, dropping ~83% of good-fit companies

The brief also *required* each company to use one of six construction tools
(Procore, Bluebeam, etc.). On the clean construction pool that requirement collapses
**~1,269 → ~221** — it removes ~83% of perfect-fit companies. They are not excluded
because they don't use construction software; they're excluded because **Apollo has
no technographic data on them** (only ~7% of GCs have a tool recorded, and
`e-Builder` returns zero entirely). The gate was discarding great prospects for a
**data-coverage** reason, not a fit reason.

**Fix — technology as a signal, not a gate** (the engine's "signals route, they
don't gate" principle), enabled per-brief with `company_filters.technology_as_signal:
true`:

1. **Source the full clean pool (~1,269)** with no technology gate.
2. **Tag each company** with the tools it actually uses (`technologies_matched`
   column / `signal_summary`). ~221 carry a tool; the other ~1,030 are **kept**.
3. **Use the tag for personalization** — "saw your team runs Procore…" for the
   ~221 — while the rest are still sourced and messaged on the industry/role angle.

This keeps every qualified construction company in play while *strengthening*
personalization where the data exists. The deliverable
`output/construction/companies_with_tech_signal.csv` implements exactly this:
full clean pool retained, technology recorded per row.

Both fixes are generic and brief-driven — a new vertical sets its own
`industry_tag_ids` and `technology_as_signal`; the engine mechanism is unchanged.
