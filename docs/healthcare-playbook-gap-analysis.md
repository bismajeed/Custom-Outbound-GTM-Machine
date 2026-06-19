# Gap Analysis — Healthcare-Admin Playbook vs. Custom Outbound Email Machine

**Date:** 2026-06-19
**Compared:** `~/Desktop/Outbound contractor/healthcare-admin/` (a 7-document VA operational playbook for a healthcare prior-auth/HCC/denials vertical) against this repo (`outbound/` automation engine + `evals/` harness).

---

## TL;DR

These two things are not the same kind of artifact, and that is the whole story.

- **The playbook is an *operating manual for humans*** — a VA + Brad running Apollo and Smartlead by hand, with SOPs for sourcing, reply triage, escalation, metrics, and asset design. It is deep on the **human judgment, reply-handling, compliance, and funnel-tracking** layers.
- **This repo is an *automation engine*** — it replaces the playbook's entire Monday sourcing SOP and personalization step with a cost-gated cascade (`source → validate → jobs → news → enrich → personalize → load → sync`). It is deep on the **sourcing, qualification, dedup, cost-control, and state-machine** layers.

They overlap in the middle (Smartlead sending, sequences, suppression, basic reporting) and are each blind where the other is strong. **The playbook describes a funnel the engine can fill the top of but cannot yet measure the bottom of.** The engine automates work the playbook still does by hand.

