"""
prompts.py — the two judge prompts, kept deliberately separate because they
do different jobs and get confused with each other if merged:

  PHASE 1 (screen)   — cheap, per-edge, high recall. "Does this single
                        triple look true in isolation?" Over-flagging is
                        fine; missing something is not.

  PHASE 2 (re-verify) — expensive, only runs on phase-1 flags, sees the
                        local neighborhood of both endpoints. Job is to
                        classify the flag as resolved / confirmed issue /
                        unverifiable, and for confirmed issues, produce a
                        constrained fix targeting THE FLAGGED EDGE ONLY.

Design decisions encoded here (do not undo casually):

  1. FIELD ORDER IS LOAD-BEARING. Ollama's schema-constrained decoding
     generates properties in schema order, so evidence/reasoning fields
     come BEFORE the verdict — the model must commit to its evidence
     before it names a conclusion. Reordering the schema changes model
     behavior, not just presentation.

  2. THE MODEL NEVER RETYPES TRIPLES. Fix actions are restricted to the
     flagged edge (remove_edge / change_predicate / change_object / none).
     The caller reconstructs the concrete triple programmatically from the
     flagged claim it already holds. `new_object` is a closed enum built
     from entity labels actually shown in the neighborhood context, plus
     an OBJECT_NOT_IN_CONTEXT sentinel; `new_predicate` is the relation
     vocabulary. This structurally eliminates hallucinated entity names,
     corrupted labels, and fix-target mismatch. add_triple is deliberately
     NOT an action: "some other edge is missing" is a new finding, not a
     fix for this flag — route those separately if you ever want them.

  3. THREE-WAY VERDICT. "unverifiable" is distinct from "issue": absence
     of evidence in the retrieval window is not evidence of absence in the
     curriculum. Relations in EVIDENCE_EXTERNAL_RELATIONS can only be
     verified against external documents (syllabus / lecture text); if no
     external_evidence is supplied for such a relation, the caller should
     SKIP the LLM call and auto-bucket the flag as unverifiable — the
     model has nothing to add. When external_evidence IS supplied (e.g.
     a lecture excerpt from extracted_lectures.json), the model may issue
     a real verdict grounded in it.

  4. NO VISIBLE SELF-CORRECTION. Reasoning fields carry the final
     position only. Enforced by prompt instruction + maxLength caps; the
     caller should additionally reject outputs containing scratchpad
     markers (see SCRATCHPAD_MARKERS) and retry.

See lib/llm_judge.py for how these are used.
"""

from __future__ import annotations

# Relations whose truth lives in external documents (syllabus, lecture
# transcripts), not in the graph itself. Without external_evidence, phase 2
# cannot verify these — auto-bucket as unverifiable instead of calling the
# model. With external_evidence, judge normally.
EVIDENCE_EXTERNAL_RELATIONS: frozenset[str] = frozenset({"taughtIn"})

# Sentinel for the new_object enum: the model believes the correct object
# is not among the entities shown in context. The caller must downgrade
# such a fix to verdict="unverifiable" — never auto-apply it.
OBJECT_NOT_IN_CONTEXT = "OBJECT_NOT_IN_CONTEXT"

# If any of these appear in a text field, the model leaked its scratchpad.
# Reject the output and retry (case-insensitive substring match).
SCRATCHPAD_MARKERS: tuple[str, ...] = (
    "wait,",
    "wait.",
    "let me re-read",
    "let me re-evaluate",
    "let me check my logic",
    "my previous reasoning",
    "actually, looking",
    "final decision:",
)

