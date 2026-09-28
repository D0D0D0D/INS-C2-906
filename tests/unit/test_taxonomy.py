# INS-C2-906 — Unit tests: flag taxonomy / flag_key stability (design §5-2, §13-1)

import pytest

from src.services.taxonomy import (
    LV1_MISSING_FIELD,
    LV2_DATE_CONFLICT,
    compute_flag_key,
    normalize_anchor,
)


class TestFlagKeyStability:
    def test_same_kind_and_anchors_yield_same_key(self):
        key1 = compute_flag_key(LV2_DATE_CONFLICT, ("accident_report.incident_date", "medical_cert.first_visit_date"))
        key2 = compute_flag_key(LV2_DATE_CONFLICT, ("accident_report.incident_date", "medical_cert.first_visit_date"))
        assert key1 == key2

    def test_anchor_order_does_not_affect_key(self):
        """design §5-2: flag_key must not depend on document order."""
        key1 = compute_flag_key(LV2_DATE_CONFLICT, ("a.x", "b.y"))
        key2 = compute_flag_key(LV2_DATE_CONFLICT, ("b.y", "a.x"))
        assert key1 == key2

    def test_different_kind_yields_different_key(self):
        key1 = compute_flag_key(LV2_DATE_CONFLICT, ("a.x", "b.y"))
        key2 = compute_flag_key(LV1_MISSING_FIELD, ("a.x", "b.y"))
        assert key1 != key2

    def test_different_anchors_yield_different_key(self):
        key1 = compute_flag_key(LV2_DATE_CONFLICT, ("a.x", "b.y"))
        key2 = compute_flag_key(LV2_DATE_CONFLICT, ("a.x", "b.z"))
        assert key1 != key2

    def test_out_of_enum_kind_raises(self):
        """design §5-2: kind outside the fixed taxonomy must be rejected/discarded."""
        with pytest.raises(ValueError):
            compute_flag_key("NOT_A_REAL_KIND", ("a.x",))

    def test_normalize_anchor_shape(self):
        assert normalize_anchor("accident_report", "incident_date") == "accident_report.incident_date"
