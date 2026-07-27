"""
Adversarial eval suite for the semantic-judge prompt stack (§6 of the
external prompt review).

Deterministic cases run with no LLM. Cases that need a live model are
recorded in LLM_EXPECTATIONS for manual / future live runs.

Run:
  python -m pytest tests/test_adversarial_semantic.py -q
  # or without pytest:
  python tests/test_adversarial_semantic.py
"""

from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

from lib.graph_utils import load_graph
from lib.llm_judge import PROMPT_VERSION, collect_claims, run_phase1
from lib.ontology_drift import (
    apply_drift_to_config,
    compute_tbox_drift,
    format_drift_vocabulary_block,
)
from lib.path_order import (
    PATH_ORDER_RELATIONS,
    attach_path_to_config,
    format_path_excerpt_for_pair,
)
from lib.prompts import (
    EVIDENCE_EXTERNAL_RELATIONS,
    PHASE1_SYSTEM,
    build_phase1_user_prompt,
    format_skill_graph_wiring,
    relation_semantics_coverage,
)

EXAMPLES = ROOT / "examples"
CONFIG_PATH = ROOT / "configs" / "comp101_w5_w6.json"
# The original combined W5-W6 module was split into one file per lecture
# folder (see examples/README.md); the test fixture needs both merged back
# into a single graph to see cross-week triples (e.g. W6 dependsOn W5).
OWL_PATHS = [
    EXAMPLES / "Lecture 5" / "comp101_L5.owl",
    EXAMPLES / "Lecture 6" / "comp101_L6.owl",
]
PATH_PATH = EXAMPLES / "comp101_path_w5_w6.json"

# Live-LLM expectations from the review (§6). Not executed here — documented
# so a future --live run can assert them.
LLM_EXPECTATIONS = [
    {
        "id": 1,
        "claim": "Skill --usesConcept--> Concept",
        "expect": "phase1 incorrect; phase2 issue + change_predicate → requiresConcept",
    },
    {
        "id": 2,
        "claim": "Method/BuiltIn --requiresConcept--> Concept",
        "expect": "phase1 incorrect; phase2 change_predicate → usesConcept",
    },
    {
        "id": 3,
        "claim": "dependsOn(W5_lists, W6_dicts_sets) reversed",
        "expect": "incorrect (backwards prerequisite)",
    },
    {
        "id": 4,
        "claim": "recommendedBefore between consecutive PATH beats",
        "expect": "questionable/incorrect — redundant with PATH",
    },
    {
        "id": 5,
        "claim": "recommendedBefore genuine sibling dilemma (no PATH/dependsOn order)",
        "expect": "correct / resolved",
    },
    {
        "id": 6,
        "claim": "confusedWith(list, for_loop) unrelated",
        "expect": "incorrect",
    },
    {
        "id": 9,
        "claim": "producesType wrong type (str.split → dict)",
        "expect": "incorrect on declared shape / meaning",
    },
    {
        "id": 10,
        "claim": "throwsError list.append → MemoryError (unrealistic COMP101)",
        "expect": "questionable or incorrect per 'realistic conditions'",
    },
]


def _load_cfg() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _load_owl() -> Graph:
    merged = Graph()
    for path in OWL_PATHS:
        g, err = load_graph(path)
        assert err is None, err
        merged += g
    return merged