The single highest-value takeaway: **the engine could run this healthcare campaign *today* for the sourcing-through-sending half, but it would go blind exactly where this vertical needs the most discipline — reply classification beyond 4 crude buckets, the "Not Now" nurture loop, meeting/pipeline tracking, and the compliance-forward sending rules.** See [§5](#5-could-the-engine-run-this-playbook-today).

---

## 1. What each side is

| | Healthcare playbook (`healthcare-admin/`) | This repo (`outbound/` + `evals/`) |
|---|---|---|
| **Form** | 7 markdown SOPs for a human operator | Python CLI automation engine + eval harness |
| **Operator** | A VA (~10 hrs/wk) + Brad (~2 hrs/wk) | `outbound <command>` + cron |
| **Strong at** | Reply triage, escalation, compliance posture, funnel metrics, nurture, asset design | Sourcing, signal-qualification, dedup, cost-gating, idempotent state, personalization-at-scale |
| **Data stack** | Apollo (manual) + Smartlead (manual) | Apollo (API) + Smartlead (API) + SQLite/Postgres |
| **Vertical** | Healthcare admin (one specific ICP) | Vertical-agnostic; configured per `briefs/*.yaml` (currently construction) |
| **Time horizon** | 8–24 week slow-burn, explicitly long-cycle | N/A — runs continuously, no campaign-clock concept |

The cleanest mental model: **the playbook is one fully-specified `brief.yaml` plus all the human SOPs that wrap around the parts the engine doesn't cover.**

---

## 2. Capability matrix

Legend: ✅ first-class · 🟡 partial / manual workaround · ❌ absent

| Capability | Playbook | Engine | Notes |
|---|:---:|:---:|---|
| **Sourcing** |
| ICP definition | ✅ (prose) | ✅ (`company_filters`/`contact_filters`) | Engine encodes it as YAML; playbook as prose. Equivalent. |
| Apollo company/contact pull | 🟡 manual, 90 min/wk | ✅ `outbound run` | `sources/apollo.py` |
| Signal-based qualification (jobs/news) | ❌ | ✅ | Engine gates on job-posting + Claude news research *before* paying to enrich. Playbook has no equivalent — it pulls by static filter. |
| Per-lead personalization signal | 🟡 VA hand-finds 1 signal each | ✅ auto-sourced + Claude-generated | `pipeline/personalize.py` |
| Single-touch / cross-wave dedup | 🟡 VA checks Smartlead history | ✅ enforced pre-spend | `db.py insert_company/insert_contact` |
| Contact verification (LinkedIn live, title still matches) | ✅ 30-sec/contact human pass | 🟡 email-status + DNS only | Engine can't see "promoted out" or dead LinkedIn. |
| **Sending** |
| Smartlead campaign/sequence creation | 🟡 manual | ✅ | `load/smartlead.py` |
| Day-of-week + time-window control | ✅ Tue/Wed/Thu, 9–11:30 local | ✅ `sending.days`/`window_local`/`timezone` | Parity. |
| Multi-touch sequence | ✅ 5 touches | ✅ `messaging.follow_ups` | Parity on count/delay. |
| **Per-touch subject A/B** | ✅ Subject A/B *per touch* | ❌ touch-1 only; follow-ups send blank subject (thread) | `smartlead.py:184` hardcodes `"subject": ""`. |
| **Named-variant swap** (anon → case study) | ✅ Touch 2 anon/named variants | ❌ no variant-swap concept | Big one given the "no proof point yet" constraint. |
| "No links in Touch 1" rule | ✅ enforced by SOP | ❌ not modeled | Engine has no per-touch link policy. |
| Volume ramp over weeks (40→200/day) | ✅ | ❌ fixed `daily_quota` | No campaign-clock / ramp schedule. |
| Per-mailbox cap enforcement | ✅ SOP hard ceiling | 🟡 schema field, not enforced in code | Relies on Smartlead. |
| Suppression (unsub/bounce/meeting) | ✅ | ✅ | `sync.py` — strong on both sides. |
| **Reply handling** |
| Reply ingestion | ✅ manual inbox | ✅ `outbound sync` | |
| Classification | ✅ 4 categories + 5 sub-templates + edge cases | 🟡 4 crude buckets (unsub/bounce/reply/positive) | Engine has no "Not Now" vs "Wrong Person" split. |
| Canned reply templates | ✅ 1A–1E, 2, 3, 4, 4B | ❌ | Engine never drafts a reply. |
| Escalation of interested replies | ✅ Slack, 1-hr SLA, full context | ❌ | Engine logs an event; no human-routing. |
| "Not Now" nurture + timed re-engage | ✅ nurture list + reminder | ❌ | No nurture concept at all. |
| Wrong-person referral re-sourcing | ✅ source the named person, priority flag | ❌ | Just marks `REPLIED`. |
| Compliance-question = always positive | ✅ explicit rule | ❌ | "HIPAA?" would tag as generic `reply`. |
| **Metrics** |
| Sends / open / reply / bounce / unsub | ✅ | ✅ `report.py` | Parity. |
| Positive-reply *rate* as north-star w/ targets | ✅ tiered targets by week | 🟡 raw positive count, no rate/target | |
| Meetings booked / held | ✅ core funnel metric | ❌ no meeting concept | |
| Meetings per 100 sends | ✅ | ❌ | |
| Pipeline value ($) | ✅ MEDDPICC-weighted | ❌ | |
| Deliverability composite (spam %, inbox placement) | ✅ Mailreach audits | 🟡 bounce/unsub only | No inbox-placement or spam-complaint metric. |
| Kill-criteria thresholds + alerting | ✅ explicit (bounce >3%, 3+ mailboxes, etc.) | ❌ | No threshold monitoring. |
| Slack weekly report | ✅ formatted | ✅ `report.py post_to_slack` | Parity on the mechanism. |
| **Quality / governance** |
| Personalization quality auditing | 🟡 manual spot-check prompt (05-F) | ✅ versioned LLM-judge rubric | `evals/` harness is *more* rigorous than the playbook here. |
| Human approval gate before send | ✅ Brad approves Monday batch | ❌ `load` just pushes | No approval checkpoint. |
| Cost tracking | 🟡 VA *hours* | ✅ per-stage `cost_usd` | `runs` table. |
| Idempotent / resumable state | ❌ (manual SOP) | ✅ | status machine + `runs` ledger. |
| Asset/design generation (one-pagers, BAA PDF) | ✅ Claude design prompts (07) | ❌ out of scope | |

---

## 3. What the engine is MISSING (the playbook does it, we can't)

Ordered by impact for actually running outbound well.

### 3.1 Reply handling is the biggest gap — by a wide margin
The playbook's `03-reply-handling.md` is a 248-line discipline. The engine's `sync.py` reduces all of it to four flags (`unsubscribe`/`bounce`/`reply`/`positive_reply`) and one action each. Specifically missing:

- **"Not Now" as a distinct category** with a nurture list and timed re-engagement ("ping me after our Epic go-live"). The engine has no nurture state — a "not now" looks identical to a dead lead.
- **"Wrong Person / referral" handling.** When a prospect says "talk to my Director of UM," the playbook sources that person, flags them priority for next week, and references the original prospect to convert cold→warm. The engine just marks `REPLIED` and the warm intro evaporates.
- **Compliance-question-as-buying-signal.** In healthcare, "are you HIPAA compliant?" is the #1 positive signal; the playbook *never* lets it be tagged non-interested. The engine's `_classify` would catch it only if Smartlead's `reply_category` happened to say "positive" — otherwise it's a generic `reply`.
- **Escalation with SLA.** No mechanism to route an interested reply to a human within 1 hour with full context. The engine logs an event to a table nobody is watching in real time.
- **Canned reply templates (1A–1E).** The engine never composes a reply.

> **Why it matters:** this vertical's entire thesis is *slow-burn nurture*. The two reply types the engine handles worst ("Not Now" and "Wrong Person") are exactly the two that dominate a 9–18 month cycle. This is the gap most likely to lose real pipeline.

### 3.2 The bottom of the funnel is invisible
`report.py` measures sends → replies. It has **no concept of a meeting or a dollar.** The playbook's north-star (`06-metrics`) is *positive-reply rate → meetings held → pipeline value*, with week-tiered targets and kill thresholds. Today the engine cannot answer "did this campaign produce a meeting?" — let alone "$ in pipeline." Missing:
- meeting booked/held tracking (no data model for it),
- positive-reply *rate* against a target (only a raw count),
- deliverability composite (spam-complaint rate, inbox placement),
- automated kill-criteria alerting.

### 3.3 Per-touch copy control
The brief models follow-ups as `{delay_days, body}` only. The playbook needs **a subject line per touch** (and A/B per touch) and a **named-variant swap** for Touch 2 (anonymous now, named-client once a pilot closes — which is *exactly* the "no proof point yet" situation recorded in memory). `smartlead.py:184` hardcodes follow-up subjects to empty. To run this playbook faithfully, the schema needs:
```yaml
follow_ups:
  - delay_days: 5
    subject_a: "re: PA volume question, {{first_name}}"
    subject_b: "Quick follow up"
    body_variants:
      anonymous: "..."
      named: "..."     # swapped in when a flag flips
```

### 3.4 Compliance-forward sending rules
Healthcare adds rules the engine can't express: *no links in Touch 1*, *soft CTA only in 1–3*, *never say "shipped in 90 days"*, *never claim clinical authority*. Some of these belong in copy (the engine sends what the brief says), but **"no links in Touch 1"** and **"link only from Touch N"** are structural policies worth enforcing in code. There is no `compliance:` brief section today.

### 3.5 Volume ramp + human approval gate
- **Ramp:** the playbook ramps 40→60→100→120→200/day over 4 weeks for deliverability. The engine sends a flat `daily_quota`. No campaign-clock.
- **Approval gate:** Brad approves each Monday batch in Slack (default-approve at 5pm). `outbound load` has no checkpoint — it drains the reservoir straight to Smartlead.

### 3.6 Contact-verification depth
The playbook's 30-second-per-contact human pass catches "promoted out," dead LinkedIn, parked domains. The engine validates email-status + DNS but can't see a stale title or an inactive profile.

---

## 4. What the PLAYBOOK is missing (the engine does it, the docs ignore it)

The playbook was written as if all sourcing and personalization is manual labor. The engine proves large parts of it don't have to be — and the docs never account for that.

### 4.1 The entire Monday sourcing SOP is automatable
`01-icp-and-sourcing.md` budgets **90–120 min/week** of VA time to pull, verify, dedup, and personalize 60 contacts. The engine does the pull, the dedup-vs-history, and the personalization automatically and continuously. The playbook's manual "has this company been emailed in 90 days?" check (an error-prone human lookup) is the engine's **single-touch guarantee, enforced before any spend.** The docs have no awareness this is solvable.

### 4.2 Signal-gated qualification doesn't exist in the playbook
The engine's `jobs` + `news` stages qualify a company on a *fresh buying signal* (hiring PA specialists, an Epic go-live, an expansion) **before** spending to enrich — and that same signal becomes the personalization hook. The playbook makes the VA hunt for a signal *per prospect by hand* in `04`'s Thursday pre-sourcing. The engine sources the signal as a gate *and* a hook in one pass. This is the engine's biggest conceptual advantage and the docs miss it entirely.

### 4.3 Cost discipline as a first-class concern
The playbook tracks **VA hours**; it never tracks **API cost.** The engine's cascade exists precisely to spend cheap filters first and gate the expensive Claude/Apollo calls behind them, logging `cost_usd` per stage in the `runs` table. A healthcare program with a smaller, harder-to-source pool (the docs note this) would benefit even more from cost-gating — but the playbook has no vocabulary for it.

### 4.4 Replenishing reservoir vs. fixed weekly batch
The playbook pulls a fixed 60/week. The engine maintains a **QUEUED reservoir at a target depth** (`reservoir.target_depth_days`) and tops up so loading never starves and ramps are smooth. This is a strictly more robust supply model than "60 every Monday."

### 4.5 Systematic message-quality evaluation
The playbook's quality control is a single human spot-check prompt (`05-F` personalization auditor). The engine's `evals/` harness is a **versioned LLM-as-judge** with hard-fail auto-zeros, weighted criteria, and v1/v2 A/B calibration. The engine's governance of copy quality is materially more rigorous than the playbook's — and notably, the engine's hard-fail list already encodes several of the playbook's "never use" rules (`innovative`, `leverage`, `groundbreaking`, AI-buzz, appearance/age). The two were clearly designed in the same spirit; the engine just operationalized it.

### 4.6 Idempotent, resumable, audited state
Every record has a status; crashes resume; the `runs` ledger logs every stage with counts and cost; the `events` table is an append-only audit. The playbook is a sequence of manual steps with no state — if the VA is out sick, the system has no memory of where it was.

### 4.7 Deterministic, reproducible variant assignment
Hooks and subject A/B are assigned by a stable hash of the email (`pipeline/personalize.py`), so re-runs are idempotent and the A/B split is reproducible. The playbook assigns variants by hand.

---

## 5. Could the engine run this playbook today?

**Partially — and the seams are exactly the gaps above.** A `briefs/healthcare-admin.yaml` could be written right now to cover sourcing → sending:

| Playbook element | Maps to brief field | Ready? |
|---|---|---|
| ICP (revenue, size, EHR tech) | `company_filters` | ✅ |
| Titles (VP RevCycle, Dir HIM/UM/CDI) | `contact_filters.titles_any` + `icp_title_keywords` | ✅ |
| Exclude clinical titles, telehealth/dental | `exclude_keywords` + title keywords | ✅ |
| Tue/Wed/Thu, 9–11:30 local | `sending.days` / `window_local` / `timezone` | ✅ |
| 5 touches, 18 days | `messaging.follow_ups` (already 4 follow-ups on construction) | ✅ |
| Signal hooks (PA backlog, Epic go-live) | `signals.job_postings` / `signals.news` + `hooks` | ✅ |
| Soft CTA, opt-out | `messaging.cta` / `opt_out` | ✅ |

**What blocks a faithful run:**
1. **Per-touch subjects + named-variant swap** (§3.3) — Touch 2 anon/named is core to the no-case-study positioning.
2. **"No links in Touch 1," link-only-from-Touch-4** (§3.4) — structural compliance rule, unmodeled.
3. **Reply triage beyond 4 buckets** (§3.1) — the engine would mishandle "Not Now" and "Wrong Person," the dominant healthcare reply types.
4. **Meeting + pipeline tracking** (§3.2) — you'd be flying blind on the only metrics `06-metrics` says matter.
5. **Volume ramp + approval gate** (§3.5).

So: the engine fills the top of this funnel better than the VA could, then drops the prospect into a reply/measurement layer the playbook spends 3 of its 7 documents on and the engine barely models.

---

## 6. Recommended roadmap (prioritized)

If the goal is to make this engine able to *run* playbooks like the healthcare one — not just construction — this is the order that buys the most per unit work:

1. **Richer reply classification + nurture state.** Extend `sync.py` from 4 flags to the playbook's taxonomy (Interested / Not Now / Wrong Person / Unsub), add a `nurture` status with a re-engage date, and a wrong-person → re-source hook. *Biggest pipeline impact.* Optionally route "Interested" to Slack with context (you already have the Slack integration in `report.py`).
2. **Meeting + pipeline tracking.** Add a `meetings` concept (booked/held/stage/value) and surface positive-reply *rate vs. target*, meetings-per-100, and pipeline-$ in `report.py`. Without this the funnel has no bottom.
3. **Per-touch subject + named-variant swap in the brief schema.** Unblocks faithful multi-touch A/B and the anon→case-study swap that the "no proof point yet" reality demands.
4. **Compliance brief section.** `compliance:` block with `links_allowed_from_touch: 4`, enforced in `smartlead.py` sequence building.
5. **Volume ramp + optional approval gate.** A campaign-clock that ramps `daily_quota`, and an optional `load --require-approval` checkpoint (Slack default-approve mirrors Brad's Monday flow).
6. **Deliverability composite + kill-criteria alerting.** Spam-complaint rate, inbox-placement hook (Mailreach), and threshold alerts to Slack.

Items 1–2 close the engine's real weakness (it's blind below the reply line). Items 3–6 make any vertical playbook — healthcare, legal, construction — runnable from a single brief.

---

## 7. One-paragraph summary for Brad

The healthcare playbook and the email engine are complementary, not redundant. The engine already automates the playbook's most labor-intensive half — the weekly Apollo sourcing, dedup, signal-finding, and personalization that the docs budget two hours of VA time for every Monday — and does it with cost-gating, single-touch guarantees, and a quality-eval harness the manual playbook can't match. But the engine goes blind exactly where this vertical lives: nuanced reply triage (especially the "not now" nurture and "wrong person" referral loops that dominate a 9–18 month healthcare cycle), and bottom-of-funnel measurement (meetings, pipeline, kill-criteria). Conversely, the playbook is written as if all of that sourcing work must be manual — it has no concept that the engine has already solved it. The path forward is to teach the engine the reply-handling and funnel-measurement discipline the playbook documents so well, so that a vertical like healthcare becomes one `brief.yaml` away instead of a separate 7-document manual operation.
