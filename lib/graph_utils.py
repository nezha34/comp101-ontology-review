"""
graph_utils.py — parsing, namespace detection, and small RDF helpers
shared by every check module in the validation pipeline.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef
from rdflib.term import BNode, Literal

FORMAT_BY_SUFFIX = {
    ".owl": "xml",
    ".rdf": "xml",
    ".xml": "xml",
    ".ttl": "turtle",
    ".n3": "n3",
    ".nt": "nt",
    ".jsonld": "json-ld",
}


def load_graph(path: Path) -> tuple[Optional[Graph], Optional[str]]:
    """Parse an ontology file into an rdflib Graph.

    Returns (graph, None) on success or (None, error_message) on failure.
    Tries the format implied by the file suffix first, then falls back
    to rdflib's format guessing so a mislabeled extension doesn't cause
    a false "invalid file" verdict.
    """
    guessed = FORMAT_BY_SUFFIX.get(path.suffix.lower())
    tried = []

    for fmt in [guessed, None]:
        if fmt in tried:
            continue
        tried.append(fmt)
        g = Graph()
        try:
            if fmt:
                g.parse(str(path), format=fmt)
            else:
                g.parse(str(path))
            return g, None
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"

    return None, last_error


def detect_namespace(g: Graph) -> str:
    """Best-effort guess of the ontology's primary namespace URI.

    Preference order:
      1. The subject of an owl:Ontology declaration (split at # or last /).
      2. The most common namespace among declared classes/properties.
    """
    onto_subjects = list(g.subjects(RDF.type, OWL.Ontology))
    if onto_subjects:
        uri = str(onto_subjects[0])
        if not uri.endswith(("#", "/")):
            uri += "#"
        return uri

    counter: Counter[str] = Counter()
    for s in list(g.subjects(RDF.type, OWL.Class)) + \
             list(g.subjects(RDF.type, OWL.ObjectProperty)) + \
             list(g.subjects(RDF.type, OWL.DatatypeProperty)) + \
             list(g.subjects(RDF.type, OWL.NamedIndividual)):
        if isinstance(s, URIRef):
            uri = str(s)
            if "#" in uri:
                counter[uri.rsplit("#", 1)[0] + "#"] += 1
            elif "/" in uri:
                counter[uri.rsplit("/", 1)[0] + "/"] += 1

    if counter:
        return counter.most_common(1)[0][0]
    return ""


def label(g: Graph, node) -> str:
    # A Literal (e.g. a free-text `definition` value used as context, not a
    # node) has no local-name to strip -- the "#"/"/" split below is only
    # correct for URIs. Applying it to prose truncated every definition at
    # its last "/" (e.g. "getter/setter", "stdin/stdout/stderr"), silently
    # feeding the semantic judge half a sentence. Bug found 2026-07-17 after
    # several LLM-flagged "issues" turned out to cite evidence that had been
    # chopped off before the model ever saw it.
    if isinstance(node, Literal):
        return str(node)
    l = g.value(node, RDFS.label)
    if l:
        return str(l)
    s = str(node)
    return s.rsplit("#", 1)[-1].rsplit("/", 1)[-1] if s else s


def named_individuals(g: Graph) -> list:
    return [s for s in g.subjects(RDF.type, OWL.NamedIndividual) if not isinstance(s, BNode)]


def named_classes(g: Graph) -> list:
    classes = set(s for s in g.subjects(RDF.type, OWL.Class) if not isinstance(s, BNode))
    classes |= set(s for s in g.subjects(RDF.type, RDFS.Class) if not isinstance(s, BNode))
    classes.discard(OWL.Thing)
    classes.discard(OWL.Nothing)
    return list(classes)


def object_properties(g: Graph) -> list:
    return [s for s in g.subjects(RDF.type, OWL.ObjectProperty) if not isinstance(s, BNode)]


def datatype_properties(g: Graph) -> list:
    return [s for s in g.subjects(RDF.type, OWL.DatatypeProperty) if not isinstance(s, BNode)]