class AdversarialDeterministic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = _load_owl()
        cls.cfg = _load_cfg()

    # --- case 7 / 8: evidence-external gating ---
    def test_07_taughtIn_gated(self):
        by, gated = collect_claims(self.g, self.cfg)
        self.assertNotIn("taughtIn", by)
        self.assertIn("taughtIn", EVIDENCE_EXTERNAL_RELATIONS)
        self.assertGreater(gated, 0)

    def test_08_revisitedIn_gated(self):
        by, _ = collect_claims(self.g, self.cfg)
        self.assertNotIn("revisitedIn", by)
        self.assertIn("revisitedIn", EVIDENCE_EXTERNAL_RELATIONS)

    # --- case 4 PATH consecutive / case 5 dilemma scaffolding ---
    def test_04_path_marks_consecutive_beats(self):
        cfg, meta = attach_path_to_config(self.cfg)
        self.assertTrue(meta and meta["ok"])
        excerpt = format_path_excerpt_for_pair(
            cfg["_path_steps"], "list_append", "list_insert"
        )
        self.assertIn("consecutive PATH beats", excerpt)
        self.assertIn("list_append", excerpt)
        self.assertIn("list_insert", excerpt)

    def test_05_path_off_spine_stated(self):
        cfg, _ = attach_path_to_config(self.cfg)
        excerpt = format_path_excerpt_for_pair(
            cfg["_path_steps"], "not_a_real_skill", "also_fake"
        )
        self.assertIn("Neither", excerpt)
        self.assertIn("appears on PATH", excerpt)

    def test_path_fail_loud_without_path_file(self):
        cfg = deepcopy(self.cfg)
        cfg.pop("path_file", None)
        by, _ = collect_claims(self.g, cfg)
        self.assertTrue(set(by) & PATH_ORDER_RELATIONS)

        class Prov:
            label = "noop"

            def chat(self, *a, **k):
                raise AssertionError("LLM must not be called on PATH fail-loud")

        result = run_phase1(self.g, cfg, Prov())
        self.assertFalse(result["ok"])
        self.assertIn("PATH", result["error"])

    # --- skill_graph wiring in prompts ---
    def test_skill_graph_wiring_in_phase1_prompt(self):
        wiring = format_skill_graph_wiring(self.cfg)
        self.assertIsNotNone(wiring)
        self.assertIn("Skill class = Skill", wiring)
        self.assertIn("skill→skill edge = dependsOn", wiring)
        self.assertIn("skill→concept edge = requiresConcept", wiring)
        self.assertIn("never usesConcept", wiring)

        prompt = build_phase1_user_prompt(
            "requiresConcept",
            self.cfg["relation_semantics"]["requiresConcept"],
            [{"index": 0, "subject_label": "S", "object_label": "C",
              "subject_types": ["Skill"], "object_types": ["Concept"]}],
            skill_graph_wiring=wiring,
        )
        self.assertIn("Module skill-graph wiring", prompt)
        self.assertIn("requiresConcept", prompt)

    def test_skill_graph_oop_uses_usesConcept(self):
        oop = {
            "skill_graph": {
                "class": "Skill",
                "depends_on": "dependsOn",
                "uses_concept": "usesConcept",
            }
        }
        wiring = format_skill_graph_wiring(oop)
        self.assertIn("usesConcept", wiring)
        self.assertIn("OOP-style", wiring)
        self.assertNotIn("never usesConcept", wiring)

    # --- cases 1–2 / 11: prompt instructions present ---
    def test_01_02_prompt_skill_vs_carrier_rules(self):
        self.assertIn("requiresConcept vs usesConcept", PHASE1_SYSTEM)
        self.assertIn("Skill", PHASE1_SYSTEM)
        wiring = format_skill_graph_wiring(self.cfg)
        p = build_phase1_user_prompt(
            "usesConcept",
            self.cfg["relation_semantics"]["usesConcept"],
            [{"index": 0, "subject_label": "some_skill", "object_label": "list",
              "subject_types": ["Skill"], "object_types": ["DataType"]}],
            skill_graph_wiring=wiring,
        )
        self.assertIn("never usesConcept", p)

    def test_11_sibling_note_in_phase1_system(self):
        self.assertIn("same subject or object", PHASE1_SYSTEM)
        self.assertIn("not evidence of redundancy", PHASE1_SYSTEM)

    # --- case 3 scaffolding: canonical week order in OWL ---
    def test_03_canonical_week_dependsOn_direction(self):
        NS = Namespace("http://gnosis.local/ontologies/comp101#")
        triples = list(self.g.triples((NS.W6_dicts_sets, NS.dependsOn, NS.W5_lists)))
        self.assertTrue(triples, "expected W6 dependsOn W5 in ontology")
        reverse = list(self.g.triples((NS.W5_lists, NS.dependsOn, NS.W6_dicts_sets)))
        self.assertFalse(reverse, "reversed week dependsOn should not be asserted")

    # --- case 6: confusedWith gloss present ---
    def test_06_confusedWith_gloss_is_strict(self):
        gloss = self.cfg["relation_semantics"]["confusedWith"]
        self.assertIn("confuse", gloss.lower())
        self.assertIn("Not for logical opposites", gloss)

    # --- case 12: drift property without comment stays missing ---
    def test_12_drift_no_comment_still_missing(self):
        NS = Namespace("http://example.org/onto#")
        baseline = Graph()
        baseline.add((NS.KnownProp, RDF.type, OWL.ObjectProperty))
        baseline.add((NS.KnownProp, RDFS.comment, Literal("known")))

        candidate = Graph()
        candidate.add((NS.KnownProp, RDF.type, OWL.ObjectProperty))
        candidate.add((NS.KnownProp, RDFS.comment, Literal("known")))
        candidate.add((NS.MysteryProp, RDF.type, OWL.ObjectProperty))
        # no rdfs:comment on MysteryProp

        drift = compute_tbox_drift(candidate, baseline)
        ids = [p["id"] for p in drift["properties_added"]]
        self.assertIn("MysteryProp", ids)
        mystery = next(p for p in drift["properties_added"] if p["id"] == "MysteryProp")
        self.assertIsNone(mystery.get("comment"))

        cfg = apply_drift_to_config(candidate, {"strict_relation_semantics": True}, drift)
        self.assertIn("MysteryProp", cfg.get("_drift_relations_still_missing", []))
        missing = relation_semantics_coverage(["MysteryProp"], cfg)
        self.assertIn("MysteryProp", missing)

        block = format_drift_vocabulary_block(drift)
        self.assertIsNotNone(block)
        self.assertIn("MysteryProp", block)
        self.assertIn("Do not invent meanings", block)

    def test_prompt_version_bumped(self):
        self.assertEqual(PROMPT_VERSION, "12")

    def test_llm_expectations_catalog_complete(self):
        ids = {c["id"] for c in LLM_EXPECTATIONS}
        # Deterministic cases 7,8,11,12 + PATH 4,5 are tested above;
        # remaining live-LLM ids must be catalogued.
        self.assertEqual(ids, {1, 2, 3, 4, 5, 6, 9, 10})


if __name__ == "__main__":
    unittest.main(verbosity=2)
