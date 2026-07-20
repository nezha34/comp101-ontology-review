"""
prompts.py — the two judge prompts, kept deliberately separate because they
do different jobs and get confused with each other if merged:

  PHASE 1 (screen)   — cheap, per-edge, high recall. "Does this single
                        triple look true in isolation?" Over-flagging is
                        fine; missing something is not.

  PHASE 2 (re-verify) — expensive, only runs on phase-1 flags, sees the
                        local neighborhood of both endpoints. Job is to
                        either dismiss the flag ("resolved by context") or
                        write an evidence-cited case for a human, ending
                        in one concrete proposed fix.

Both call an Ollama model with a JSON-schema `format` so the response is
guaranteed parseable — see lib/llm_judge.py for how these are used.
"""

from __future__ import annotations

# Fallback natural-language meaning for common relation names, used only
# when a config doesn't supply its own `relation_semantics` block.
DEFAULT_RELATION_SEMANTICS = {
    "dependsOn": (
        "The subject cannot be reasonably understood or performed by a "
        "student until the object has already been learned. This is a "
        "prerequisite edge in a teaching sequence."
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
        "conceptually associated with it."
    ),
    "taughtIn": (
        "The subject concept/skill is actually introduced or covered in "
        "the named lecture, not merely tangentially related to it."
    ),
}


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
- "uncertain"   — you genuinely cannot judge this without more context \
(e.g. it depends on curriculum details you don't have).

Bias toward "questionable" or "uncertain" over false confidence. A wrong \
"correct" verdict is worse than an over-cautious "questionable" one — \
this is a first-pass screen, not a final verdict; everything you flag \
gets a second, more careful look before any human sees it.

Respond ONLY through the structured output fields you're given — do not \
add commentary outside them."""


def build_phase1_user_prompt(relation_name: str, relation_meaning: str, claims: list[dict]) -> str:
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
        "a verdict, and one concise sentence of reasoning specific to that "
        "claim (not a generic justification)."
    )
    return "\n".join(lines)


PHASE1_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {
                        "type": "string",
                        "enum": ["correct", "questionable", "incorrect", "uncertain"],
                    },
                    "reasoning": {"type": "string"},
                },
                "required": ["index", "verdict", "reasoning"],
            },
        }
    },
    "required": ["verdicts"],
}


