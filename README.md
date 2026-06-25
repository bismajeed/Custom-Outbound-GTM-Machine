# Outbound Engine

A bring-your-own-keys CLI that runs a **replenishing-reservoir cold-email
pipeline** end to end: source companies, qualify them through a cost-controlled
cascade of signals, personalize copy in a house voice, and load leads into
Smartlead — then sync replies and suppression back. State lives in local SQLite
by default, with a Postgres switch for shared team state.

One YAML **brief** per industry drives everything (ICP filters, signals,
messaging). The engine code is generic; only the brief changes per vertical.

---

## Table of contents
1. [Principles](#principles)
2. [The cascade at a glance](#the-cascade-at-a-glance)
3. [Every API call, step by step](#every-api-call-step-by-step) ← diagrams
4. [The Apollo sourcing funnel](#the-apollo-sourcing-funnel)
5. [Technology as a signal, not a gate](#technology-as-a-signal-not-a-gate)
6. [The messaging voice + how we taste-tested it](#the-messaging-voice--how-we-taste-tested-it)
7. [CTAs, tracked links, and the opener A/B](#ctas-tracked-links-and-the-opener-ab)
8. [Commands](#commands)
9. [The brief](#the-brief)
10. [Cost model](#cost-model)
11. [State store + CSV outputs](#state-store--csv-outputs)
12. [Configuration](#configuration)

---

## Principles

- **Bring-your-own-keys.** No secret lives in the repo. Keys load from `.env`. The
  app fails fast if a required key is missing. Keys are never logged or passed as
  CLI args.
- **Idempotent + resumable.** Every record carries a status. Re-running any command
  never double-processes a record or double-loads a lead; a crash resumes from the
  last completed stage.
- **Single-touch guarantee.** A company/contact is contacted at most once, enforced
  by the ledger *before* any paid enrichment is spent.
- **Signals route, they don't gate.** No company is dropped for lacking a signal —
  it routes into the right campaign instead. `validate` is the only stage that drops
  records (genuinely unreachable domains).
- **Deliverability first.** Per-mailbox caps + daily quotas respected, every email
  carries an opt-out, suppression applied on every load, ASCII-clean copy.

---

## The cascade at a glance

```
  source ─▶ validate ─▶ jobs ─▶ news ─▶ (tech tag) ─▶ enrich ─▶ personalize ─▶ load
   Apollo     DNS       scrape   DDG+      Apollo        Apollo     Haiku        Smartlead
   search    (free)    (free)    Haiku    (read-only)   (credits)  (~$0.003)    (DRAFT)
     │          │         │        │          │            │          │            │
  SOURCED ▶  VALID ▶  JOB_SIGNAL ▶ QUALIFIED ─────────────▶ ENRICHED ▶ QUEUED ▶  LOADED
                     (signals attach here; nobody is dropped)
```

Paid steps are **news** (Haiku tokens only) and **enrich** (Apollo email credits).
Everything left of `enrich` is free or token-cheap, so the expensive credit spend
only happens on companies that already cleared the cheap filters.

---

## Every API call, step by step

### 1. Apollo — company search (`source`)
`POST https://api.apollo.io/api/v1/mixed_companies/search`

```
  brief.company_filters                Apollo applies AND between layers,
  ┌───────────────────────┐            OR within a layer:
  │ industry_tag_ids       │   ───▶     ┌──────────────────────────────────────┐
  │ revenue_range {min,max}│            │ US ─▶ revenue ─▶ employees ─▶ industry │
  │ employees → buckets    │            │   ─▶ (technology) ─▶ founded ─▶ exclude│
  │ countries              │            └──────────────────────────────────────┘
  │ founded_before         │                          │ paginated, per_page 100
  │ exclude_keywords       │                          ▼
  └───────────────────────┘            organizations[]  +  accounts[]   ← MERGE both
                                                          │  (reading one drops ~half)
                                                          ▼  dedup by domain
                                                   new companies → DB (SOURCED)
```
Search consumes **no email credits**. The two response arrays are merged (a past
bug read only one and under-collected up to half of every page).

### 2. Validate (`validate`) — free, the only drop gate
```
  domain ─▶ DNS getaddrinfo  ─▶ resolves?  ─yes─▶ VALID
                              └────────────no──▶ DROPPED (invalid_domain)
```
Run single-threaded / with retries — concurrent DNS lookups produce false
negatives that wrongly drop live domains.

### 3. Jobs signal (`jobs`) — free scrape, no LLM, never drops
```
  company domain        discover the ATS          pull structured postings
  ┌────────────┐        ┌────────────────────┐    ┌──────────────────────────────┐
  │ /careers   │ ─────▶ │ scan page for links │─▶ │ Greenhouse / Lever JSON       │
  │ homepage   │        │ gh · lever · workday│    │ (title + full description)    │
  └────────────┘        │ · icims · taleo     │    │ Workday CXS (best-effort)     │
                        └────────────────────┘    └──────────────────────────────┘
                                                        │ match against the brief:
                                                        │  keywords_any      → TITLE (role)
                                                        │  signal_phrases_any → DESCRIPTION (growth)
                                                        ▼
                                 job signal → signal_summary (snippet reused in the email)
```

### 4. News signal (`news`) — DuckDuckGo (free) + Haiku
`DuckDuckGo .news()/.text()` → `Claude Haiku` (no web-search tool, no per-search fee)
```
  name + location       DuckDuckGo (free)            Claude Haiku (~$0.003)
  ┌────────────┐        ┌──────────────────┐         ┌───────────────────────────┐
  │ build 2-3  │ ─────▶ │ first results     │ ──────▶ │ extract ONE dated signal   │
  │ queries    │        │ (.news + .text)   │ snippets│ · within lookback window   │
  └────────────┘        └──────────────────┘         │ · specificity >= 3         │
                                                      │ · MUST cite a URL we passed│
                                                      └───────────────────────────┘
                                                            │ passed → signal_summary
                                                            │ else   → free_implementation
```
Replaced Claude's server-side web_search ($10/1k searches → ~$180/3k) with this
(~$10/3k). Short-circuited: a company that already has a job signal skips this.

### 5. Technology as a signal (`tech tag`) — Apollo, read-only
`POST /mixed_companies/search` once per tool (no credits)
```
  technologies_any[]                  for each tool, base filters + that one tool
  [procore, bluebeam, ...] ──────────▶ collect matching domains
                                       ┌──────────────────────────────┐
                                       │ domain → [tools it uses]      │
                                       └──────────────────────────────┘
                                                  │ tag the company (NO gate)
                                                  ▼
                              tech signal → signal_summary; companies with none are KEPT
```

### 6. Apollo — contact enrichment (`enrich`) — spends credits
`POST /mixed_people/api_search` → `POST /people/match` (1 credit per reveal)
```
  per company             find people                       reveal email (per person)
  ┌─────────────┐         ┌────────────────────────┐        ┌────────────────────────┐
  │ domain      │ ──────▶ │ person_titles (broad)   │ ─────▶ │ /people/match           │
  │ titles_any  │         │ person_seniorities      │  each  │ = 1 credit (even if the │
  │ seniority   │         │ person_locations (US)   │ person │   email is catch-all)   │
  └─────────────┘         └────────────────────────┘        └────────────────────────┘
                                people[] (masked)                     │
                                                       keep verified/likely ONLY (skip
                                                       catch-all) → up to N contacts → DB
```
⚠️ Credits are spent **per reveal**, not per kept lead — catch-all reveals are paid
for then discarded (the price of low bounce risk).

### 7. Personalize (`personalize`) — Claude Haiku (~$0.003/lead)
```
  contact + company.signal_summary        system prompt = prompts/messaging.md (house voice)
  ┌──────────────────────────┐            ┌───────────────────────────────────────────┐
  │ segment_for(company):     │ ─────────▶ │ generate UNIQUE subject + first line       │
  │  signal vs free_impl      │            │ grounded in the signal (or value angle)    │
  └──────────────────────────┘            └───────────────────────────────────────────┘
                                                       │ ASCII-normalized, QUEUED
```

### 8. Smartlead — load (`load`) — the ONLY sender, creates DRAFT
```
  QUEUED contacts        create campaign per segment       build sequence
  (split by segment) ─▶  POST /campaigns/create      ─▶    POST /campaigns/{id}/sequences
                         ┌──────────────────────┐          ┌─────────────────────────────┐
                         │ <industry> — signal   │          │ touch 1: A/B (link vs none)  │
                         │ <industry> — free_*   │          │ touches 2-5: links, anchors  │
                         └──────────────────────┘          └─────────────────────────────┘
                              │ POST /settings (tracking)         │
                              │ POST /schedule (days/caps)        ▼
                              │ POST /leads (100/batch)     status = DRAFTED (NOTHING SENDS)
                              ▼                              start it yourself in Smartlead
                        custom fields: first_line, subject, subject_variant, hook
```

---

## The Apollo sourcing funnel

`outbound funnel <industry>` prints the live company count after each cumulative
layer (read-only, no spend). Use it to audit exactly what each filter removes.

```
 step  filter              logic   companies     removed
   1   location            OR      8,285,818        —
   2   revenue_usd         range      65,096   -8,220,722
   3   employees           OR         32,121      -32,975
   4   industry (structured) OR        2,218      -29,903   ← Apollo's real industry, not keywords
   5   founded_before      range       2,073         -145
   6   exclude_keywords    NOT         1,269         -804
```

**Industry: structured vs keyword.** `q_organization_keyword_tags` (keywords) matches
any company whose tags merely *mention* a term — banks with "construction loans",
staffing, software firms — and pollutes the pool. `organization_industry_tag_ids`
(Apollo's structured industry) is precise: a bank is "banking", never "construction".
Find an industry's tag id by enriching a known company (`organizations/enrich`
returns `industry_tag_id`). Briefs should prefer `industry_tag_ids`.

---

## Technology as a signal, not a gate

Requiring a tool (e.g. Procore) as a hard filter collapsed the clean pool ~1,269 →
~221 — not because those companies don't use the tools, but because **Apollo has no
technographic data on ~93% of them**. So `company_filters.technology_as_signal: true`
sources the full pool **without** the tech gate, then tags each company with the
tools it uses (per-tool read-only queries). Companies with a tool route to the
`signal` segment and the email can name it; companies with none are **kept** and
route to `free_implementation`.

---

## The messaging voice + how we taste-tested it

The house voice lives in **`outbound/prompts/messaging.md`** (the system prompt for
*every* brief's personalization) and is summarized in **`docs/messaging-standard.md`**.
It was derived from a structured **15-question A/B taste-test** (full vote log:
`docs/construction-messaging-taste-test.txt`).

**The standard:**
- **Subjects (no-signal):** provocative-but-respectful **waste reframes** — aim at
  the system, never insult the people. e.g. *"you're paying estimators to copy-paste"*.
  No "free", no "pilot", no flat labels, no cutesy/puns.
- **Subjects (signal):** name their tool/move as the culprit — *"procore's costing
  your estimators hours"*. Never a bare label like *"acme precon backlog"*.
- **Openers:** blunt half-truth / number-anchored / direct question (not empathy-soft).
- **Body:** ruthlessly short — hook + one mechanism line + ask.
- **Greeting:** `{first_name} -` (no "Hi"). **CTA** lives in a **P.S.**
- **Always:** plain ASCII (no em-dashes — they're mojibake in Excel *and* an AI tell),
  lowercase subjects, deliverability-safe.

**How the test worked:** 4 rounds of `AskUserQuestion` votes (subjects → openers →
edginess/CTA → body structure). Each option was a real candidate line; picks and
rejects both trained the spec. Example outcome — rejected *"free bid extraction
pilot"* and *"acme precon backlog"*; chose *"you're paying estimators to copy-paste"*
and *"weird question about your bids"*. The synthesized spec was then encoded into
`messaging.md` so all future copy follows it.

**Example email (construction):**
```
Subject: you're paying estimators to copy-paste

{first_name} -

Half of what your estimators do all day isn't estimating, it's re-typing bid data
between systems. we pull it straight out of your estimating system automatically.

P.S. want me to run it on one of your recent bids?
```

---

## CTAs, tracked links, and the opener A/B

The CTA is a soft, show-don't-tell ask living in a **P.S.** (e.g. *"want me to run
it on one of your recent bids?"*). When a brief adds a UTM link, the **anchor text
follows the voice** — lowercase, 2-5 words, show-don't-tell, never "click here /
learn more / book a demo". Use a **different anchor per touch**:

| Bucket | Examples |
|---|---|
| Show-don't-tell | `see it on a real bid` · `see the 60-second teardown` · `see the before/after` |
| Curiosity | `see what i mean` · `here's the part nobody automates` · `the 2-minute version` |
| Contrarian | `skip my pitch, see the output` · `don't take my word for it` · `prove me wrong` |
| Personalized | `see it on a {{company_name}} bid` |

**Deliverability + the opener A/B.** A link in the cold opener (touch 1) is a strong
spam signal, especially with click-tracking wrapping it in a redirect. So when a
brief sets `cta_link`, the engine **A/B tests the opener link 50/50** via Smartlead
step variants: **A = no link, B = link**. Touches 2-5 carry the link with varied
anchors. Brief fields: `cta_link` (URL), `cta_link_text` (opener anchor), and the
per-touch anchors live in `messaging.follow_ups`.

---

## Commands

| Command | What it does |
|---|---|
| `outbound init` | Create the DB; scaffold `.env`. |
| `outbound brief new <industry>` | Interactive wizard → `briefs/<industry>.yaml` (prompts for ICP, **signals**, messaging). |
| `outbound brief list` | List briefs. |
| `outbound funnel <industry>` | Read-only Apollo company-count funnel (per-layer counts) → `output/<industry>/funnel.csv`. |
| `outbound seed <industry> [--depth N] [--limit N]` | One-time large source + cascade. |
| `outbound run <industry> [--limit N]` | Incremental cascade + reservoir top-up (idempotent). |
| `outbound preview <industry> [-n 5]` | Print sample personalized emails. |
| `outbound load <industry> [--quota N]` | Drain QUEUED → Smartlead (one DRAFT campaign per segment). **The only sender.** |
| `outbound sync [<industry>]` | Pull replies/bounces/unsubs → ledger + suppression. |
| `outbound status [<industry>]` | Reservoir depth, counts, cost-to-date. |
| `outbound export <industry>` | CSV snapshots. |
| `outbound report [<industry>]` | Smartlead stats → Slack. |

**Helper scripts** (`scripts/`): `funnel`-style counts, `tech_signal_export.py`
(tag tech per company), `signal_scan.py` / `signal_complete.py` / `signal_scan_csv.py`
(bulk signal scan, the last one Apollo-free from a captured CSV), `enrich_only.py`,
`personalize_only.py`.

---

## The brief

One YAML per industry under `briefs/`. The engine is generic — only the brief changes.

```yaml
company_filters:
  industry_tag_ids: ["..."]        # Apollo structured industry (precise)
  industries: [...]                # OR loose keywords (fallback)
  revenue_usd: {min, max}
  employees:   {min, max}          # mapped to Apollo's discrete buckets
  countries:   [United States]
  technology_as_signal: true       # tech is a signal, not a gate
  technologies_any: [...]          # tools to tag (UIDs/names)
  founded_before: 2018
  exclude_keywords: [...]          # sent server-side (q_not_organization_keyword_tags)
contact_filters:
  titles_any: [...]                # broad enough to actually match (see below)
  seniority: [Director, VP, C-Suite]
  email_status: [verified, likely] # skip catch-all
  contacts_per_company: 5
signals:
  job_postings: {keywords_any: [...], signal_phrases_any: [...]}  # title / description
  news: {lookback_days: 120, themes_any: [...]}
messaging:
  offer / cta / opt_out / subject_value_fallback
  cta_link / cta_link_text         # opener A/B link + anchor
  follow_ups: [{delay_days, body (may contain <a href=...>)}]
  segments: {free_implementation: {...overrides...}}   # per-segment copy
sending: {daily_quota, days, window_local, per_mailbox_cap, tracking}
```

**Title breadth matters.** Narrow titles (e.g. only "Chief Estimator") match 0-3
people per mid-market company in Apollo; broadening to construction-leadership terms
(Operations, Project Executive, President, COO, CFO …) + the seniority filter surfaces
6-17 real decision-makers. Note: Apollo's people search returns `total_entries: 0` as
a quirk — the `people[]` array is the real result.

---

## Cost model

| Step | Cost | Notes |
|---|---|---|
| source / funnel / tech-tag | **$0** | Apollo company search consumes no email credits |
| validate / jobs | **$0** | DNS + scraping |
| news | **~$0.003/co** | Haiku tokens only (DuckDuckGo is free) |
| personalize | **~$0.003/lead** | Haiku |
| **enrich** | **~1 Apollo credit per reveal** | ~3-5 reveals/co (catch-all reveals paid + discarded) |
| load | **$0** | Smartlead API |

Per ~640 companies enriched we spent ~2,500 Apollo credits for ~1,750 verified/likely
leads (≈4 credits/company). Budget ~4 credits/company.

---

## State store + CSV outputs

`DATABASE_URL` defaults to `sqlite:///outbound.db`; point it at Postgres to share team
state. Tables: `companies`, `contacts`, `suppression`, `events`, `runs`.

CSVs land under `output/<industry>/`: `companies.csv`, `contacts.csv` (incl. `cell`
segment, `subject`, `first_line`), `runs.csv`, `funnel.csv`, plus analysis exports
(`company_signals.csv`, `companies_with_tech_signal.csv`, `pending_enrichment.csv`).
CSVs are written `utf-8-sig` so Excel renders them correctly.

---

## Configuration

| Var | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | News research + personalization (Haiku) |
| `APOLLO_API_KEY` | yes | Company search + contact enrichment |
| `SMARTLEAD_API_KEY` | yes | Campaign + lead loading |
| `DATABASE_URL` | no | SQLite default; Postgres for teams |
| `SLACK_TOKEN` / `SLACK_CHANNEL_ID` | no | Weekly report to Slack |

```bash
pip install -e .        # installs the `outbound` command (Python 3.10+)
pip install -e ".[dev]" # + pytest
pytest                  # offline suite (no keys / network)
```

## License
MIT.
