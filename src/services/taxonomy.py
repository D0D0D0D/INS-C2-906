"""AgentCore Platform v1.0 — INS-C2-906 flag taxonomy (fixed enum kinds)"""

# docs/02_design.md §5-2 — flag_key identity contract:
#   flag_key = f"{kind}:{sorted(normalized anchors)}"
# `kind` MUST be a fixed taxonomy code value; the LLM structured-output schema
# constrains Lv2 output to LV2_KIND_VALUES only, and any kind outside the
# enum is discarded before compose (design §5-2 / §13-1).

from __future__ import annotations

# Lv1 — deterministic check kinds (design §5-1 / §5-2)
LV1_AMOUNT_OVER_LIMIT = "LV1_AMOUNT_OVER_LIMIT"
LV1_DEDUCTIBLE_MISMATCH = "LV1_DEDUCTIBLE_MISMATCH"
LV1_ITEMIZED_SUM_MISMATCH = "LV1_ITEMIZED_SUM_MISMATCH"
LV1_INCIDENT_OUT_OF_TERM = "LV1_INCIDENT_OUT_OF_TERM"
LV1_MISSING_FIELD = "LV1_MISSING_FIELD"

LV1_KIND_VALUES: frozenset[str] = frozenset(
    {
        LV1_AMOUNT_OVER_LIMIT,
        LV1_DEDUCTIBLE_MISMATCH,
        LV1_ITEMIZED_SUM_MISMATCH,
        LV1_INCIDENT_OUT_OF_TERM,
        LV1_MISSING_FIELD,
    }
)

# Lv2 — LLM cross-document contradiction kinds (design §5-2)
LV2_DATE_CONFLICT = "LV2_DATE_CONFLICT"
LV2_BODYPART_MISMATCH = "LV2_BODYPART_MISMATCH"
LV2_SUBJECT_MISMATCH = "LV2_SUBJECT_MISMATCH"
LV2_NARRATIVE_INCONSISTENCY = "LV2_NARRATIVE_INCONSISTENCY"
LV2_ROLE_CONFLICT = "LV2_ROLE_CONFLICT"

LV2_KIND_VALUES: frozenset[str] = frozenset(
    {
        LV2_DATE_CONFLICT,
        LV2_BODYPART_MISMATCH,
        LV2_SUBJECT_MISMATCH,
        LV2_NARRATIVE_INCONSISTENCY,
        LV2_ROLE_CONFLICT,
    }
)

ALL_KIND_VALUES: frozenset[str] = LV1_KIND_VALUES | LV2_KIND_VALUES


def normalize_anchor(doc_type: str, field_name: str) -> str:
    """Normalize a single anchor reference to `doc_type.field_name`.

    Anchors identify *which* extracted entities were involved in a flag,
    independent of document order or exact quote wording (design §5-2).
    """
    return f"{doc_type}.{field_name}"


def compute_flag_key(kind: str, anchors: tuple[str, ...]) -> str:
    """Compute the deterministic flag identity key (design §5-2).

    flag_key = f"{kind}:{sorted(normalized anchors)}" joined with "|".

    This is a pure function: same (kind, anchors) always yields the same
    flag_key, regardless of document order or LLM-generated quote wording.
    Used both at Lv1/Lv2 flag-generation time and by compute_revision_diff()
    for cross-revision matching.

    Raises ValueError if `kind` is not in the fixed taxonomy — callers must
    discard out-of-enum LLM output before calling this (design §5-2).
    """
    if kind not in ALL_KIND_VALUES:
        raise ValueError(f"compute_flag_key: kind '{kind}' is outside the fixed taxonomy")
    anchor_part = "|".join(sorted(anchors))
    return f"{kind}:{anchor_part}"
