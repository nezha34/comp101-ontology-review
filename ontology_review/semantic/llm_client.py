"""LLM provider wrapper (planned)."""

from __future__ import annotations


def complete(prompt: str, *, system: str | None = None) -> str:
    raise NotImplementedError("configure an LLM provider before semantic review")
