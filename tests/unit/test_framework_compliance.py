# INS-C2-906 — TC-01..TC-08 Framework Compliance Tests


import pytest

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.main_node import ClaimWorkflowGraphNode
from src.nodes.post_process_node import OpinionComposeNode
from src.nodes.pre_process_node import ClaimIntakeNode
from src.schemas.claim_workflow_state import ClaimWorkflowState
from src.schemas.state import State


class TestTC01StateContract:
    """TC-01: Flat TypedDict type check pass, no Pydantic/dataclass."""

    def test_outer_state_is_typed_dict_subclass(self):
        assert hasattr(State, "__annotations__")
        # TypedDict classes do not use dataclasses.is_dataclass True
        import dataclasses

        assert not dataclasses.is_dataclass(State)

    def test_inner_state_is_typed_dict_subclass(self):
        import dataclasses

        assert hasattr(ClaimWorkflowState, "__annotations__")
        assert not dataclasses.is_dataclass(ClaimWorkflowState)

    def test_no_pydantic_basemodel_in_state_annotations(self):
        for name, annotation in {**State.__annotations__, **ClaimWorkflowState.__annotations__}.items():
            assert "BaseModel" not in str(annotation), f"{name}: {annotation}"


class TestTC02SecurityViolationError:
    """TC-02: SecurityViolationError fires on invalid input (S-5 JWT class scan)."""

    def test_security_violation_error_importable_and_raisable(self):
        with pytest.raises(SecurityViolationError):
            raise SecurityViolationError("test violation")

    def test_jwt_shaped_class_attribute_cannot_ship_undetected(self):
        """A JWT-shaped constant on a node must not be able to reach production.

        🔴 The MECHANISM changed between SDK versions — measured A/B on 2026-08-19:

          1.0.0 — ``BaseNode.__init_subclass__`` ran an S-5 scan and raised
                  ``SecurityViolationError [S-5] JWT pattern detected ... at import time``
                  the moment the class was defined.
          1.0.1 — **that scan no longer exists**; the class definition succeeds silently.
                  ``framework/nodes/base_node.py`` only re-exports ``SecurityViolationError``.

        So the *import-time runtime enforcement* is lost on 1.0.1. This test does not
        pretend otherwise: it asserts the claim that still holds on both versions —
        the value is **detected**, so it cannot be committed and shipped. Detection is
        what ``gate-credential-scan`` (blocking CI, ``scripts/check_credentials.py .``)
        and the S-3 output gate both key on.

        If a future SDK restores the import-time scan, this test keeps passing and
        ``test_import_time_scan_presence_is_recorded`` below is what changes.
        """
        from framework.security import detect_credentials

        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQ"
        findings = detect_credentials(jwt)
        assert findings, "the substrate detector no longer matches a JWT — nothing is left to stop it"
        assert any(f.get("type") == "jwt" for f in findings)

    def test_ci_credential_gate_reports_a_jwt_in_src(self, tmp_path):
        """The shipping barrier that survives 1.0.1 — verified by running the real gate.

        Mirrors the CI invocation exactly (``scripts/check_credentials.py .``) rather than
        re-implementing the check, so a change to the gate is caught here too.
        """
        import subprocess
        import sys
        from pathlib import Path

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "leaky.py").write_text(
            'API_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQ"\n'
        )
        checker = Path(__file__).parents[2] / "scripts" / "check_credentials.py"
        proc = subprocess.run([sys.executable, str(checker), str(tmp_path)],
                              capture_output=True, text=True)
        assert proc.returncode != 0, f"gate passed a committed JWT:\n{proc.stdout}"
        assert "[jwt]" in proc.stdout

    def test_import_time_scan_presence_is_recorded(self):
        """Pin WHICH mechanism the installed SDK provides, so a change is never silent.

        This is the test that flips when the SDK changes. 1.0.0 raised; 1.0.1 does not.
        Both outcomes are accepted here — the point is that the transition shows up as a
        deliberate edit to this assertion rather than as an unexplained red elsewhere.
        """
        import importlib.metadata as md

        from framework.nodes.base_node import BaseNode

        def _define():
            class _Probe(BaseNode):
                FAKE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQ"

                def execute(self, state):
                    return {}

                def _security_gate_input(self, state):
                    return state

                def _security_gate_output(self, result):
                    return result

        version = md.version("agenticstar-agentcore")
        try:
            _define()
            scans_at_import = False
        except SecurityViolationError:
            scans_at_import = True

        expected = {"1.0.0": True}.get(version, False)
        assert scans_at_import is expected, (
            f"SDK {version}: import-time S-5 class scan present={scans_at_import}, "
            f"expected={expected}. The framework changed — confirm whether the check moved "
            f"or was removed before updating this expectation."
        )


