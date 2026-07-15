"""Semantic review: ok | suspect | unclear + rationale (planned)."""

from __future__ import annotations


def review_edge(subject: str, predicate: str, obj: str, *, context: dict) -> dict:
    raise NotImplementedError("LLM semantic edge review not implemented yet")
