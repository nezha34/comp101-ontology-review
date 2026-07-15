"""
structural.py — generic structural-integrity checks that apply to any
OWL/RDF ontology, independent of domain vocabulary.

These are cheap, offline, rdflib-only checks meant to catch the kind of
mistakes a reasoner or OOPS! either won't catch or would take a network
round-trip to catch:
  - file fails to parse at all (fatal)
  - no owl:Ontology declaration
  - URIs referenced (as triple objects) inside the ontology's own
    namespace but never declared as a class/property/individual
  - reflexive edges on properties that were not declared owl:ReflexiveProperty
  - punning: same URI declared as both a Class and a NamedIndividual
  - classes/individuals missing rdfs:label
  - individuals with no outgoing/incoming triples beyond rdf:type ("orphans")
  - duplicate values of a designated identifier property (config-driven)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef
from rdflib.term import BNode

from .graph_utils import label, named_classes, named_individuals, object_properties


def check_structural(g: Graph, ns_uri: str, id_property: Optional[str] = None) -> dict:
    issues: list[str] = []
    warnings: list[str] = []

    has_ontology_decl = bool(list(g.subjects(RDF.type, OWL.Ontology)))
    if not has_ontology_decl:
        warnings.append("No owl:Ontology declaration found in the file.")

    # Dangling references: URIs in-namespace used as an object but never typed.
    if ns_uri:
        declared = set()
        for s in g.subjects(RDF.type, None):
            if isinstance(s, URIRef) and str(s).startswith(ns_uri):
                declared.add(s)

        seen = set()
        for s, p, o in g:
            if isinstance(o, URIRef) and str(o).startswith(ns_uri) and o not in declared and o not in seen:
                seen.add(o)
                issues.append(f"Dangling reference: {label(g,o)} <{o}> (target of {label(g,p)})")

    # Reflexive edges on properties not declared owl:ReflexiveProperty
    reflexive_ok = set(g.subjects(RDF.type, OWL.ReflexiveProperty))
    for p in object_properties(g):
        if p in reflexive_ok:
            continue
        for s, _, o in g.triples((None, p, None)):
            if s == o:
                issues.append(f"Self-loop: {label(g,s)} --{label(g,p)}--> itself (not declared ReflexiveProperty)")

    # Punning: same URI is both a Class and a NamedIndividual
    classes = set(named_classes(g))
    individuals = set(named_individuals(g))
    for node in classes & individuals:
        warnings.append(f"Punning: {label(g,node)} is declared as both owl:Class and owl:NamedIndividual")

    # Missing rdfs:label
    unlabeled_classes = [c for c in classes if not g.value(c, RDFS.label)]
    unlabeled_inds = [i for i in individuals if not g.value(i, RDFS.label)]
    if unlabeled_classes:
        warnings.append(f"{len(unlabeled_classes)} class(es) missing rdfs:label")
    if unlabeled_inds:
        warnings.append(f"{len(unlabeled_inds)} individual(s) missing rdfs:label")

    # Orphan individuals: no outgoing/incoming triples beyond rdf:type
    for ind in individuals:
        out_edges = sum(1 for _ in g.predicate_objects(ind) if _[0] != RDF.type)
        in_edges = sum(1 for _ in g.subject_predicates(ind))
        if out_edges == 0 and in_edges == 0:
            warnings.append(f"Orphan individual: {label(g,ind)} has no relationships beyond rdf:type")

    # Duplicate identifiers (only if the ontology declares an id property)
    if id_property:
        id_prop = URIRef(ns_uri + id_property) if ns_uri else None
        ids_seen = defaultdict(list)
        if id_prop:
            for ind in individuals:
                val = g.value(ind, id_prop)
                if val:
                    ids_seen[str(val)].append(str(ind))
            for val, uris in ids_seen.items():
                if len(uris) > 1:
                    issues.append(f"Duplicate {id_property} '{val}': {uris}")

    return {
        "has_ontology_declaration": has_ontology_decl,
        "issues": issues,
        "warnings": warnings,
    }
