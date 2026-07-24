#!/usr/bin/env python3
"""Fix OOPS P04 / P13 in examples/Lecture 3/comp101_L3.owl.

Connects LanguageConstruct, Skill, Algorithm, and ConceptCarrier into the T-Box:
  - ConceptCarrier organizational class; LC/Operator/BuiltInFunction/DataType ⊑ it
  - Skill / Algorithm ⊑ owl:Thing (explicit)
  - ConceptCarrier used as domain/range of object properties (clears OOPS P04):
      implementedBy range = ConceptCarrier
      implementsConcept domain = ConceptCarrier
      usesConstruct range = ConceptCarrier
      constructUsedBy domain = ConceptCarrier
  - usesConcept domain = union(Skill, Algorithm, carriers, Method)
  - implementedBy domain = Concept only (not Algorithm — that broke
    owl:inverseOf with implementsConcept / OOPS P05);
  - implementsConcept domain/range = exact inverse mirror of implementedBy
  - isUsedBy range aligned as inverse of usesConcept
  - Algorithm→carrier edges belong on usesConstruct, not implementedBy
  - usesConstruct ⊣ constructUsedBy (owl:inverseOf both ways; clears OOPS P13)
"""

from __future__ import annotations

from pathlib import Path

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.term import BNode

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "examples" / "Lecture 3" / "comp101_L3.owl"
NS = "http://comp101.sase.um6p.ma/ontology/control-structures#"


def C(name: str) -> URIRef:
    return URIRef(NS + name)


def set_union_domain(g: Graph, prop: URIRef, members: list[str]) -> None:
    for old in list(g.objects(prop, RDFS.domain)):
        g.remove((prop, RDFS.domain, old))
    union = BNode()
    g.add((union, RDF.type, OWL.Class))
    members_node = BNode()
    Collection(g, members_node, [C(m) for m in members])
    g.add((union, OWL.unionOf, members_node))
    g.add((prop, RDFS.domain, union))


def set_union_range(g: Graph, prop: URIRef, members: list[str]) -> None:
    for old in list(g.objects(prop, RDFS.range)):
        g.remove((prop, RDFS.range, old))
    union = BNode()
    g.add((union, RDF.type, OWL.Class))
    members_node = BNode()
    Collection(g, members_node, [C(m) for m in members])
    g.add((union, OWL.unionOf, members_node))
    g.add((prop, RDFS.range, union))


def set_named_domain(g: Graph, prop: URIRef, cls: str) -> None:
    for old in list(g.objects(prop, RDFS.domain)):
        g.remove((prop, RDFS.domain, old))
    g.add((prop, RDFS.domain, C(cls)))


def set_named_range(g: Graph, prop: URIRef, cls: str) -> None:
    for old in list(g.objects(prop, RDFS.range)):
        g.remove((prop, RDFS.range, old))
    g.add((prop, RDFS.range, C(cls)))


def ensure_inverse_pair(g: Graph, a: URIRef, b: URIRef) -> None:
    for old in list(g.objects(a, OWL.inverseOf)):
        g.remove((a, OWL.inverseOf, old))
    for old in list(g.objects(b, OWL.inverseOf)):
        g.remove((b, OWL.inverseOf, old))
    g.add((a, OWL.inverseOf, b))
    g.add((b, OWL.inverseOf, a))


def _list_cells(g: Graph, head: BNode | URIRef) -> list:
    cells: list = []
    node: BNode | URIRef | None = head
    seen: set = set()
    while node is not None and node != RDF.nil and node not in seen:
        seen.add(node)
        cells.append(node)
        rests = list(g.objects(node, RDF.rest))
        node = rests[0] if rests else None
    return cells


def drop_unused_union_blank_nodes(g: Graph) -> None:
    """Remove owl:unionOf blank classes no longer used as domain/range.

    Prior rewrites often detached old unions without deleting them; leftover
    BNodes bloat the file and can leave ConceptCarrier appearing only in
    orphan lists.
    """
    for owner in list(g.subjects(OWL.unionOf, None)):
        if not isinstance(owner, BNode):
            continue
        used = bool(list(g.subjects(RDFS.domain, owner))) or bool(
            list(g.subjects(RDFS.range, owner))
        )
        # Still referenced by something other than its own type/unionOf?
        other = [
            p
            for s, p, o in g.triples((None, None, owner))
            if p not in (RDFS.domain, RDFS.range)
        ]
        if used or other:
            continue
        heads = list(g.objects(owner, OWL.unionOf))
        g.remove((owner, RDF.type, OWL.Class))
        for head in heads:
            g.remove((owner, OWL.unionOf, head))
            for cell in _list_cells(g, head):
                for p, o in list(g.predicate_objects(cell)):
                    g.remove((cell, p, o))


