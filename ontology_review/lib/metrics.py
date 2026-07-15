"""
OntoQA-style schema + instance metrics (stdlib + rdflib only).

Falls back here when the original ontometrics package is absent.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from rdflib import OWL, RDF, RDFS, Graph
from rdflib.term import URIRef

from .graph_utils import named_classes, named_individuals, object_properties


def compute_ontoqa_metrics(g: Graph, path: Path | None = None) -> dict:
    classes = named_classes(g)
    individuals = named_individuals(g)
    props = object_properties(g)

    subclass_edges = list(g.triples((None, RDFS.subClassOf, None)))
    # Only named→named subclass for richness (skip restrictions / bnodes)
    named_subclass = [
        (s, o)
        for s, _, o in subclass_edges
        if isinstance(s, URIRef) and isinstance(o, URIRef) and o != OWL.Thing
    ]

    n_cls = max(len(classes), 1)
    n_op = len(props)
    n_sub = len(named_subclass)
    relationship_richness = round(n_op / (n_op + n_sub), 4) if (n_op + n_sub) else 0.0

    dtype_props = set(g.subjects(RDF.type, OWL.DatatypeProperty)) | set(
        g.subjects(RDF.type, OWL.AnnotationProperty)
    )
    attribute_richness = round(len(dtype_props) / n_cls, 4)

    children: dict = defaultdict(list)
    for s, o in named_subclass:
        children[o].append(s)
    inheritance_richness = round(
        sum(len(v) for v in children.values()) / max(len(children), 1), 4
    ) if children else 0.0

    def depth(node, seen=None) -> int:
        seen = seen or set()
        if node in seen:
            return 0
        seen.add(node)
        kids = children.get(node, [])
        if not kids:
            return 0
        return 1 + max(depth(k, seen) for k in kids)

    roots = [c for c in classes if not any(o == c for _, o in named_subclass)]
    depths = [depth(r) for r in (roots or list(classes))]
    max_depth = max(depths) if depths else 0
    avg_depth = round(sum(depths) / max(len(depths), 1), 4) if depths else 0.0

    pop: dict[str, int] = defaultdict(int)
    for ind in individuals:
        for t in g.objects(ind, RDF.type):
            if t in classes:
                pop[str(t)] += 1
    classes_with_inst = sum(1 for c in classes if pop[str(c)] > 0)
    class_richness = round(classes_with_inst / n_cls, 4)
    avg_population = round(len(individuals) / n_cls, 4)

    return {
        "source": str(path) if path else None,
        "implementation": "builtin_ontoqa",
        "schema": {
            "class_count": len(classes),
            "object_property_count": n_op,
            "datatype_property_count": len(dtype_props),
            "subclass_edges": n_sub,
            "subclass_edge_count": n_sub,
            "relationship_richness": relationship_richness,
            "attribute_richness": attribute_richness,
            "inheritance_richness": inheritance_richness,
            "max_inheritance_depth": max_depth,
            "avg_inheritance_depth": avg_depth,
        },
        "instance": {
            "individual_count": len(individuals),
            "class_richness": class_richness,
            "average_population": avg_population,
            "classes_with_instances": classes_with_inst,
            "per_class_population": dict(sorted(pop.items(), key=lambda x: -x[1])[:40]),
        },
    }
