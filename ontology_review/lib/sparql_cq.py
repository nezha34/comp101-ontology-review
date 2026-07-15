"""
sparql_cq.py — run a list of SPARQL "competency questions" (CQs) supplied by
a per-ontology config file against the graph. Each CQ declares the minimum
number of results it expects, turning "does this ontology answer question X"
into a pass/fail check.
"""

from __future__ import annotations

from rdflib import Graph


def run_sparql_cqs(g: Graph, ns_uri: str, cqs: list[dict]) -> list[dict]:
    results = []
    for cq in cqs:
        q = cq["sparql"].format(ns=ns_uri)
        try:
            rows = list(g.query(q))
            n = len(rows)
            passed = n >= cq.get("expect_min", 1)
            results.append({
                "id": cq["id"],
                "question": cq["question"],
                "results": n,
                "expected_min": cq.get("expect_min", 1),
                "passed": passed,
                "sample": [str(r[0]).rsplit("#", 1)[-1] for r in rows[:5]],
            })
        except Exception as e:
            results.append({
                "id": cq["id"], "question": cq["question"],
                "error": str(e), "passed": False,
            })
    return results