def main() -> None:
    g = Graph()
    g.parse(PATH)

    if (C("ConceptCarrier"), RDF.type, OWL.Class) not in g:
        g.add((C("ConceptCarrier"), RDF.type, OWL.Class))
        g.add((C("ConceptCarrier"), RDFS.label, Literal("ConceptCarrier")))

    # Refresh gloss: carriers include data types that realize concepts (e.g. bool).
    for old in list(g.objects(C("ConceptCarrier"), RDFS.comment)):
        g.remove((C("ConceptCarrier"), RDFS.comment, old))
    g.add((
        C("ConceptCarrier"),
        RDFS.comment,
        Literal(
            "Python surface that can realize or carry curriculum concepts "
            "(language constructs, operators, builtins, data types). "
            "Organizational superclass used as domain/range of realization "
            "and algorithm-construct properties."
        ),
    ))
    g.add((C("ConceptCarrier"), RDFS.subClassOf, OWL.Thing))

    for leaf in ("LanguageConstruct", "Operator", "BuiltInFunction", "DataType"):
        g.add((C(leaf), RDFS.subClassOf, C("ConceptCarrier")))

    g.add((C("Skill"), RDFS.subClassOf, OWL.Thing))
    g.add((C("Algorithm"), RDFS.subClassOf, OWL.Thing))

    set_union_domain(
        g,
        C("usesConcept"),
        [
            "Skill",
            "Algorithm",
            "LanguageConstruct",
            "Operator",
            "BuiltInFunction",
            "Method",
        ],
    )
    # Concept-only domain so implementedBy ⊣ implementsConcept is a true inverse
    # (OOP/ICS pattern). Algorithm→carrier uses usesConstruct instead.
    set_named_domain(g, C("implementedBy"), "Concept")
    # Named ConceptCarrier range (not leaf union) so ConceptCarrier participates
    # in property axioms — required to clear OOPS P04.
    set_named_range(g, C("implementedBy"), "ConceptCarrier")
    set_named_domain(g, C("implementsConcept"), "ConceptCarrier")
    set_named_range(g, C("implementsConcept"), "Concept")
    ensure_inverse_pair(g, C("implementedBy"), C("implementsConcept"))

    for old in list(g.objects(C("implementedBy"), RDFS.comment)):
        g.remove((C("implementedBy"), RDFS.comment, old))
    g.add((
        C("implementedBy"),
        RDFS.comment,
        Literal(
            "Concept is realized/expressed by this ConceptCarrier "
            "(language construct, operator, builtin, or data type). "
            "Inverse of implementsConcept. Domain is Concept only (not Algorithm)."
        ),
    ))
    for old in list(g.objects(C("implementsConcept"), RDFS.comment)):
        g.remove((C("implementsConcept"), RDFS.comment, old))
    g.add((
        C("implementsConcept"),
        RDFS.comment,
        Literal(
            "Inverse of implementedBy: a ConceptCarrier realizes or expresses "
            "this Concept. A-Box may assert only implementedBy; reasoners can "
            "infer this direction."
        ),
    ))

    set_union_range(
        g,
        C("isUsedBy"),
        [
            "Skill",
            "Algorithm",
            "LanguageConstruct",
            "Operator",
            "BuiltInFunction",
            "Method",
        ],
    )

    # usesConstruct ⊣ constructUsedBy
    set_named_domain(g, C("usesConstruct"), "Algorithm")
    set_named_range(g, C("usesConstruct"), "ConceptCarrier")
    for old in list(g.objects(C("usesConstruct"), RDFS.comment)):
        g.remove((C("usesConstruct"), RDFS.comment, old))
    g.add((
        C("usesConstruct"),
        RDFS.comment,
        Literal(
            "Algorithm is coded with / employs this ConceptCarrier "
            "(language construct, operator, builtin, or data type). "
            "Distinct from implementedBy (Concept → carrier realization). "
            "Inverse of constructUsedBy."
        ),
    ))
    if not list(g.objects(C("usesConstruct"), RDFS.label)):
        g.add((C("usesConstruct"), RDFS.label, Literal("uses_construct")))

    if (C("constructUsedBy"), RDF.type, OWL.ObjectProperty) not in g:
        g.add((C("constructUsedBy"), RDF.type, OWL.ObjectProperty))
    for old in list(g.objects(C("constructUsedBy"), RDFS.label)):
        g.remove((C("constructUsedBy"), RDFS.label, old))
    g.add((C("constructUsedBy"), RDFS.label, Literal("construct_used_by")))
    set_named_domain(g, C("constructUsedBy"), "ConceptCarrier")
    set_named_range(g, C("constructUsedBy"), "Algorithm")
    for old in list(g.objects(C("constructUsedBy"), RDFS.comment)):
        g.remove((C("constructUsedBy"), RDFS.comment, old))
    g.add((
        C("constructUsedBy"),
        RDFS.comment,
        Literal(
            "Inverse of usesConstruct: this ConceptCarrier is employed by "
            "the object Algorithm. A-Box may assert only usesConstruct; "
            "reasoners can infer this direction."
        ),
    ))
    ensure_inverse_pair(g, C("usesConstruct"), C("constructUsedBy"))

    drop_unused_union_blank_nodes(g)

    PATH.write_text(g.serialize(format="xml"), encoding="utf-8")
    print(f"Updated {PATH}")


if __name__ == "__main__":
    main()