# Fallback natural-language meaning for common relation names, used only
# when a config doesn't supply its own `relation_semantics` block.
DEFAULT_RELATION_SEMANTICS = {
    "dependsOn": (
        "The subject cannot be reasonably understood or performed by a "
        "student until the object has already been learned. This is a "
        "prerequisite edge in a teaching sequence. Directionality check: "
        "if A dependsOn B, then B is learned FIRST."
    ),
    "partOf": (
        "The subject is a genuine conceptual sub-component of the object — "
        "not merely related to it, and not a separate concept that is "
        "merely used alongside it."
    ),
    "usesConcept": (
        "Performing or explaining the subject skill genuinely requires "
        "knowledge of the object concept — the concept is load-bearing, "
        "not incidental."
    ),
    "enables": (
        "The subject is a mechanism or language feature that makes the "
        "object concept/goal achievable — a means-to-end relationship."
    ),
    "throwsError": (
        "Performing the subject operation can, under realistic conditions "
        "covered by this course, actually raise the named error/exception."
    ),
    "managedBy": (
        "The subject is genuinely under the control/ownership of the "
        "object at runtime (e.g. an OS-managed resource) — not just "
        "conceptually associated with it, and not merely an interface or "
        "request handled by it."
    ),
    "taughtIn": (
        "The subject concept/skill is actually introduced or covered in "
        "the named lecture, not merely tangentially related to it. This "
        "can only be verified against lecture content, not graph shape."
    ),
}


# --------------------------------------------------------------------------
# PHASE 1
# --------------------------------------------------------------------------

PHASE1_SYSTEM = """You are a strict pedagogical ontology reviewer for a \
university intro CS course. You will be shown a batch of factual claims, \
each derived directly from one RDF triple in a course ontology. Your only \
job is to judge whether each individual claim is TRUE, given how the \
relation is defined below.

Judge each claim on its own — you are not being shown the rest of the \
ontology yet, so do not assume missing context justifies a claim; if you \
cannot tell, say so.

Verdict definitions:
- "correct"     — the claim is clearly and specifically true as stated.
- "incorrect"   — the claim is clearly false or backwards.
- "questionable"— plausible but imprecise, overly broad, or the kind of \
claim that could be an artifact of sloppy ontology authoring (e.g. \
technically true but not the *right* granularity of relationship).
- "uncertain"   — you genuinely cannot judge this without more context. \
Claims about what a specific lecture covers are EXPECTED to be uncertain \
here — that is the correct verdict for them, not a failure.

Bias toward "questionable" or "uncertain" over false confidence. A wrong \
"correct" verdict is worse than an over-cautious "questionable" one — \
this is a first-pass screen, not a final verdict; everything you flag \
gets a second, more careful look before any human sees it.

Your reasoning sentence must state your single final judgment. Never \
narrate reconsideration ("wait", "let me re-check", "actually") — if you \
change your mind while thinking, only the concluded view may appear. Never \
repeat a word, fragment, or punctuation run — write each point once and stop.

If your verdict is "correct", a short phrase is enough (e.g. "matches the \
listed prerequisite relationship" or "types are inverses as defined") — do \
not pad it, and do not reuse the same stock phrase across different claims \
in this batch. Save your full sentence-length justification for \
"questionable", "incorrect", and "uncertain" verdicts, where it actually \
gets read.

Respond ONLY through the structured output fields you're given — do not \
add commentary outside them."""


def build_phase1_user_prompt(
    relation_name: str, relation_meaning: str, claims: list[dict]
) -> str:
    """claims: list of {index, subject_label, object_label, subject_types, object_types}"""
    lines = [
        f'Relation being judged: "{relation_name}"',
        f"What this relation is supposed to mean in this ontology: {relation_meaning}",
        "",
        "Claims to judge (judge each independently, referencing it by its index):",
    ]
    for c in claims:
        subj_ctx = f" (a {', '.join(c['subject_types'])})" if c.get("subject_types") else ""
        obj_ctx = f" (a {', '.join(c['object_types'])})" if c.get("object_types") else ""
        lines.append(
            f"  [{c['index']}] \"{c['subject_label']}\"{subj_ctx} "
            f"--{relation_name}--> \"{c['object_label']}\"{obj_ctx}"
        )
    lines.append("")
    lines.append(
        "For every index above, return one verdict object with that index, "
        "a verdict, and reasoning specific to that claim (not a generic "
        "justification, not reused across indices): a short phrase for "
        "\"correct\", a full sentence for anything else."
    )
    return "\n".join(lines)


