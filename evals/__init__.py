"""Message-quality evaluation pipeline.

A small, self-contained toolkit that sits alongside the outbound engine and
evaluates the *copy* it sends — subjects, bodies, and hook angles — rather than
the delivery mechanics. The flow (built incrementally) is:

    extract  -> pull every message used so far into a baseline CSV
    generate -> produce N variations per message (Claude)
    judge    -> LLM-as-judge scores variations against a versioned rubric
    rate     -> export the variations for a human (Brad) to rate, import back
    analyze  -> compare judge vs human scores: correlation, gaps, blind spots

Only `extract` is implemented so far. Everything reads the existing
``outbound`` package (briefs, DB, Smartlead client) so there is one source of
truth for what was actually sent.
"""

__version__ = "0.1.0"
