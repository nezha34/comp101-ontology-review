"""
llm_judge.py — two-phase semantic/logical correctness judge.

Phase 1 screens every asserted edge in isolation (cheap, batched, high
recall — over-flagging is fine). Phase 2 takes only the phase-1 flags and
re-checks each against the local neighborhood of both endpoints, reaching
one of three verdicts — resolved / issue / unverifiable — and, for a
confirmed issue, a fix constrained to the flagged edge itself.

The LLM is READ-ONLY here: it never touches the ontology file. Its output
is just another section of the results dict / report, same as OOPS or the
structural checks. Nothing in this module writes to the source file.

Backed by a pluggable provider (lib/llm_providers.py) — local Ollama or
a hosted NVIDIA NIM endpoint — so the judge logic doesn't care which
backend is actually answering.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rdflib import RDF, RDFS, Graph
from rdflib.term import BNode, Literal

from .graph_utils import label, object_properties
from .llm_providers import LLMProvider, build_provider
from .review_store import load_reviewed, triple_key
from .prompts import (
    DEFAULT_RELATION_SEMANTICS,
    EVIDENCE_EXTERNAL_RELATIONS,
    PHASE1_SCHEMA,
    PHASE1_SYSTEM,
    PHASE2_SYSTEM,
    SCRATCHPAD_MARKERS,
    build_phase1_user_prompt,
    build_phase2_schema,
    build_phase2_user_prompt,
    downgrade_sentinel_fix,
    validate_phase2_output,
)

BATCH_SIZE = 6
CONTEXT_LIMIT = 20  # max neighborhood lines shown per node in phase 2
MAX_CONTEXT_ENTITY_LABELS = 15  # cap on the change_object enum in phase 2 —
# keeps the grammar constraint light on a small/quantized model regardless
# of how well-connected a node happens to be

# Parallel LLM calls. Hosted endpoints (NIM) benefit fully; a local Ollama
# server queues them GPU-side, which is still fine. Override with the
# SEMANTIC_WORKERS env var; 1 restores the old strictly-serial behaviour.
WORKERS = max(1, int(os.environ.get("SEMANTIC_WORKERS", "6")))

# On-disk verdict cache: reruns only pay LLM calls for claims whose content
# (or, in phase 2, whose local neighborhood) actually changed since the last
# run — the same incremental idea as course_genx beat hashes. Delete the file
# or bump PROMPT_VERSION (e.g. after editing prompts.py) to force a full
# re-judge. Keyed by model, so switching models also re-judges everything.
CACHE_PATH = Path(__file__).resolve().parent.parent / "results" / ".semantic_cache.json"
# v4 (2026-07-21): reason-before-verdict field order in both schemas;
# three-way phase-2 verdict (resolved/issue/unverifiable) replacing the
# resolved boolean; fixes constrained to the flagged edge only (no more
# free-text proposed_fix_triple the model could corrupt or mistarget);
# taughtIn gated out of phase 1 entirely pending lecture-transcript
# grounding (see EVIDENCE_EXTERNAL_RELATIONS in prompts.py).
# v5 (2026-07-21): mitigations for repetition-loop degeneration under
# grammar-constrained decoding — higher baseline sampling temperature,
# min_p, terser-reasoning-for-"correct" prompt change, tighter maxLength.
# Bumped so verdicts built under old schema/prompts/sampling aren't reused.
PROMPT_VERSION = "5"


class _VerdictCache:
    """Thread-safe {content-hash: verdict-dict} store, one JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}
        self.dirty = False

    def get(self, key: str):
        return self.data.get(key)

    def put(self, key: str, value: dict):
        with self.lock:
            self.data[key] = value
            self.dirty = True

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data))
            self.dirty = False


def _cache_key(*parts) -> str:
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str).encode()
    ).hexdigest()[:24]


def _scratchpad_leak(*texts: str) -> str | None:
    blob = " ".join(texts).lower()
    for marker in SCRATCHPAD_MARKERS:
        if marker in blob:
            return marker
    return None


# catches "asas/as/as/as/as" and "ergo, the/claim/is/incorrect.ergo, the/claim/is/incorrect."
# style collapse — a short token (or phrase) repeating 3+ times in a row.
_REPEAT_LOOP_RE = re.compile(r"(\S{2,24})([\s/.,;-]*\1){2,}", re.IGNORECASE)


def _looks_degenerate(text: str) -> bool:
    return bool(_REPEAT_LOOP_RE.search(text))


