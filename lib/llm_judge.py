"""
llm_judge.py — two-phase semantic/logical correctness judge.

Phase 1 screens every asserted edge in isolation (cheap, batched, high
recall — over-flagging is fine). Phase 2 takes only the phase-1 flags and
re-checks each against the local neighborhood of both endpoints, either
dismissing the concern or writing an evidence-cited, single-fix proposal.

The LLM is READ-ONLY here: it never touches the ontology file. Its output
is just another section of the results dict / report, same as OOPS or the
structural checks. Nothing in this module writes to the source file.

Backed by a pluggable provider (lib/llm_providers.py) — local Ollama or
a hosted NVIDIA NIM endpoint — so the judge logic doesn't care which
backend is actually answering.
"""

from __future__ import annotations

from collections import defaultdict

from rdflib import RDF, RDFS, Graph
from rdflib.term import BNode

from .graph_utils import label, object_properties
from .llm_providers import LLMProvider, build_provider
from .prompts import (
    DEFAULT_RELATION_SEMANTICS,
    PHASE1_SCHEMA,
    PHASE1_SYSTEM,
    PHASE2_SCHEMA,
    PHASE2_SYSTEM,
    build_phase1_user_prompt,
    build_phase2_user_prompt,
)

BATCH_SIZE = 15
CONTEXT_LIMIT = 20  # max neighborhood lines shown per node in phase 2


