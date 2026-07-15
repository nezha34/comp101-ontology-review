"""
metrics.py — thin wrapper around ontometrics/ontology_metrics.py's OntoQA-style
schema + instance metrics (Tartir et al., 2005 / OntoMetrics-style), so the
validation pipeline reuses one implementation instead of maintaining two.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ONTOMETRICS_DIR = Path(__file__).resolve().parents[2] / "ontometrics"
if str(_ONTOMETRICS_DIR) not in sys.path:
    sys.path.insert(0, str(_ONTOMETRICS_DIR))

from ontology_metrics import compute_metrics as compute_ontoqa_metrics  # noqa: E402,F401