def _phase1_validator(result: dict) -> list[str]:
    errors = []
    seen_reasoning: dict[str, int] = {}
    for v in result.get("verdicts", []):
        text = str(v.get("reasoning", ""))
        idx = v.get("index")
        marker = _scratchpad_leak(text)
        if marker:
            errors.append(f"scratchpad leakage in verdict for index {idx}: {marker!r}")
        if _looks_degenerate(text):
            errors.append(f"degenerate repetition-loop text in verdict for index {idx}: {text!r}")
        # boilerplate/duplicate detection: skip "correct" verdicts — nobody
        # reads that reasoning, so it's not worth burning a retry to reword it.
        if v.get("verdict") != "correct" and text.strip():
            key = text.strip().lower()
            if key in seen_reasoning:
                errors.append(
                    f"duplicate reasoning text shared by indices {seen_reasoning[key]} and {idx}"
                )
            else:
                seen_reasoning[key] = idx
    return errors


def _call_llm(provider: LLMProvider, system: str, user: str, schema: dict,
              retries: int = 2, validator=None) -> dict:
    """Some models (local ones especially) occasionally return empty,
    malformed, or internally-inconsistent JSON even when the request is
    well-formed. Retry with a DIFFERENT temperature/seed each attempt —
    for a local model, retrying with identical sampling tends to fail the
    same way. `validator`, if given, receives the parsed result and
    returns a list of violation strings; any violations trigger a retry
    just like a request-level failure."""
    last_error = None
    for attempt in range(retries + 1):
        # 0.6/0.75/0.9 — low temperature under grammar-constrained decoding
        # pushes small local models toward near-greedy decoding, which is
        # exactly the regime that produces repetition loops; stay at/above
        # the model's stable range from the first attempt, not just on retry.
        temperature = 0.6 + 0.15 * attempt
        try:
            result = provider.chat_json(system, user, schema, temperature=temperature, seed=attempt)
        except Exception as e:
            last_error = RuntimeError(
                f"{provider.label} call failed on attempt {attempt+1}/{retries+1}: {e}"
            )
            if attempt < retries:
                time.sleep(15.0 * (attempt + 1) if "429" in str(e) else 2.0 * (attempt + 1))
            continue

        if validator:
            violations = validator(result)
            if violations:
                last_error = RuntimeError(
                    f"{provider.label} output failed validation on attempt "
                    f"{attempt+1}/{retries+1}: {'; '.join(violations)}"
                )
                if attempt < retries:
                    time.sleep(1.0)
                continue

        return result
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


def collect_claims(g: Graph, config: dict) -> tuple[dict, int]:
    """Group every object-property triple by relation name.

    Relations in EVIDENCE_EXTERNAL_RELATIONS (e.g. taughtIn) are excluded
    entirely rather than screened and then bucketed unverifiable later —
    without lecture-transcript grounding wired in, phase 1 would just be
    generating flags we already know phase 2 can't ground. Cheaper and
    more honest to not ask. Returns (by_relation, gated_count) where
    gated_count is how many triples were excluded this way.
    """
    by_relation = defaultdict(list)
    idx_counter = defaultdict(int)
    gated_count = 0

    include = set((config or {}).get("semantic_relations", [])) or None  # None = all

    for prop in object_properties(g):
        rel_name = _local_name(prop)
        if include is not None and rel_name not in include:
            continue
        if rel_name in EVIDENCE_EXTERNAL_RELATIONS:
            gated_count += sum(1 for _ in g.subject_objects(prop))
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
    return dict(by_relation), gated_count


def _p1_key(provider: LLMProvider, rel_name: str, meaning: str, c: dict) -> str:
    return _cache_key("p1", PROMPT_VERSION, provider.label, rel_name, meaning,
                      c["subject_uri"], c["object_uri"],
                      c["subject_label"], c["object_label"],
                      c["subject_types"], c["object_types"])


