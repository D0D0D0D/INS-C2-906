"""AgentCore Platform v1.0 — INS-C2-906 ingest row hashing/normalization"""

# docs/02_design.md §7-3 — each ingested row gets a source_row_hash (hash of
# normalized row content). Re-ingesting a row with an identical hash is
# skipped, not re-inserted (safe re-submission of the same file).

from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_source_row_hash(normalized_row: dict[str, Any]) -> str:
    """Hash a normalized row's content for duplicate-detection on reingest.

    Deterministic: same normalized content -> same hash, regardless of key
    insertion order (json.dumps sort_keys=True).
    """
    canonical = json.dumps(normalized_row, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