PHASE2_SYSTEM = """You are the senior reviewer in a two-pass ontology audit. \
A first-pass reviewer already flagged one triple as questionable/incorrect/ \
uncertain, in isolation. You are now shown that same claim together with \
everything else the two endpoint nodes connect to in the ontology — the \
full local context the first reviewer didn't have.

Your job, in order:
1. Decide whether the surrounding context RESOLVES the original concern \
(e.g. the edge looks odd alone but is fully justified once you see the \
class hierarchy / sibling edges / where it fits the teaching sequence), \
or whether the concern still stands even with full context.
2. If resolved: explain concretely what in the context resolves it. Do \
not invent context that isn't shown to you.
3. If NOT resolved: write a precise, evidence-based case a human can act \
on without re-deriving your reasoning. Quote the exact triple. Point to \
the specific piece of context (or its absence) that makes it wrong. Then \
propose exactly ONE concrete fix — the smallest single change (add, \
remove, or modify one triple) that would resolve it — with a one-sentence \
rationale for why that specific fix is right rather than some other one.

Be argumentative and specific, never vague ("this seems off") — a human \
is going to read only your fields and decide whether to accept or reject \
your proposed fix, without re-examining the ontology themselves. If you \
are not confident enough to defend the verdict in one paragraph, that's a \
sign it should be resolved=true, not a reason to soften the wording.

Before you write resolved=false, re-read your own evidence field and ask: \
does it actually argue for removing/changing this edge, or does it argue \
the edge is correct? These are not the same thing, and conflating them is \
the single most common mistake here — e.g. citing "the object's definition \
explicitly relies on the subject" as evidence, then proposing to REMOVE the \
edge that connects them, when that same evidence is a reason to KEEP it. \
If your own cited evidence supports the edge rather than undermining it, \
that means the concern IS resolved — set resolved=true and say so, don't \
force a fix out of a flag that no longer holds up.

The context below will often show several other edges of the SAME relation \
from the SAME subject (e.g. a skill with many usesConcept edges). That is \
the normal, expected shape of this graph, not evidence of clutter — do not \
propose removing an edge just because siblings of the same relation exist \
alongside it. Judge each edge only on its own merits: is the object \
individually true and relevant per the subject's own definition text? \
"This edge and its sibling are both about related things, so one is \
redundant" is not a valid argument on its own — most of the edges you will \
see flagged this way turn out to each be independently named in the \
subject's definition, which makes them independently load-bearing, not \
redundant with each other.

Never justify a fix by referring to a broader/collapsed concept (a "group", \
a "category", "the streams as a whole", etc.) unless that concept is an \
actual node shown to you in the context below. If no such node exists, you \
are inventing structure that isn't in the ontology — that is not grounds to \
remove a real edge, and proposing add_triple/remove_triple based on a node \
that doesn't exist makes the fix un-actionable. A human reading your fix \
must be able to act on it using only what's actually in the ontology.

Keep every text field SHORT: one or two plain sentences, under 40 words \
each. Do not repeat yourself, do not pad, do not use filler or repeated \
punctuation/underscores. Stop as soon as you've made the point once.

If resolved=false, you MUST fill in ALL of these — none may be left \
empty: issue_summary, evidence, phase2_reasoning, proposed_fix_action, \
proposed_fix_triple, proposed_fix_rationale. A human cannot act on a \
finding that is missing its evidence or its exact proposed fix, so an \
empty field here makes the whole finding useless — treat leaving one \
blank as a failure to complete the task, not an acceptable shortcut."""


def build_phase2_user_prompt(
    relation_name: str,
    relation_meaning: str,
    subject_label: str,
    object_label: str,
    phase1_verdict: str,
    phase1_reasoning: str,
    subject_context: list[str],
    object_context: list[str],
) -> str:
    lines = [
        f'Flagged claim: "{subject_label}" --{relation_name}--> "{object_label}"',
        f"What {relation_name} is supposed to mean here: {relation_meaning}",
        f"First-pass verdict: {phase1_verdict} — \"{phase1_reasoning}\"",
        "",
        f'Everything else "{subject_label}" connects to in the ontology:',
    ]
    lines.extend(f"  - {t}" for t in subject_context) if subject_context else lines.append("  (nothing else)")
    lines.append("")
    lines.append(f'Everything else "{object_label}" connects to in the ontology:')
    lines.extend(f"  - {t}" for t in object_context) if object_context else lines.append("  (nothing else)")
    lines.append("")
    lines.append(
        "Decide: is the original concern resolved by this context, or does "
        "it still hold? Fill in the structured fields accordingly."
    )
    return "\n".join(lines)


PHASE2_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved": {"type": "boolean"},
        "resolution_explanation": {
            "type": "string",
            "description": "If resolved=true, what in the context resolves it. If resolved=false, brief note on why context doesn't save it.",
        },
        "issue_summary": {"type": "string", "description": "One sentence. Only fill if resolved=false."},
        "evidence": {"type": "string", "description": "The exact triple and the specific contradicting/missing context. Only fill if resolved=false."},
        "phase2_reasoning": {"type": "string", "description": "The argumentative case, 1-2 sentences. Only fill if resolved=false."},
        "proposed_fix_action": {
            "type": "string",
            "enum": ["add_triple", "remove_triple", "modify_triple", "none"],
        },
        "proposed_fix_triple": {"type": "string", "description": "The specific triple to add/remove/modify. Only fill if action is not none."},
        "proposed_fix_rationale": {"type": "string", "description": "Why this exact fix, one sentence. Only fill if action is not none."},
    },
    "required": ["resolved", "resolution_explanation"],
}