def run_phase1(g: Graph, config: dict, provider: LLMProvider,
               cache: _VerdictCache | None = None, workers: int = WORKERS,
               reviewed: dict | None = None) -> dict:
    """Screen every claim. Cached verdicts are reused without an LLM call;
    only cache misses are re-batched and judged, with batches running in
    parallel. A batch that fails (even after retries) is skipped, not
    fatal — one flaky relation shouldn't discard every other
    successfully-judged claim.

    `reviewed`, if given, is the {triple_key: {status, ...}} store from
    review_store.py — a triple a human has already dismissed or accepted
    is skipped before it ever reaches the LLM (no call, not even a cache
    lookup), so a settled call never gets re-litigated or re-paid-for.
    Returns {ok, error, all_claims, flagged, skipped_batches, cache_hits,
    reviewed_skipped, gated_count}."""
    by_relation, gated_count = collect_claims(g, config)
    all_claims = []
    flagged = []
    skipped_batches = []
    reviewed_skipped = []
    cache_hits = 0
    reviewed = reviewed or {}

    def record(claim: dict, verdict: dict, meaning: str):
        enriched = {**claim, **verdict, "relation_meaning": meaning}
        all_claims.append(enriched)
        if verdict["verdict"] != "correct":
            flagged.append(enriched)

    # split into cache hits (recorded immediately) and pending batches
    tasks = []  # (rel_name, meaning, [(key, claim), ...])
    for rel_name, claims in by_relation.items():
        meaning = _relation_meaning(rel_name, config)
        pending = []
        for c in claims:
            rkey = triple_key(c["subject_uri"], rel_name, c["object_uri"])
            if rkey in reviewed:
                reviewed_skipped.append({**reviewed[rkey], "predicate": rel_name,
                                          "subject": c["subject_label"], "object": c["object_label"]})
                continue
            key = _p1_key(provider, rel_name, meaning, c)
            hit = cache.get(key) if cache else None
            if hit is not None:
                record(c, hit, meaning)
                cache_hits += 1
            else:
                pending.append((key, c))
        for batch_start in range(0, len(pending), BATCH_SIZE):
            tasks.append((rel_name, meaning, pending[batch_start:batch_start + BATCH_SIZE]))

    def judge_batch(task):
        rel_name, meaning, keyed_batch = task
        user_prompt = build_phase1_user_prompt(rel_name, meaning, [c for _, c in keyed_batch])
        return _call_llm(provider, PHASE1_SYSTEM, user_prompt, PHASE1_SCHEMA, validator=_phase1_validator)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(judge_batch, t): t for t in tasks}
        for fut in as_completed(futures):
            rel_name, meaning, keyed_batch = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                skipped_batches.append({"relation": rel_name,
                                         "batch_start": keyed_batch[0][1]["index"],
                                         "batch_size": len(keyed_batch), "error": str(e)})
                continue
            by_index = {c["index"]: (k, c) for k, c in keyed_batch}
            for v in result.get("verdicts", []):
                entry = by_index.get(v.get("index"))
                if entry is None:
                    continue
                key, claim = entry
                verdict = {"verdict": v["verdict"], "reasoning": v["reasoning"]}
                if cache:
                    cache.put(key, verdict)
                record(claim, verdict, meaning)

    if cache:
        cache.flush()
    # parallel completion order is nondeterministic — restore a stable order
    all_claims.sort(key=lambda c: (c["predicate"], c["index"]))
    flagged.sort(key=lambda c: (c["predicate"], c["index"]))

    error = None
    if skipped_batches:
        error = f"{len(skipped_batches)} of {len(tasks)} batch(es) failed after retries and were skipped (see skipped_batches)"
    return {"ok": True, "error": error, "all_claims": all_claims, "flagged": flagged,
            "skipped_batches": skipped_batches, "cache_hits": cache_hits,
            "reviewed_skipped": reviewed_skipped, "gated_count": gated_count}


def _neighborhood(g: Graph, node, exclude_predicate, exclude_partner) -> list[str]:
    """Human-readable neighborhood lines for the prompt."""
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


def _neighborhood_labels(g: Graph, node, exclude_predicate, exclude_partner) -> list[str]:
    """Plain labels of real entity nodes (not literals) connected to
    `node`, in either direction. This is the closed vocabulary for the
    change_object enum — the model can only pick something actually
    shown to it, never invent or corrupt a name."""
    labels: set[str] = set()
    for p, o in g.predicate_objects(node):
        if p in (RDF.type, RDFS.label) or isinstance(o, (BNode, Literal)):
            continue
        if p == exclude_predicate and o == exclude_partner:
            continue
        labels.add(label(g, o))
    for s, p in g.subject_predicates(node):
        if p in (RDF.type, RDFS.label) or isinstance(s, BNode):
            continue
        if p == exclude_predicate and s == exclude_partner:
            continue
        labels.add(label(g, s))
    return sorted(labels)


