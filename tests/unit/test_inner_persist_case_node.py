# INS-C2-906 — Unit tests: PersistCaseNode (inner, design §7-1 (3)(4)(5)/§13-1)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.persist_case_node import PersistCaseNode


def _base_assess_state(base_state, **overrides):
    state = {
        **base_state,
        "case_id": "case-1",
        "revision": 1,
        "prior_flags": [],
        "lv1_flags": [],
        "lv2_flags": [],
        "contract": {"policy_type": "auto", "coverage_items": ["collision"]},
        "documents": [{"doc_id": "d1", "doc_type": "invoice", "fields": {"claimed_amount": 1000}}],
        "lv3_result": {},
        "claimant_key": "claimant-1",
    }
    state.update(overrides)
    return state


class TestPersistCaseNodeBasics:
    def test_no_case_service_skips_persistence(self, base_state):
        node = PersistCaseNode(case_service=None)
        state = _base_assess_state(base_state)
        result = node.execute(state)
        assert result["persistence_status"] == "skipped"
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_revision_1_has_no_diff_computation(self, base_state, case_service):
        node = PersistCaseNode(case_service=case_service)
        state = _base_assess_state(base_state, revision=1, prior_flags=[])
        result = node.execute(state)
        assert result["revision_diff"] == {}
        assert result["persistence_status"] == "ok"

    def test_successful_insert_new_row(self, base_state, case_service):
        node = PersistCaseNode(case_service=case_service)
        state = _base_assess_state(base_state)
        result = node.execute(state)
        assert result["persistence_status"] == "ok"
        assert case_service.get_revision("case-1", 1) is not None

    def test_revision_2_computes_diff_against_prior(self, base_state, case_service):
        prior_flags = [{"kind": "LV1_MISSING_FIELD", "flag_key": "K1", "verdict": None}]
        node = PersistCaseNode(case_service=case_service)
        state = _base_assess_state(
            base_state,
            revision=2,
            prior_flags=prior_flags,
            lv1_flags=[{"kind": "LV1_MISSING_FIELD", "flag_key": "K1"}],
        )
        result = node.execute(state)
        assert result["revision_diff"]["continuing"] == ["K1"]
        assert result["persistence_status"] == "ok"


class TestPersistCaseNodeCollisionRetry:
    """design §7-1 (4)/§13-1: unique-constraint collision on (case_id, revision)
    -> reload latest -> reassign revision -> RECOMPUTE diff against new prior
    -> retry INSERT exactly once. Second failure -> persistence_status=failed,
    never raised (assessment response must never be blocked)."""

    def test_collision_reloads_and_retries_once_successfully(self, base_state, case_service):
        # Simulate a concurrent writer having already taken revision=1.
        case_service.seed(
            {
                "case_id": "case-1",
                "revision": 1,
                "consistency_flags": [{"kind": "LV1_MISSING_FIELD", "flag_key": "CONCURRENT_K", "verdict": None}],
                "final_verdict": "needs_review",
            }
        )
        node = PersistCaseNode(case_service=case_service)
        # This node thinks it's writing revision=1 too (stale provisional value).
        state = _base_assess_state(
            base_state, revision=1, prior_flags=[], lv1_flags=[{"kind": "LV1_MISSING_FIELD", "flag_key": "MY_K"}]
        )
        result = node.execute(state)

        assert result["persistence_status"] == "ok"
        assert result["revision"] == 2  # reassigned past the concurrent writer's row
        # diff recomputed against the ACTUAL prior (CONCURRENT_K), not the stale
        # empty prior_flags this node started with — CONCURRENT_K must appear
        # as resolved (or absent from current) and MY_K as new.
        assert "MY_K" in result["revision_diff"]["new"]
        assert case_service.get_revision("case-1", 2) is not None

    def test_collision_retry_failure_sets_persistence_failed_not_raise(self, base_state, case_service, monkeypatch):
        case_service.seed({"case_id": "case-1", "revision": 1, "consistency_flags": [], "final_verdict": "needs_review"})
        node = PersistCaseNode(case_service=case_service)

        call_count = {"n": 0}

        def always_fail(record):
            call_count["n"] += 1
            raise RuntimeError("persistent collision")

        monkeypatch.setattr(case_service, "insert_case_revision", always_fail)

        state = _base_assess_state(base_state, revision=1, prior_flags=[])
        result = node.execute(state)

        assert result["persistence_status"] == "failed"
        assert result["status"] == AgentStatus.SUCCESS.value  # findings still returned
        assert call_count["n"] == 2  # original attempt + exactly one retry

    def test_persistence_failure_never_blocks_findings_response(self, base_state, case_service, monkeypatch):
        node = PersistCaseNode(case_service=case_service)

        def always_fail(record):
            raise RuntimeError("db down")

        monkeypatch.setattr(case_service, "insert_case_revision", always_fail)
        monkeypatch.setattr(case_service, "get_latest_revision", lambda case_id: (_ for _ in ()).throw(RuntimeError("db down")))

        state = _base_assess_state(base_state)
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["persistence_status"] == "failed"
