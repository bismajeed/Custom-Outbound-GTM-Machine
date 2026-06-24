# Outbound Engine

A public, installable CLI that runs a **replenishing-reservoir cold-email
pipeline** end to end: source companies, qualify them through a cost-controlled
cascade, personalize copy, and load leads into Smartlead — then sync replies and
suppression back. Bring your own API keys; state lives in local SQLite by
default, with a Postgres switch for shared team state.

![Outbound Engine explainer](explainer.gif)

## Principles

- **Bring-your-own-keys.** No secret lives in the repo. Keys load from `.env`
  via `python-dotenv`. The app fails fast if a required key is missing.
- **Idempotent + resumable.** Every record carries a status. Re-running any
  command never double-processes a record or double-loads a lead; a crash
  resumes from the last completed stage.
- **Single-touch guarantee.** A company/contact is contacted at most once,
  enforced by the ledger *before* any paid enrichment is spent. Holds team-wide
  when all operators share one database.
- **Signals route, they don't gate.** No company is dropped for lacking a
  signal — the cascade routes it into the right campaign instead. Cheap filters
  still run first: the news research (free DuckDuckGo results summarized by Claude
  Haiku) is skipped for any company that already has a free job signal, and
  `validate` is the only stage that drops records (genuinely unreachable domains).
- **Deliverability first.** Tracking defaults OFF (brief-controlled via
  `sending.tracking`), per-mailbox caps and daily quotas are respected, every
  email carries an opt-out, and suppression is applied on every load.

## Install

```bash
pip install -e .          # installs the `outbound` command
# optional Postgres support:
pip install -e ".[postgres]"
```

Requires Python 3.10+.

## Quick start

```bash
outbound init                      # create the DB + scaffold .env
# edit .env and fill in your three keys:
#   ANTHROPIC_API_KEY, APOLLO_API_KEY, SMARTLEAD_API_KEY

outbound brief list                # the bundled `construction` brief is ready
outbound seed construction         # one-time large fill of the reservoir
outbound preview construction -n 5 # human spot-check of personalized emails
outbound load construction --quota 500   # drain reservoir -> Smartlead (the only sender)

# ongoing, day to day:
outbound run construction          # top up the reservoir (idempotent)
outbound sync                      # pull replies/bounces/unsubs -> ledger + suppression
outbound status                    # reservoir depth, counts, cost-to-date
outbound report                    # Smartlead performance summary -> Slack
```

## Commands

| Command | What it does |
|---|---|
| `outbound init` | Create the DB; scaffold `.env` from `.env.example`. |
| `outbound brief new <industry>` | Interactive wizard → `briefs/<industry>.yaml`. |
| `outbound brief list` | List available briefs (public + private). |
| `outbound seed <industry> [--depth N] [--limit N]` | One-time large source + cascade to fill the reservoir to N days. `--limit` hard-caps companies sourced (cheap test). |
| `outbound run <industry> [--limit N]` | Incremental cascade + reservoir top-up (idempotent, resumable). `--limit` caps companies sourced. |
| `outbound preview <industry> [-n 5]` | Print sample personalized emails for a spot-check. |
| `outbound load <industry> [--quota 500]` | Drain the reservoir into Smartlead (campaign + schedule + sequence, tracking off). **The only command that sends.** |
| `outbound sync [<industry>]` | Pull replies/bounces/unsubs; update ledger + suppression. |
| `outbound status [<industry>]` | Reservoir depth, pipeline counts, cost-to-date, last run. |
| `outbound export <industry> [--dir output]` | Write CSV snapshots (companies, contacts, runs) of current state. |
| `outbound report [<industry>] [--days N]` | Pull Smartlead campaign stats (sent, open %, reply %, positive replies, bounces) and post a summary to Slack if `SLACK_TOKEN` + `SLACK_CHANNEL_ID` are set. |

## The pipeline (cascade)

`run` / `seed` execute these stages in order. Each stage reads records in their
pre-stage status and writes the post-stage status, so a crash resumes cleanly.

1. **source** — Apollo company search (honours `company_filters`); dedup vs DB → `SOURCED`
2. **validate** — DNS-reachability domain check (free); **the only drop gate** → `VALID` / `DROPPED`
3. **jobs** — job-posting signal, scrape-only (free), **non-blocking** → `JOB_SIGNAL` (records a signal when found; never drops)
4. **news** — DuckDuckGo results (free) summarized by Claude (Haiku) into one dated signal (**paid only for Haiku tokens**, 120-day), **non-blocking**; short-circuited for companies that already have a job signal → `QUALIFIED`
5. **enrich** — Apollo contact enrichment (verified/likely only); dedup → `ENRICHED`
6. **personalize** — Claude first-line (or full email); routes each company into a campaign **segment** (`signal` vs `free_implementation`) via its `cell` → `QUEUED`

