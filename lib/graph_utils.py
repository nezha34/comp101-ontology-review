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


def local_name(uri) -> str:
    s = str(uri)
    return s.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _first_literal(g: Graph, node, *predicates) -> str | None:
    for pred in predicates:
        val = g.value(node, pred)
        if val is not None:
            text = str(val).strip()
            if text:
                return text
    return None


def property_shape(g: Graph, prop) -> dict:
    """T-Box for an object property: domain/range local names + rdfs:comment.

    Used by the semantic judge so the LLM sees declared shape instead of
    inventing endpoint-type restrictions.
    """
    domains = sorted({local_name(d) for d in g.objects(prop, RDFS.domain) if not isinstance(d, BNode)})
    ranges = sorted({local_name(r) for r in g.objects(prop, RDFS.range) if not isinstance(r, BNode)})
    return {
        "domain": domains,
        "range": ranges,
        "comment": _first_literal(g, prop, RDFS.comment),
    }


def class_comment(g: Graph, class_uri, config: dict | None = None) -> str | None:
    """Class gloss: config class_semantics overlay, else rdfs:comment on the class."""
    name = local_name(class_uri)
    custom = ((config or {}).get("class_semantics") or {}).get(name)
    if custom:
        return str(custom).strip() or None
    return _first_literal(g, class_uri, RDFS.comment)


def class_glossary_for_types(
    g: Graph,
    type_uris: list | set,
    config: dict | None = None,
) -> list[tuple[str, str]]:
    """Unique (class_local_name, comment) for rdf:types appearing in a batch."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for t in type_uris:
        if isinstance(t, BNode):
            continue
        name = local_name(t)
        if name in seen or name in ("NamedIndividual", "Thing", "Nothing"):
            continue
        seen.add(name)
        gloss = class_comment(g, t, config)
        if gloss:
            out.append((name, gloss))
    return sorted(out, key=lambda x: x[0])


def class_glossary_for_names(
    g: Graph,
    class_names: list[str] | set[str],
    config: dict | None = None,
) -> list[tuple[str, str]]:
    """Glossary entries for class local names (e.g. declared domain/range).

    Looks up a named class URI in the graph when possible; falls back to
    config class_semantics only.
    """
    by_name: dict[str, object] = {}
    for c in named_classes(g):
        by_name.setdefault(local_name(c), c)

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in sorted(set(class_names)):
        if not name or name in seen or name in ("NamedIndividual", "Thing", "Nothing"):
            continue
        seen.add(name)
        uri = by_name.get(name)
        if uri is not None:
            gloss = class_comment(g, uri, config)
        else:
            gloss = ((config or {}).get("class_semantics") or {}).get(name)
            gloss = str(gloss).strip() if gloss else None
        if gloss:
            out.append((name, gloss))
    return out


def merge_class_glossaries(
    *glossaries: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Union of (name, gloss) lists; first occurrence wins."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for gloss in glossaries:
        for name, text in gloss or []:
            if name in seen:
                continue
            seen.add(name)
            out.append((name, text))
    return sorted(out, key=lambda x: x[0])


def individual_definition(g: Graph, node, ns_uri: str | None = None) -> str | None:
    """Short individual gloss from definition annotation or rdfs:comment."""
    preds = [RDFS.comment]
    if ns_uri:
        preds.insert(0, Namespace(ns_uri)["definition"])
    # Also try a bare relative lookup when ns unknown: scan annotation props named definition
    text = _first_literal(g, node, *preds)
    if text:
        return text
    for pred in g.predicates(node):
        if local_name(pred) == "definition":
            val = g.value(node, pred)
            if val is not None and str(val).strip():
                return str(val).strip()
    return None
