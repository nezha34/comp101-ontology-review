"""
skill_graph.py — optional, config-driven DAG/coverage checks for ontologies
that model a prerequisite graph (e.g. a "Skill --dependsOn--> Skill" chain).

This is deliberately NOT run unless a config supplies a `skill_graph` block,
since "does this ontology have a dependency DAG" is a domain assumption, not
a generic OWL property.
"""

from __future__ import annotations

from collections import defaultdict, deque

from rdflib import RDF, RDFS, Graph, URIRef

from .graph_utils import label


def check_skill_graph(g: Graph, ns_uri: str, config: dict) -> dict:
    """config keys: class, depends_on, and optionally requires_concept
    or uses_concept (local predicate name linking Skill → Concept)."""
    cls_name = config.get("class")
    dep_name = config.get("depends_on")
    # COMP101 uses requiresConcept on Skills; older OOP configs used usesConcept.
    uses_name = config.get("requires_concept") or config.get("uses_concept")

    if not cls_name or not dep_name:
        return {"issues": ["skill_graph config missing required 'class' or 'depends_on'"], "skills": []}

    Skill = URIRef(ns_uri + cls_name)
    depOn = URIRef(ns_uri + dep_name)
    usesCon = URIRef(ns_uri + uses_name) if uses_name else None

    issues: list[str] = []
    skills = list(g.subjects(RDF.type, Skill))
    skill_set = set(skills)

    adj = defaultdict(set)
    for s in skills:
        for dep in g.objects(s, depOn):
            adj[s].add(dep)

    in_degree = defaultdict(int)
    for s in adj:
        for t in adj[s]:
            if t in skill_set:
                in_degree[t] += 1

    queue = deque([n for n in skill_set if in_degree[n] == 0])
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for neighbor in adj[node]:
            if neighbor in skill_set:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

    if visited < len(skill_set):
        issues.append(f"CYCLE DETECTED in {cls_name} {dep_name} graph — not a DAG")

    if usesCon:
        for s in skills:
            if not list(g.objects(s, usesCon)):
                issues.append(f"{cls_name} '{label(g,s)}' has no {uses_name} edges")

    for s in skills:
        for dep in g.objects(s, depOn):
            if dep not in skill_set:
                issues.append(f"{cls_name} '{label(g,s)}' {dep_name} '{label(g,dep)}' which is not a {cls_name}")

    skill_info = []
    for s in skills:
        deps = [label(g, d) for d in g.objects(s, depOn) if d in skill_set]
        uses = [label(g, u) for u in g.objects(s, usesCon)] if usesCon else []
        skill_info.append({
            "name": label(g, s),
            "depends_on": deps,
            "uses_concept": uses,  # values from requires_concept or uses_concept predicate
            "concept_link_predicate": uses_name,
            "is_root": len(deps) == 0,
        })

    return {"issues": issues, "skills": skill_info}
