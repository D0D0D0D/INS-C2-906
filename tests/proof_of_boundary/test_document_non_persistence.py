# INS-C2-906 — PB: document / free-text non-persistence structural guard
# (design §6-3/§13-1 — "書類本文は格納しない" / "書類再送契約・fail-fast" test item)
#
# Verifies structurally that no code path in the assessment_cases service
# layer reads or writes document body text or free-text opinion content —
# the case revision thread carries only coded flags/verdicts, never raw
# document text. This is the automated back-check for the "PHI 非保持" claim
# underlying the document-resend-on-recheck design decision (§2-3).

import ast
from pathlib import Path

FORBIDDEN_FIELD_SUBSTRINGS = ("document_text", "doc_text", "ag_opinion", "opinion_text", "free_text", "raw_document")


def _service_source() -> str:
    path = Path(__file__).resolve().parents[2] / "src" / "services" / "assessment_case_service.py"
    return path.read_text()


class TestDocumentNonPersistence:
    def test_no_forbidden_field_names_in_service_source(self):
        source = _service_source().lower()
        violations = [f for f in FORBIDDEN_FIELD_SUBSTRINGS if f in source]
        assert not violations, f"assessment_case_service.py references forbidden fields: {violations}"

    def test_no_documents_key_read_from_db_records(self):
        """load_case_thread must only read consistency_flags/verdicts from a
        DB record — never a 'documents' or 'text' key (which would imply
        document bodies were persisted and are being re-read)."""
        path = Path(__file__).resolve().parents[2] / "src" / "nodes" / "inner" / "load_case_thread_node.py"
        tree = ast.parse(path.read_text())
        subscript_keys = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                if node.args and isinstance(node.args[0], ast.Constant):
                    subscript_keys.add(node.args[0].value)
        assert "documents" not in subscript_keys
        assert "text" not in subscript_keys

    def test_persist_case_record_builder_excludes_document_text(self):
        """persist_case's _build_record must not copy raw document `text`
        fields into the persisted record — only derived coded values."""
        path = Path(__file__).resolve().parents[2] / "src" / "nodes" / "inner" / "persist_case_node.py"
        source = path.read_text()
        # The only reference to doc.get(...) fields in _build_record must be
        # to structured `fields` sub-keys, never the raw `text` field itself.
        assert 'doc.get("text"' not in source
        assert "doc[\"text\"]" not in source