def _build_fix_display(claim: dict, result: dict) -> tuple[str, str, str]:
    """Turn the model's constrained fix (fix_action/new_predicate/
    new_object) into a display triple, built entirely from data we
    already hold — the model never retypes the flagged triple, so there
    is nothing here for it to corrupt or mistarget. Returns
    (action_label, display_triple, rationale) in the vocabulary the
    report/webapp already render ("remove_triple"/"modify_triple"/"none")."""
    action = result.get("fix_action", "none")
    subj, pred, obj = claim["subject_label"], claim["predicate"], claim["object_label"]
    rationale = result.get("fix_rationale", "")

    if action == "remove_edge":
        return "remove_triple", f'"{subj}" --{pred}--> "{obj}"', rationale
    if action == "change_predicate":
        new_pred = result.get("new_predicate", "")
        return "modify_triple", f'"{subj}" --{new_pred}--> "{obj}"', rationale
    if action == "change_object":
        new_obj = result.get("new_object", "")
        return "modify_triple", f'"{subj}" --{pred}--> "{new_obj}"', rationale
    return "none", "", ""


def run_phase2(g: Graph, config: dict, flagged: list[dict], provider: LLMProvider,
               cache: _VerdictCache | None = None, workers: int = WORKERS) -> dict:
    """Re-verify each phase-1 flag against local context. Cached verdicts
    (keyed on the claim AND its neighborhood, so editing a nearby edge
    re-triggers the check) skip the LLM; the rest run in parallel. A claim
    whose LLM call fails (even after retries) is skipped, not fatal — same
    per-item degradation as phase 1. Returns
    {ok, error, resolved, issues, unverifiable, skipped_claims, cache_hits}."""
    resolved = []
    issues = []
    unverifiable = []
    skipped_claims = []
    cache_hits = 0

    relation_vocab = sorted({_local_name(p) for p in object_properties(g)})

    # neighborhoods and prompts are built serially — rdflib Graph reads are
    # not guaranteed thread-safe; only the HTTP calls go to the pool
    pending = []  # (pos, key, user_prompt, schema), in flagged order
    outcomes: dict[int, dict] = {}  # position in `flagged` -> llm/cached result
    for pos, claim in enumerate(flagged):
        subj_ctx = _neighborhood(g, claim["subject_uri"], claim["predicate_uri"], claim["object_uri"])
        obj_ctx = _neighborhood(g, claim["object_uri"], claim["predicate_uri"], claim["subject_uri"])
        key = _cache_key("p2", PROMPT_VERSION, provider.label, claim["predicate"],
                         claim["subject_uri"], claim["object_uri"],
                         claim["relation_meaning"], claim["verdict"], claim["reasoning"],
                         subj_ctx, obj_ctx)
        hit = cache.get(key) if cache else None
        if hit is not None:
            outcomes[pos] = hit
            cache_hits += 1
            continue

        subj_labels = _neighborhood_labels(g, claim["subject_uri"], claim["predicate_uri"], claim["object_uri"])
        obj_labels = _neighborhood_labels(g, claim["object_uri"], claim["predicate_uri"], claim["subject_uri"])
        context_labels = sorted(set(subj_labels + obj_labels))[:MAX_CONTEXT_ENTITY_LABELS]
        schema = build_phase2_schema(context_labels, relation_vocab, claim["predicate"])
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
        pending.append((pos, key, user_prompt, schema))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_call_llm, provider, PHASE2_SYSTEM, prompt, schema,
                        validator=validate_phase2_output): (pos, key)
            for pos, key, prompt, schema in pending
        }
        for fut in as_completed(futures):
            pos, key = futures[fut]
            claim = flagged[pos]
            try:
                result = fut.result()
            except Exception as e:
                skipped_claims.append({
                    "predicate": claim["predicate"], "subject": claim["subject_label"],
                    "object": claim["object_label"], "error": str(e),
                })
                continue
            result = downgrade_sentinel_fix(result)
            if cache:
                cache.put(key, result)
            outcomes[pos] = result

    if cache:
        cache.flush()

    for pos in sorted(outcomes):  # stable report order = phase-1 flag order
        claim, result = flagged[pos], outcomes[pos]
        fix_action, fix_triple, fix_rationale = _build_fix_display(claim, result)
        record = {
            "predicate": claim["predicate"],
            "subject": claim["subject_label"],
            "object": claim["object_label"],
            "subject_uri": str(claim["subject_uri"]),
            "object_uri": str(claim["object_uri"]),
            "phase1_verdict": claim["verdict"],
            "phase1_reasoning": claim["reasoning"],
            "verdict": result["verdict"],
            "evidence": result.get("evidence", ""),
            "phase2_reasoning": result.get("phase2_reasoning", ""),
            "issue_summary": result.get("issue_summary", ""),
            "resolution_explanation": result.get("phase2_reasoning", ""),
            "proposed_fix_action": fix_action,
            "proposed_fix_triple": fix_triple,
            "proposed_fix_rationale": fix_rationale,
            "confidence": 1.0,
        }
        if result["verdict"] == "resolved":
            resolved.append(record)
        elif result["verdict"] == "unverifiable":
            unverifiable.append(record)
        else:
            issues.append(record)

    error = None
    if skipped_claims:
        error = f"{len(skipped_claims)} of {len(flagged)} flagged claim(s) failed re-verification after retries and were skipped"
    return {"ok": True, "error": error, "resolved": resolved, "issues": issues,
            "unverifiable": unverifiable, "skipped_claims": skipped_claims, "cache_hits": cache_hits}