Every sourced + valid company reaches `QUEUED` — the signals decide *which* campaign it lands in, not *whether* it's kept. The reservoir is `contacts WHERE status='QUEUED'`; `load` drains it into **one Smartlead campaign per segment**.

## The Industry Brief

One YAML file per industry under `briefs/` (see `briefs/construction.yaml`).
Authored once via `outbound brief new`, then referenced by name. The brief drives
every filter, signal window, hook, sending rule, and the reservoir target depth.
Secret briefs go in `briefs/private/` (gitignored).

An optional `messaging:` block holds the copy that wraps the personalized first
line — `offer`, `proof_point`, `cta`, `signature`, `opt_out`, and
`subject_value_fallback` — so each industry owns its voice without touching code.
Subjects are generated per lead and A/B split: variant **A** is signal-anchored
(names their project/hire), variant **B** is a value/outcome line. The chosen
variant is pushed to Smartlead as a `subject_variant` custom field so the two can
be compared. Set `sending.tracking: on|off` to control open/click tracking.

**Per-segment copy.** Companies route into one campaign per segment (`signal` for
those with a job/news signal, `free_implementation` for those without). Override
the copy for a segment under `messaging.segments.<segment>` — any keys there
(`offer`, `cta`, `follow_ups`, …) win over the base block, so the two campaigns
can speak differently while inheriting everything you don't override.

**Technology filter.** `company_filters.technologies_any` takes Apollo
**technology UIDs** (slugs like `epic`, `oracle_netsuite`) — *not* display names,
which Apollo silently ignores. Verify UIDs against live counts with
`scripts/health_counts.py`.

## State store

`DATABASE_URL` defaults to `sqlite:///outbound.db`. Point it at a Postgres URL
(`postgresql+psycopg://user:pass@host/db`) and the same schema applies — share
one database across a team and the single-touch guarantee holds for everyone.

Tables: `companies`, `contacts`, `suppression`, `events` (append-only audit),
`runs` (resumability + per-stage cost log).

## Seeing the data (CSV outputs)

The DB is the source of truth, but you can render it to CSV any time — `run`,
`seed`, and `load` auto-write snapshots, and `outbound export <industry>` writes
them on demand. Files land under `output/<industry>/`:

- `companies.csv` — every company with its `status` (SOURCED → VALID →
  JOB_SIGNAL → QUALIFIED, or DROPPED only at `validate`) plus `drop_reason` and
  the `signal_summary` used for personalization.
- `contacts.csv` — every contact with `status` (ENRICHED → PERSONALIZED →
  QUEUED → LOADED, or SUPPRESSED), the `cell` (campaign segment: `signal` /
  `free_implementation`), and the generated `first_line`.
- `runs.csv` — per-stage log: in/out/dropped counts and `cost_usd`.

The `status` column is how you see exactly where each record sits in the
cascade at any step.

## Configuration

Copy `.env.example` to `.env` and fill in:

| Var | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | News research + personalization |
| `APOLLO_API_KEY` | yes | Company search + contact enrichment |
| `SMARTLEAD_API_KEY` | yes | Campaign + lead loading |
| `DATABASE_URL` | no | State store (SQLite default; Postgres for teams) |
| `SEARCH_API_KEY` | no | Optional web-search fallback |
| `SLACK_TOKEN` | no | Bot token for weekly report (`xoxb-…`). Needs `chat:write` scope. |
| `SLACK_CHANNEL_ID` | no | Channel to post the weekly report to (e.g. `C0B4Y5AT8J1`). |

Keys are never logged, never accepted as CLI arguments, and never committed.

## Weekly report

`outbound report` pulls live campaign stats from Smartlead and prints a summary table. If `SLACK_TOKEN` and `SLACK_CHANNEL_ID` are set in `.env`, it also posts a formatted Block Kit message to that Slack channel automatically.

```bash
outbound sync && outbound report   # sync first so local event counts are fresh
outbound report construction       # limit to one brief
outbound report --days 14          # widen the local event window
```

To get a Slack bot token: create an app at [api.slack.com/apps](https://api.slack.com/apps), add the `chat:write` scope, install to your workspace, and invite the bot to the target channel (`/invite @bot-name`).

## Development

```bash
pip install -e ".[dev]"
pytest                  # offline test suite (no keys / network required)
```

## License

MIT.
