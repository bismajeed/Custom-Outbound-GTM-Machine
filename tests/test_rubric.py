"""Rubric loads, the hard-fail checker flags the right things, scores blend."""

from evals import rubric


def test_v1_loads_and_weights_sum_to_one():
    r = rubric.load_rubric("v1")
    assert r["version"] == "v1"
    assert abs(sum(r["weights"].values()) - 1.0) < 1e-6


def test_check_hard_fails_on_three_examples():
    r = rubric.load_rubric("v1")

    # 1. Clean, specific opener — should pass with no fails.
    clean = ("Saw DEW broke ground on the $4.5M fire station in North Hero — "
             "is your precon team still re-keying bid line items by hand?")
    assert rubric.check_hard_fails(clean, r) == []

    # 2. Generic template opener + buzzword — should trip at least one rule.
    spammy = "I came across your profile and love your innovative work."
    rules = {f["rule"] for f in rubric.check_hard_fails(spammy, r)}
    assert "generic_openers" in rules
    assert "buzzwords" in rules

    # 3. Appearance/age reference — should trip the protected-category rule.
    creepy = "You look young to be running estimating at a firm this size."
    rules = {f["rule"] for f in rubric.check_hard_fails(creepy, r)}
    assert "appearance_age_gender" in rules

    # Buzzwords stem-match their inflections, not just the bare token.
    assert any(f["rule"] == "buzzwords"
               for f in rubric.check_hard_fails("leveraging your data", r))


def test_compute_overall_weighted_blend():
    weights = {"specificity": 0.30, "pain_reference": 0.25, "clarity": 0.20,
               "conversational": 0.15, "credibility": 0.10}
    # All 4s -> weighted average is 4.0.
    scores = {k: 4 for k in weights}
    assert rubric.compute_overall(scores, weights) == 4.0
    # Partial scores renormalize by applied weight, not the full set.
    assert rubric.compute_overall({"specificity": 5}, weights) == 5.0