def run_semantic_judge(g: Graph, config: dict, provider_type: str = "ollama",
                        model: str = "gemma4:26b", ns_uri: str | None = None) -> dict:
    """Full two-phase pipeline. Never modifies `g` or any file.

    provider_type: "ollama" (local, default) or "nvidia_nim" (hosted,
    needs NVIDIA_API_KEY in the environment — see lib/llm_providers.py).

    ns_uri, if given, loads that namespace's persistent review store
    (results/reviewed/<slug>.json) so triples a human already dismissed or
    accepted on a prior run are skipped entirely rather than re-judged and
    re-argued every time. Generic across any ontology — keyed purely by
    namespace + triple, nothing domain-specific baked in.
    """
    try:
        provider = build_provider(provider_type, model)
    except Exception as e:
        return {"ok": False, "error": str(e), "model": model, "provider": provider_type,
                "phase1_total_claims": 0, "phase1_flagged": 0,
                "phase2_resolved": [], "issues": [], "phase2_unverifiable": [],
                "skipped_batches": [], "skipped_claims": [], "gated_count": 0}

    cache = _VerdictCache(CACHE_PATH)
    reviewed = load_reviewed(ns_uri) if ns_uri else {}

    p1 = run_phase1(g, config, provider, cache=cache, reviewed=reviewed)
    if not p1["ok"]:
        return {"ok": False, "error": p1["error"], "model": model, "provider": provider_type,
                "phase1_total_claims": len(p1["all_claims"]), "phase1_flagged": len(p1["flagged"]),
                "phase2_resolved": [], "issues": [], "phase2_unverifiable": [],
                "skipped_batches": p1.get("skipped_batches", []),
                "reviewed_skipped": p1.get("reviewed_skipped", []),
                "gated_count": p1.get("gated_count", 0)}

    if not p1["flagged"]:
        return {"ok": True, "error": p1["error"], "model": model, "provider": provider_type,
                "phase1_total_claims": len(p1["all_claims"]), "phase1_flagged": 0,
                "phase1_cache_hits": p1.get("cache_hits", 0),
                "phase2_resolved": [], "issues": [], "phase2_unverifiable": [],
                "skipped_batches": p1.get("skipped_batches", []),
                "reviewed_skipped": p1.get("reviewed_skipped", []),
                "gated_count": p1.get("gated_count", 0)}

    p2 = run_phase2(g, config, p1["flagged"], provider, cache=cache)
    combined_error = " | ".join(e for e in (p1["error"], p2["error"]) if e) or None
    return {
        "ok": p2["ok"],
        "error": combined_error,
        "model": model,
        "provider": provider_type,
        "phase1_total_claims": len(p1["all_claims"]),
        "phase1_flagged": len(p1["flagged"]),
        "phase1_cache_hits": p1.get("cache_hits", 0),
        "phase2_cache_hits": p2.get("cache_hits", 0),
        "phase2_resolved": p2["resolved"],
        "issues": p2["issues"],
        "phase2_unverifiable": p2["unverifiable"],
        "skipped_batches": p1.get("skipped_batches", []),
        "skipped_claims": p2.get("skipped_claims", []),
        "reviewed_skipped": p1.get("reviewed_skipped", []),
        "gated_count": p1.get("gated_count", 0),
    }