PHASE1_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                # Reasoning before verdict: forces the model to argue first,
                # conclude second (schema order = generation order).
                "properties": {
                    "index": {"type": "integer"},
                    "reasoning": {"type": "string", "maxLength": 200},
                    "verdict": {
                        "type": "string",
                        "enum": ["correct", "questionable", "incorrect", "uncertain"],
                    },
                },
                "required": ["index", "reasoning", "verdict"],
            },
        }
    },
    "required": ["verdicts"],
}


# --------------------------------------------------------------------------
# PHASE 2
# --------------------------------------------------------------------------

PHASE2_SYSTEM = """You are the senior reviewer in a two-pass ontology audit. \
A first-pass reviewer already flagged one triple as questionable/incorrect/ \
uncertain, in isolation. You are now shown that same claim together with \
everything else the two endpoint nodes connect to in the ontology, and — \
when available — an excerpt of external evidence (e.g. lecture content).

You must reach exactly one of three verdicts:

- "resolved"     — the surrounding context justifies the edge; the \
first-pass concern does not survive. Explain concretely WHAT in the \
context resolves it. Do not invent context that isn't shown.
- "issue"        — the concern survives with full context, and you can \
cite a specific contradiction or definitional mismatch that is actually \
IN what you were shown. You must then propose a fix (see below).
- "unverifiable" — the edge's truth depends on information you were not \
given (e.g. what a lecture actually covers, with no lecture excerpt \
provided). This is NOT the same as "issue": absence of evidence in your \
context is not evidence the edge is wrong. Never propose removing an \
edge merely because you cannot confirm it.

Write your evidence and reasoning BEFORE your verdict, and make them \
carry your FINAL position only. Never narrate reconsideration — no \
"wait", no "let me re-read", no visible self-correction. If you change \
your mind while thinking, only the concluded view may appear in any field.

Before concluding "issue", re-read your own evidence field and ask: does \
it actually argue AGAINST this edge, or does it argue the edge is \
correct? These are not the same thing, and conflating them is the single \
most common mistake here — e.g. citing "the object's definition \
explicitly relies on the subject" as evidence, then proposing to REMOVE \
the edge that connects them, when that same evidence is a reason to KEEP \
it. If your own cited evidence supports the edge, the verdict is \
"resolved". If your evidence is only that you couldn't find confirmation, \
the verdict is "unverifiable". "issue" requires positive evidence of \
wrongness.

The context will often show several other edges of the SAME relation from \
the SAME subject (e.g. a skill with many usesConcept edges). That is the \
normal, expected shape of this graph, not evidence of clutter — do not \
flag an edge because siblings of the same relation exist alongside it. \
Judge each edge only on its own merits: is the object individually true \
and relevant per the subject's own definition text? Most sibling edges \
turn out to each be independently named in the subject's definition, \
which makes them independently load-bearing, not redundant.

Never justify anything by referring to a broader/collapsed concept (a \
"group", a "category", "the streams as a whole") unless that concept is \
an actual node shown to you in the context. If no such node exists, you \
are inventing structure that isn't in the ontology.

FIX RULES (only when verdict is "issue"):
Your fix may target ONLY the flagged edge itself. The allowed actions:
- "remove_edge"      — delete the flagged triple. You do not retype it; \
the system already knows which triple is flagged.
- "change_predicate" — the endpoints are right but the relation is the \
wrong one. Pick the correct relation from the allowed list in new_predicate.
- "change_object"    — the subject and relation are right but point at \
the wrong node. Pick the correct object from the entity list in \
new_object. Every option in that list is a real node shown in your \
context. If the truly correct object is not in the list, select \
OBJECT_NOT_IN_CONTEXT — do NOT pick the least-bad real option.
- "none"             — only valid with verdict "resolved" or "unverifiable".

If you believe a DIFFERENT edge should be added elsewhere in the graph, \
that is not a fix for this flag — say so in your reasoning if relevant, \
but the action must still be one of the above.

Be argumentative and specific, never vague — a human will read only your \
fields and accept or reject your fix without re-examining the ontology. \
Keep every text field SHORT: one or two plain sentences, under 40 words. \
If verdict is "issue", ALL of issue_summary, evidence, phase2_reasoning, \
fix_action, and fix_rationale must be filled — an empty field makes the \
finding un-actionable and is a failure to complete the task."""