def _call_llm(provider: LLMProvider, system: str, user: str, schema: dict, retries: int = 2) -> dict:
    """Some models (local ones especially) occasionally return empty or
    malformed JSON even when the request is well-formed — retry a couple
    of times before giving up, regardless of which provider is behind it."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            return provider.chat_json(system, user, schema)
        except Exception as e:
            last_error = RuntimeError(
                f"{provider.label} call failed on attempt {attempt+1}/{retries+1}: {e}"
            )
    raise last_error


def _local_name(uri) -> str:
    s = str(uri)
    return s.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _relation_meaning(relation_name: str, config: dict) -> str:
    custom = (config or {}).get("relation_semantics", {})
    if relation_name in custom:
        return custom[relation_name]
    if relation_name in DEFAULT_RELATION_SEMANTICS:
        return DEFAULT_RELATION_SEMANTICS[relation_name]
    return f'No explicit definition given — judge "{relation_name}" by its plain English meaning.'


def _types_of(g: Graph, node) -> list[str]:
    return [
        _local_name(t) for t in g.objects(node, RDF.type)
        if str(t) not in ("http://www.w3.org/2002/07/owl#NamedIndividual",)
    ]


def collect_claims(g: Graph, config: dict) -> dict:
    """Group every object-property triple by relation name.

    Returns {relation_name: [claim_dict, ...]}, index unique within relation.
    """
    by_relation = defaultdict(list)
    idx_counter = defaultdict(int)

    include = set((config or {}).get("semantic_relations", [])) or None  # None = all

    for prop in object_properties(g):
        rel_name = _local_name(prop)
        if include is not None and rel_name not in include:
            continue
        for s, o in g.subject_objects(prop):
            if isinstance(s, BNode) or isinstance(o, BNode):
                continue
            i = idx_counter[rel_name]
            idx_counter[rel_name] += 1
            by_relation[rel_name].append({
                "index": i,
                "predicate": rel_name,
                "predicate_uri": prop,
                "subject_uri": s,
                "object_uri": o,
                "subject_label": label(g, s),
                "object_label": label(g, o),
                "subject_types": _types_of(g, s),
                "object_types": _types_of(g, o),
            })
    return dict(by_relation)


def run_phase1(g: Graph, config: dict, provider: LLMProvider) -> dict:
    """Screen every claim, batch by batch. A batch that fails (even after
    retries) is skipped, not fatal — one flaky relation shouldn't discard
    every other successfully-judged claim. Returns
    {ok, error, all_claims, flagged, skipped_batches}."""
    by_relation = collect_claims(g, config)
    all_claims = []
    flagged = []
    skipped_batches = []

    for rel_name, claims in by_relation.items():
        meaning = _relation_meaning(rel_name, config)
        for batch_start in range(0, len(claims), BATCH_SIZE):
            batch = claims[batch_start:batch_start + BATCH_SIZE]
            user_prompt = build_phase1_user_prompt(rel_name, meaning, batch)
            try:
                result = _call_llm(provider, PHASE1_SYSTEM, user_prompt, PHASE1_SCHEMA)
            except Exception as e:
                skipped_batches.append({"relation": rel_name, "batch_start": batch_start,
                                         "batch_size": len(batch), "error": str(e)})
                continue

            by_index = {c["index"]: c for c in batch}
            for v in result.get("verdicts", []):
                claim = by_index.get(v.get("index"))
                if claim is None:
                    continue
                enriched = {**claim, "verdict": v["verdict"], "confidence": v["confidence"],
                            "reasoning": v["reasoning"], "relation_meaning": meaning}
                all_claims.append(enriched)
                if v["verdict"] != "correct":
                    flagged.append(enriched)

    error = None
    if skipped_batches:
        error = f"{len(skipped_batches)} of {sum((len(c)+BATCH_SIZE-1)//BATCH_SIZE for c in by_relation.values())} batch(es) failed after retries and were skipped (see skipped_batches)"
    return {"ok": True, "error": error, "all_claims": all_claims, "flagged": flagged, "skipped_batches": skipped_batches}


def _neighborhood(g: Graph, node, exclude_predicate, exclude_partner) -> list[str]:
    lines = []
    types = _types_of(g, node)
    if types:
        lines.append(f"is a {', '.join(types)}")

    for p, o in g.predicate_objects(node):
        if p in (RDF.type, RDFS.label):
            continue
        if p == exclude_predicate and o == exclude_partner:
            continue
        if isinstance(o, BNode):
            continue
        lines.append(f"{_local_name(p)} --> \"{label(g, o)}\"")

    for s, p in g.subject_predicates(node):
        if p in (RDF.type, RDFS.label):
            continue
        if p == exclude_predicate and s == exclude_partner:
            continue
        if isinstance(s, BNode):
            continue
        lines.append(f"\"{label(g, s)}\" --{_local_name(p)}--> this")

    return lines[:CONTEXT_LIMIT]


def run_phase2(g: Graph, config: dict, flagged: list[dict], provider: LLMProvider) -> dict:
    """Re-verify each phase-1 flag against local context. A claim whose LLM
    call fails (even after retries) is skipped, not fatal — same
    per-item degradation as phase 1. Returns
    {ok, error, resolved, issues, skipped_claims}."""
    resolved = []
    issues = []
    skipped_claims = []

    for claim in flagged:
        subj_ctx = _neighborhood(g, claim["subject_uri"], claim["predicate_uri"], claim["object_uri"])
        obj_ctx = _neighborhood(g, claim["object_uri"], claim["predicate_uri"], claim["subject_uri"])

        user_prompt = build_phase2_user_prompt(
            relation_name=claim["predicate"],
            relation_meaning=claim["relation_meaning"],
            subject_label=claim["subject_label"],
            object_label=claim["object_label"],
            phase1_verdict=claim["verdict"],
            phase1_reasoning=claim["reasoning"],
            subject_context=subj_ctx,
            object_context=obj_ctx,
        )
        try:
            result = _call_llm(provider, PHASE2_SYSTEM, user_prompt, PHASE2_SCHEMA)
        except Exception as e:
            skipped_claims.append({
                "predicate": claim["predicate"], "subject": claim["subject_label"],
                "object": claim["object_label"], "error": str(e),
            })
            continue

        record = {
            "predicate": claim["predicate"],
            "subject": claim["subject_label"],
            "object": claim["object_label"],
            "subject_uri": str(claim["subject_uri"]),
            "object_uri": str(claim["object_uri"]),
            "phase1_verdict": claim["verdict"],
            "phase1_reasoning": claim["reasoning"],
            **result,
        }
        if result.get("resolved"):
            resolved.append(record)
        else:
            issues.append(record)

    error = None
    if skipped_claims:
        error = f"{len(skipped_claims)} of {len(flagged)} flagged claim(s) failed re-verification after retries and were skipped"
    return {"ok": True, "error": error, "resolved": resolved, "issues": issues, "skipped_claims": skipped_claims}


def run_semantic_judge(g: Graph, config: dict, provider_type: str = "ollama",
                        model: str = "gemma4:26b") -> dict:
    """Full two-phase pipeline. Never modifies `g` or any file.

    provider_type: "ollama" (local, default) or "nvidia_nim" (hosted,
    needs NVIDIA_API_KEY in the environment — see lib/llm_providers.py).
    """
    try:
        provider = build_provider(provider_type, model)
    except Exception as e:
        return {"ok": False, "error": str(e), "model": model, "provider": provider_type,
                "phase1_total_claims": 0, "phase1_flagged": 0,
                "phase2_resolved": [], "issues": [], "skipped_batches": [], "skipped_claims": []}

    p1 = run_phase1(g, config, provider)
    if not p1["ok"]:
        return {"ok": False, "error": p1["error"], "model": model, "provider": provider_type,
                "phase1_total_claims": len(p1["all_claims"]), "phase1_flagged": len(p1["flagged"]),
                "phase2_resolved": [], "issues": [], "skipped_batches": p1.get("skipped_batches", [])}

    if not p1["flagged"]:
        return {"ok": True, "error": p1["error"], "model": model, "provider": provider_type,
                "phase1_total_claims": len(p1["all_claims"]), "phase1_flagged": 0,
                "phase2_resolved": [], "issues": [], "skipped_batches": p1.get("skipped_batches", [])}

    p2 = run_phase2(g, config, p1["flagged"], provider)
    combined_error = " | ".join(e for e in (p1["error"], p2["error"]) if e) or None
    return {
        "ok": p2["ok"],
        "error": combined_error,
        "model": model,
        "provider": provider_type,
        "phase1_total_claims": len(p1["all_claims"]),
        "phase1_flagged": len(p1["flagged"]),
        "phase2_resolved": p2["resolved"],
        "issues": p2["issues"],
        "skipped_batches": p1.get("skipped_batches", []),
        "skipped_claims": p2.get("skipped_claims", []),
    }
