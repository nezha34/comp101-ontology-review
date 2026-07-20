"""
review_store.py — persistent memory of human verdicts on semantic-judge
findings, so a dismissed or accepted finding doesn't get re-flagged (and
re-argued) on every future run.

Generic by design: keyed only by ontology namespace + the exact triple
(subject_uri, predicate, object_uri). Nothing here is specific to any one
ontology's content or domain — a coworker validating a completely different
ontology gets their own store, scoped by their own namespace, for free.

One JSON file per namespace under results/reviewed/. Not a cache (verdicts
here never expire on their own) -- a human decision stands until a human
changes it, same as the accept/dismiss buttons that write it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

REVIEW_DIR = Path(__file__).resolve().parent.parent / "results" / "reviewed"


def _slug(ns_uri: str) -> str:
    s = re.sub(r"^https?://", "", ns_uri or "unknown")
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_")
    return s or "unknown"


def _path(ns_uri: str) -> Path:
    return REVIEW_DIR / f"{_slug(ns_uri)}.json"


def triple_key(subject_uri, predicate: str, object_uri) -> str:
    return f"{subject_uri}|{predicate}|{object_uri}"


def load_reviewed(ns_uri: str) -> dict:
    """{triple_key: {"status": "dismissed"|"accepted", "reviewed_at": iso, "note": str}}"""
    p = _path(ns_uri)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def record_decision(ns_uri: str, subject_uri, predicate: str, object_uri,
                     status: str, note: str = "") -> None:
    """status: 'dismissed' (human disagrees with the LLM, edge stands) or
    'accepted' (human agrees it's wrong, fix is queued/pending). Either way
    it's settled -- future runs won't re-judge this exact triple until this
    entry is removed or overwritten by a fresh decision."""
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    data = load_reviewed(ns_uri)
    key = triple_key(subject_uri, predicate, object_uri)
    data[key] = {
        "status": status,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
    }
    _path(ns_uri).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def forget_decision(ns_uri: str, subject_uri, predicate: str, object_uri) -> None:
    """Remove a prior verdict so the triple goes back into normal judging."""
    p = _path(ns_uri)
    data = load_reviewed(ns_uri)
    key = triple_key(subject_uri, predicate, object_uri)
    if key in data:
        del data[key]
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
