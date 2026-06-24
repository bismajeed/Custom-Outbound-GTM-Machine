# Cold-Email Messaging Standard (all industries)

The house style for every campaign the engine writes. Encoded in
`outbound/prompts/messaging.md` (the system prompt for all personalization), so it
applies to every brief by default. A brief may tweak its own copy (offer, CTA,
hooks), but this is the standard it starts from. Derived from a structured
taste-test (see `docs/construction-messaging-taste-test.txt` for the vote log).

## The standard

- **Subjects (no-signal):** concrete, **provocative-but-respectful** waste
  reframes. Point at the broken system/process, **never insult the people**. No
  "free", no "pilot", no flat labels, no cutesy/puns.
- **Subjects (signal):** name their tool / move / event as the angle or culprit —
  e.g. *"[their tool]'s costing your team hours."* Never a bare label like
  "[company] [topic]".
- **Subject format:** lowercase, mid-length (~3-7 words), no merge tags, no gimmicks.
- **Openers:** blunt half-truth / number-anchored / direct question. Concrete
  beats empathy — no "you're not alone" lead.
- **Body:** ruthlessly short — hook + **one** mechanism line + ask. No fabricated
  metrics.
- **Greeting:** `{first_name} -` (punchy, no "Hi").
- **CTA:** soft, specific, show-don't-tell — and it lives in a **P.S.**
- **Always:** plain ASCII (no em-dashes / curly quotes), lowercase subjects,
  deliverability-safe (no spam-trigger words).

## Template (fill the [brackets] per industry)

```
Subject: you're paying [role] to copy-paste

{first_name} -

[blunt half-truth / number / question about the manual busywork their team does].
[one line: how we automate it].

P.S. [soft specific CTA — show, don't pitch].
```

## Worked example (construction)

```
Subject: you're paying estimators to copy-paste

{first_name} -

Half of what your estimators do all day isn't estimating, it's re-typing bid data
between systems. we pull it straight out of your estimating system automatically.

P.S. want me to run it on one of your recent bids?
```

## Worked example (signal on file, e.g. a known tool)

```
Subject: [their tool]'s costing your team hours

{first_name} -

your team's already in [their tool], but [role] probably still does [task] by hand
every cycle. we pull that straight out of [their tool] automatically.

P.S. want me to run it on a recent [unit of their work]?
```

## What we rejected (and why)

- Flat labels ("company precon backlog") — no intrigue, nobody opens them.
- "free … pilot" — boring AND a spam-filter magnet.
- Cutesy/punny ("bidding's dumbest hour") — tries too hard.
- Loss-aversion / vague teasers ("the bid you didn't get to") — didn't land.
- Insults ("glorified typists") — aim at the system, not the people.
- Empathy-soft openers — concrete/blunt out-performs.
- Em-dashes — mojibake in spreadsheets + an AI tell.