def build_phase2_user_prompt(
    relation_name: str,
    relation_meaning: str,
    subject_label: str,
    object_label: str,
    phase1_verdict: str,
    phase1_reasoning: str,
    subject_context: list[str],
    object_context: list[str],
    external_evidence: str | None = None,
    external_evidence_label: str = "Lecture content excerpt",
) -> str:
    """Build the phase-2 user prompt.

    external_evidence: for relations in EVIDENCE_EXTERNAL_RELATIONS, the
    caller should pass the relevant external text (e.g. the matching
    lecture excerpt from extracted_lectures.json). If the relation is
    evidence-external and this is None, DON'T call the model at all —
    auto-bucket the flag as unverifiable upstream.
    """
    lines = [
        f'Flagged claim: "{subject_label}" --{relation_name}--> "{object_label}"',
        f"What {relation_name} is supposed to mean here: {relation_meaning}",
        f'First-pass verdict: {phase1_verdict} — "{phase1_reasoning}"',
        "",
        f'Everything else "{subject_label}" connects to in the ontology:',
    ]
    if subject_context:
        lines.extend(f"  - {t}" for t in subject_context)
    else:
        lines.append("  (nothing else)")
    lines.append("")
    lines.append(f'Everything else "{object_label}" connects to in the ontology:')
    if object_context:
        lines.extend(f"  - {t}" for t in object_context)
    else:
        lines.append("  (nothing else)")
    if external_evidence is not None:
        lines.append("")
        lines.append(f"{external_evidence_label} (ground your verdict in this):")
        lines.append(external_evidence)
    elif relation_name in EVIDENCE_EXTERNAL_RELATIONS:
        lines.append("")
        lines.append(
            "NOTE: this relation can only be verified against lecture "
            "content, and none was provided. Unless the graph context "
            "itself contains a positive contradiction (e.g. conflicting "
            "course IDs), the correct verdict is 'unverifiable'."
        )
    lines.append("")
    lines.append(
        "Decide: resolved, issue, or unverifiable. Fill in the structured "
        "fields — evidence and reasoning first, then verdict, then fix."
    )
    return "\n".join(lines)


