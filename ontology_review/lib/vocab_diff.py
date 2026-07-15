"""Compare an OWL module's vocabulary against the shared COMP101 T-Box baseline."""

from __future__ import annotations

import json
from pathlib import Path

from rdflib import OWL, RDF, Graph
from rdflib.term import URIRef

from .graph_utils import named_classes, object_properties


def _local(iri: str) -> str:
    if "#" in iri:
        return iri.rsplit("#", 1)[-1]
    return iri.rsplit("/", 1)[-1]


def load_baseline(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "id": data.get("id") or path.stem,
        "version": data.get("version") or "unknown",
        "classes": {c["id"] for c in data.get("classes") or [] if c.get("id")},
        "object_properties": {
            p["id"] for p in data.get("object_properties") or [] if p.get("id")
        },
    }


def extract_owl_vocab(g: Graph) -> dict:
    classes = {_local(str(c)) for c in named_classes(g)}
    props = {_local(str(p)) for p in object_properties(g)}
    # asserted edge types (= predicates that are object properties or used as such)
    used_edges: set[str] = set()
    prop_iris = set(object_properties(g))
    for s, p, o in g:
        if not isinstance(p, URIRef):
            continue
        if p in prop_iris or (s, RDF.type, OWL.ObjectProperty) in g:
            used_edges.add(_local(str(p)))
        # also count known pedagogical predicates even if mistyped
        local = _local(str(p))
        if local in {
            "taughtIn",
            "dependsOn",
            "usesConcept",
            "requiresConcept",
            "throwsError",
            "methodOf",
            "producesType",
            "contrastsWith",
            "partOf",
            "managedBy",
            "enables",
            "implementsConcept",
            "implementedBy",
        }:
            used_edges.add(local)
    return {"classes": classes, "object_properties": props, "edge_types": used_edges}


def vocab_diff(g: Graph, baseline_path: Path, *, other_id: str = "upload") -> dict:
    base = load_baseline(baseline_path)
    other = extract_owl_vocab(g)

    classes_only_other = sorted(other["classes"] - base["classes"])
    classes_only_base = sorted(base["classes"] - other["classes"])
    props_only_other = sorted(other["object_properties"] - base["object_properties"])
    props_only_base = sorted(base["object_properties"] - other["object_properties"])
    edges_only_other = sorted(other["edge_types"] - base["object_properties"])
    edges_only_base = sorted(base["object_properties"] - other["edge_types"])
    undeclared = sorted(other["edge_types"] - other["object_properties"])

    return {
        "baseline_id": base["id"],
        "baseline_version": base["version"],
        "other_id": other_id,
        "classes_only_in_other": classes_only_other,
        "classes_only_in_baseline": classes_only_base,
        "object_properties_only_in_other": props_only_other,
        "object_properties_only_in_baseline": props_only_base,
        "edge_types_only_in_other": edges_only_other,
        "edge_types_missing_vs_baseline_props": edges_only_base,
        "edge_types_used_but_undeclared": undeclared,
        "summary": {
            "classes_added": len(classes_only_other),
            "classes_missing": len(classes_only_base),
            "object_properties_added": len(props_only_other),
            "object_properties_missing": len(props_only_base),
            "edge_types_added": len(edges_only_other),
        },
    }


def compare_owl_vocabs(g_a: Graph, g_b: Graph, *, id_a: str, id_b: str) -> dict:
    """Symmetric vocabulary diff between two OWL modules (not vs shared T-Box)."""
    a = extract_owl_vocab(g_a)
    b = extract_owl_vocab(g_b)

    def pack(left: set[str], right: set[str]) -> dict:
        only_a = sorted(left - right)
        only_b = sorted(right - left)
        both = sorted(left & right)
        return {
            "only_in_a": only_a,
            "only_in_b": only_b,
            "shared": both,
            "summary": {
                "only_in_a": len(only_a),
                "only_in_b": len(only_b),
                "shared": len(both),
            },
        }

    classes = pack(a["classes"], b["classes"])
    props = pack(a["object_properties"], b["object_properties"])
    edges = pack(a["edge_types"], b["edge_types"])
    return {
        "id_a": id_a,
        "id_b": id_b,
        "classes": classes,
        "object_properties": props,
        "edge_types": edges,
        "summary": {
            "classes_only_a": classes["summary"]["only_in_a"],
            "classes_only_b": classes["summary"]["only_in_b"],
            "classes_shared": classes["summary"]["shared"],
            "props_only_a": props["summary"]["only_in_a"],
            "props_only_b": props["summary"]["only_in_b"],
            "props_shared": props["summary"]["shared"],
            "edges_only_a": edges["summary"]["only_in_a"],
            "edges_only_b": edges["summary"]["only_in_b"],
            "edges_shared": edges["summary"]["shared"],
        },
    }
