# INS-C2-906 — Unit tests: compute_revision_diff purity (design §2-3, §7-1 (4), §13-1)

from src.services.revision_diff import compute_revision_diff


class TestRevisionDiffPurity:
    def test_pure_function_same_input_same_output(self):
        prior = [{"flag_key": "K1"}, {"flag_key": "K2"}]
        current = [{"flag_key": "K2"}, {"flag_key": "K3"}]
        result1 = compute_revision_diff(prior, current)
        result2 = compute_revision_diff(prior, current)
        assert result1 == result2

    def test_three_way_classification(self):
        prior = [{"flag_key": "RESOLVED_1"}, {"flag_key": "CONTINUING_1"}]
        current = [{"flag_key": "CONTINUING_1"}, {"flag_key": "NEW_1"}]
        diff = compute_revision_diff(prior, current)
        assert diff["resolved"] == ["RESOLVED_1"]
        assert diff["new"] == ["NEW_1"]
        assert diff["continuing"] == ["CONTINUING_1"]
        assert diff["counts"] == {"resolved": 1, "new": 1, "continuing": 1, "dismissed_recurrence": 0}

    def test_revision_1_has_empty_diff(self):
        """design §2-3: revision=1 has no prior — diff must be empty, not error."""
        diff = compute_revision_diff([], [{"flag_key": "K1"}])
        assert diff["resolved"] == []
        assert diff["continuing"] == []
        assert diff["new"] == ["K1"]

    def test_dismissed_flag_recurrence_annotated_not_suppressed(self):
        """design §2-3: a previously-dismissed flag_key reappearing must be
        annotated (dismissed_recurrence) but still present in new/continuing —
        never auto-suppressed."""
        prior = [{"flag_key": "K1", "verdict": "dismissed"}]
        current = [{"flag_key": "K1"}]
        diff = compute_revision_diff(prior, current)
        assert "K1" in diff["continuing"]
        assert "K1" in diff["dismissed_recurrence"]

    def test_dismissed_recurrence_only_flags_actual_dismissals(self):
        prior = [{"flag_key": "K1", "verdict": "valid"}, {"flag_key": "K2", "verdict": "dismissed"}]
        current = [{"flag_key": "K1"}, {"flag_key": "K2"}]
        diff = compute_revision_diff(prior, current)
        assert diff["dismissed_recurrence"] == ["K2"]

    def test_no_llm_dependency(self):
        """Pure function contract (design §7-1 (4)): compute_revision_diff must
        not import any LLM client — recomputing the diff on collision retry
        must never trigger an LLM call."""
        import ast
        import inspect

        import src.services.revision_diff as module

        tree = ast.parse(inspect.getsource(module))
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)

        assert not any("llm" in name.lower() for name in imported_names), (
            f"revision_diff.py must have zero LLM imports, found: {imported_names}"
        )
