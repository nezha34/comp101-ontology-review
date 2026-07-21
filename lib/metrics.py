"""
metrics.py — OntoQA-style schema + instance metrics.

Prefers the shared ontometrics package when present as a sibling checkout:
  Documents/ontometrics/ontology_metrics.py

Otherwise uses a built-in fallback so validate.py still runs.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from rdflib import OWL, RDF, RDFS, Graph


def _try_import_ontometrics():
    candidates = [
        Path(__file__).resolve().parents[2] / "ontometrics",
        Path(__file__).resolve().parents[1] / "ontometrics",
    ]
    for d in candidates:
        if (d / "ontology_metrics.py").is_file():
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
            from ontology_metrics import compute_metrics as compute_ontoqa_metrics  # type: ignore

            return compute_ontoqa_metrics
    return None


_external = _try_import_ontometrics()


def _fallback_compute_metrics(g: Graph, path=None) -> dict:
    """Minimal Tartir-style OntoQA metrics from an rdflib Graph."""
    classes = set(g.subjects(RDF.type, OWL.Class)) | set(g.subjects(RDF.type, RDFS.Class))
    for s, o in g.subject_objects(RDFS.subClassOf):
        if not str(s).startswith("http://www.w3.org/"):
            classes.add(s)
        if not str(o).startswith("http://www.w3.org/"):
            classes.add(o)

    obj_props = set(g.subjects(RDF.type, OWL.ObjectProperty))
    data_props = set(g.subjects(RDF.type, OWL.DatatypeProperty))
    subclass_edges = list(g.subject_objects(RDFS.subClassOf))

    n_cls = max(len(classes), 1)
    n_obj = len(obj_props)
    n_sub = len(subclass_edges)
    rr = n_obj / (n_obj + n_sub) if (n_obj + n_sub) else 0.0
    ar = len(data_props) / n_cls
    ir = n_sub / n_cls

    children = defaultdict(set)
    parents = defaultdict(set)
    for child, parent in subclass_edges:
        children[parent].add(child)
        parents[child].add(parent)
    roots = [c for c in classes if not parents[c]]
    max_depth = 0
    depth_sum = 0
    depth_n = 0
    for root in roots or list(classes):
        stack = [(root, 0)]
        seen = set()
        while stack:
            node, d = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            max_depth = max(max_depth, d)
            depth_sum += d
            depth_n += 1
            for ch in children[node]:
                stack.append((ch, d + 1))

    individuals = set(g.subjects(RDF.type, OWL.NamedIndividual))
    typed = defaultdict(set)
    for s, t in g.subject_objects(RDF.type):
        if t in classes:
            individuals.add(s)
            typed[t].add(s)

    classes_with_instances = sum(1 for c in classes if typed[c])
    ind_count = len(individuals)
    cr = classes_with_instances / n_cls
    avg_pop = ind_count / n_cls

    return {
        "schema": {
            "class_count": len(classes),
            "object_property_count": n_obj,
            "datatype_property_count": len(data_props),
            "subclass_edges": n_sub,
            "relationship_richness": round(rr, 4),
            "attribute_richness": round(ar, 4),
            "inheritance_richness": round(ir, 4),
            "max_inheritance_depth": max_depth,
            "avg_inheritance_depth": round(depth_sum / depth_n, 4) if depth_n else 0.0,
        },
        "instance": {
            "individual_count": ind_count,
            "class_richness": round(cr, 4),
            "average_population": round(avg_pop, 4),
            "classes_with_instances": classes_with_instances,
            "source": "builtin_fallback" if path is None else f"builtin_fallback:{path}",
        },
    }


def compute_ontoqa_metrics(g: Graph, path=None) -> dict:
    if _external is not None:
        return _external(g, path)
    return _fallback_compute_metrics(g, path)
