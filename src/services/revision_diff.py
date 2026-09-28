"""AgentCore Platform v1.0 — INS-C2-906 revision diff (pure function)"""

# docs/02_design.md §2-3 / §3 / §7-1 (4) — compute_revision_diff is a PURE
# function: no I/O, no LLM calls. It diffs the current revision's flags
# against the prior revision's flags on `flag_key` identity, producing:
#   - resolved:   flag_key present in prior, absent in current
#   - new:        flag_key present in current, absent in prior
#   - continuing: flag_key present in both
# Dismissed-flag recurrence (a flag_key that a prior assessor dismissed and
# that reappears in the current revision) is annotated but NEVER auto-
# suppressed — the assessor must re-evaluate given the new document set
# (design §2-3 "再考の材料として出す — 自動抑制はしない").
#
# This function is called twice by persist_case on a unique-constraint
# collision: once against the provisional prior baseline (from
# load_case_thread), and again against the reloaded actual prior baseline
# after the collision is detected — no LLM re-execution is required either
# time (design §7-1 (4)).

from __future__ import annotations

from typing import Any


def compute_revision_diff(prior_flags: list[dict[str, Any]], current_flags: list[dict[str, Any]]) -> dict[str, Any]:
    """Diff current flags against prior-revision flags by flag_key.

    Args:
        prior_flags: flags (with `flag_key` and, optionally, `verdict`) from
            the immediately preceding revision. Empty list for revision=1.
        current_flags: flags produced by the current revision's Lv1+Lv2 run.

    Returns:
        {
            "resolved": [flag_key, ...],
            "new": [flag_key, ...],
            "continuing": [flag_key, ...],
            "dismissed_recurrence": [flag_key, ...],  # subset of "continuing" or "new"
                                                        # whose flag_key was previously
                                                        # dismissed by an assessor
            "counts": {"resolved": int, "new": int, "continuing": int, "dismissed_recurrence": int},
        }

    Pure function: same input always yields the same output. Does not call
    an LLM and does not perform any I/O.
    """
    prior_by_key: dict[str, dict[str, Any]] = {f["flag_key"]: f for f in prior_flags if "flag_key" in f}
    current_by_key: dict[str, dict[str, Any]] = {f["flag_key"]: f for f in current_flags if "flag_key" in f}

    prior_keys = set(prior_by_key.keys())
    current_keys = set(current_by_key.keys())

    resolved = sorted(prior_keys - current_keys)
    new = sorted(current_keys - prior_keys)
    continuing = sorted(prior_keys & current_keys)

    dismissed_recurrence = sorted(
        key
        for key in (new + continuing)
        if prior_by_key.get(key, {}).get("verdict") == "dismissed"
    )

    return {
        "resolved": resolved,
        "new": new,
        "continuing": continuing,
        "dismissed_recurrence": dismissed_recurrence,
        "counts": {
            "resolved": len(resolved),
            "new": len(new),
            "continuing": len(continuing),
            "dismissed_recurrence": len(dismissed_recurrence),
        },
    }