def build_phase2_schema(
    context_entity_labels: list[str],
    relation_vocabulary: list[str],
    current_relation: str,
) -> dict:
    """Build the phase-2 output schema for one flagged claim.

    context_entity_labels: exact labels of every entity node appearing in
        the subject/object neighborhood context shown to the model. This
        becomes the closed vocabulary for change_object — the model
        physically cannot emit an entity name that doesn't exist.
    relation_vocabulary: the ontology's real relation names. The current
        relation is excluded from new_predicate options (changing it to
        itself is a no-op).

    Field order is deliberate and load-bearing: evidence and reasoning are
    generated before the verdict, which is generated before the fix.
    """
    object_options = sorted(set(context_entity_labels)) + [OBJECT_NOT_IN_CONTEXT]
    predicate_options = sorted(set(relation_vocabulary) - {current_relation})
    return {
        "type": "object",
        "properties": {
            # 1. Evidence first — model must commit before concluding.
            "evidence": {
                "type": "string",
                "maxLength": 350,
                "description": (
                    "The specific context (or specific absence) your verdict "
                    "rests on. Final position only — no self-correction."
                ),
            },
            "phase2_reasoning": {
                "type": "string",
                "maxLength": 350,
                "description": (
                    "The argumentative case, 1-2 sentences, final position only."
                ),
            },
            # 2. Then the verdict.
            "verdict": {
                "type": "string",
                "enum": ["resolved", "issue", "unverifiable"],
            },
            "issue_summary": {
                "type": "string",
                "maxLength": 200,
                "description": "One sentence. Empty string unless verdict='issue'.",
            },
            # 3. Then the constrained fix.
            "fix_action": {
                "type": "string",
                "enum": ["remove_edge", "change_predicate", "change_object", "none"],
            },
            "new_predicate": {
                "type": "string",
                "enum": predicate_options + ["N/A"],
                "description": "Only meaningful when fix_action='change_predicate'; otherwise 'N/A'.",
            },
            "new_object": {
                "type": "string",
                "enum": object_options + ["N/A"],
                "description": "Only meaningful when fix_action='change_object'; otherwise 'N/A'.",
            },
            "fix_rationale": {
                "type": "string",
                "maxLength": 250,
                "description": "Why this exact fix, one sentence. Empty string when fix_action='none'.",
            },
        },
        "required": [
            "evidence",
            "phase2_reasoning",
            "verdict",
            "issue_summary",
            "fix_action",
            "new_predicate",
            "new_object",
            "fix_rationale",
        ],
    }


# --------------------------------------------------------------------------
# Post-hoc validation helpers (call from lib/llm_judge.py after parsing)
# --------------------------------------------------------------------------

def validate_phase2_output(out: dict) -> list[str]:
    """Return a list of violation strings; empty list means the output is
    internally consistent. Any violation → reject and retry the call.
    (Enum/type conformance is already guaranteed by schema decoding; this
    checks cross-field consistency the schema can't express.)"""
    errors: list[str] = []
    verdict = out.get("verdict")
    action = out.get("fix_action")

    if verdict == "issue" and action == "none":
        errors.append("verdict=issue requires a concrete fix_action")
    if verdict in ("resolved", "unverifiable") and action != "none":
        errors.append(f"verdict={verdict} must have fix_action=none, got {action}")
    if action == "change_predicate" and out.get("new_predicate") in (None, "", "N/A"):
        errors.append("change_predicate requires new_predicate")
    if action == "change_object" and out.get("new_object") in (None, "", "N/A"):
        errors.append("change_object requires new_object")
    if verdict == "issue":
        for field in ("issue_summary", "evidence", "phase2_reasoning", "fix_rationale"):
            if not out.get(field, "").strip():
                errors.append(f"verdict=issue requires non-empty {field}")

    text_blob = " ".join(
        str(out.get(f, "")) for f in ("evidence", "phase2_reasoning", "issue_summary", "fix_rationale")
    ).lower()
    for marker in SCRATCHPAD_MARKERS:
        if marker in text_blob:
            errors.append(f"scratchpad leakage detected: {marker!r}")
            break

    return errors


def downgrade_sentinel_fix(out: dict) -> dict:
    """If the model selected OBJECT_NOT_IN_CONTEXT, the fix is not
    actionable from the shown context — convert to unverifiable rather
    than surfacing a phantom fix. Call after validate_phase2_output."""
    if out.get("fix_action") == "change_object" and out.get("new_object") == OBJECT_NOT_IN_CONTEXT:
        out = dict(out)
        out["verdict"] = "unverifiable"
        out["fix_action"] = "none"
        out["new_object"] = "N/A"
        out["phase2_reasoning"] = (
            out.get("phase2_reasoning", "").strip()
            + " [Correct object not present in shown context; downgraded to unverifiable.]"
        ).strip()
    return out