class TestTC03NoCredentialsInSrc:
    """TC-03: No JWT or API keys in src/ — confirmed by gate-credential-scan CI."""

    def test_no_credential_patterns_in_src(self):
        import re
        from pathlib import Path

        patterns = [r"sk-[a-zA-Z0-9]{20,}", r"eyJ[a-zA-Z0-9._-]{10,}", r"AKIA[A-Z0-9]{16}"]
        src_dir = Path(__file__).resolve().parents[2] / "src"
        violations = []
        for py_file in src_dir.rglob("*.py"):
            text = py_file.read_text()
            for pattern in patterns:
                if re.search(pattern, text):
                    violations.append(f"{py_file}: matched {pattern}")
        assert not violations, violations


class TestTC04InvocationContextConfigurableOnly:
    """TC-04: InvocationContext accessed via config["configurable"]/from_state() only."""

    def test_no_manual_invocation_context_construction_in_nodes(self):
        """Nodes must use InvocationContext.from_state(state), never construct
        InvocationContext(...) directly inside execute() (design/how-to:
        configure-secrets.md 'Common mistakes')."""
        import ast
        from pathlib import Path

        nodes_dir = Path(__file__).resolve().parents[2] / "src" / "nodes"
        violations = []
        for py_file in nodes_dir.rglob("*.py"):
            tree = ast.parse(py_file.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                    if name == "InvocationContext" and not (
                        isinstance(func, ast.Attribute) and func.attr == "from_state"
                    ):
                        violations.append(f"{py_file}:{node.lineno}")
        assert not violations, f"Manual InvocationContext() construction found: {violations}"


class TestTC05AuditLogging:
    """TC-05: emit_trace_event() called inside execute() for every node."""

    def test_every_node_module_calls_emit_trace_event(self):
        from pathlib import Path

        nodes_dir = Path(__file__).resolve().parents[2] / "src" / "nodes"
        missing = []
        for py_file in nodes_dir.rglob("*.py"):
            if py_file.name == "__init__.py":
                continue
            text = py_file.read_text()
            if "emit_trace_event(" not in text:
                missing.append(str(py_file))
        assert not missing, f"Modules missing emit_trace_event() call: {missing}"


class TestTC06SecurityGateInputNonBypassable:
    """TC-06: _security_gate_input is @final on FunctionNode; override raises TypeError."""

    def test_override_raises_type_error_at_class_definition(self):
        with pytest.raises(TypeError):

            class BadNode(FunctionNode):
                required_trust_level = TrustLevel.VERIFIED_EXTERNAL

                def execute(self, state):
                    return {}

                def _security_gate_input(self, state):  # prohibited override
                    return state

    def test_our_nodes_do_not_override_security_gate_input(self):
        for cls in (ClaimIntakeNode, OpinionComposeNode):
            assert "_security_gate_input" not in cls.__dict__


class TestTC07SecurityGateOutputNonBypassable:
    """TC-07: _security_gate_output is @final on FunctionNode; override raises TypeError."""

    def test_override_raises_type_error_at_class_definition(self):
        with pytest.raises(TypeError):

            class BadNode(FunctionNode):
                required_trust_level = TrustLevel.VERIFIED_EXTERNAL

                def execute(self, state):
                    return {}

                def _security_gate_output(self, result):  # prohibited override
                    return result

    def test_our_nodes_do_not_override_security_gate_output(self):
        for cls in (ClaimIntakeNode, OpinionComposeNode):
            assert "_security_gate_output" not in cls.__dict__


class TestTC08RequiredTrustLevelEnforced:
    """TC-08: required_trust_level declared and enforced; insufficient level refuses execution."""

    @pytest.mark.parametrize("node_cls", [ClaimIntakeNode, ClaimWorkflowGraphNode, OpinionComposeNode])
    def test_declared_explicitly_as_verified_external(self, node_cls):
        assert "required_trust_level" in node_cls.__dict__
        assert node_cls.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_anonymous_caller_denied_at_call_time(self, base_state):
        node = ClaimIntakeNode()
        state = {**base_state, "caller_trust_level": TrustLevel.ANONYMOUS.value, "input_context": {"mode": "assess"}}
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "S-1 trust gate denied" in result["error_log"][0]

    def test_verified_external_caller_allowed_to_execute(self, base_state):
        node = ClaimIntakeNode()
        state = {
            **base_state,
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "input_context": {"mode": "feedback", "case_id": "c1"},
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
