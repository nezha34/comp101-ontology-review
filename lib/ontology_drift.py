"""
ontology_drift.py — T-Box vocabulary drift between a candidate ontology
and a baseline ontology.

Used by the semantic judge so unknown / module-new classes and object
properties are not invented by the LLM: gap entities are listed with their
OWL rdfs:comment (and domain/range for properties), and gap properties that
lack a config gloss can be soft-filled from the candidate comment.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from rdflib import RDFS, Graph
from rdflib.term import BNode

from .graph_utils import (
    label,
    load_graph,
    local_name,
    named_classes,
    object_properties,
    property_shape,
)

DRIFT_GLOSS_PREFIX = "[Drift vs baseline — from candidate OWL T-Box] "


def _index_by_local_name(uris) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for uri in uris:
        if isinstance(uri, BNode):
            continue
        name = local_name(uri)
        if name and name not in out:
            out[name] = uri
    return out


def _class_entry(g: Graph, uri) -> dict:
    parents = sorted(
        {
            local_name(p)
            for p in g.objects(uri, RDFS.subClassOf)
            if not isinstance(p, BNode) and local_name(p) not in ("Thing", "Nothing", "")
        }
    )
    comment = g.value(uri, RDFS.comment)
    return {
        "id": local_name(uri),
        "label": label(g, uri),
        "comment": str(comment).strip() if comment else None,
        "subclass_of": parents,
        "uri": str(uri),
    }


def _property_entry(g: Graph, uri) -> dict:
    shape = property_shape(g, uri)
    return {
        "id": local_name(uri),
        "label": label(g, uri),
        "comment": shape.get("comment"),
        "domain": shape.get("domain") or [],
        "range": shape.get("range") or [],
        "uri": str(uri),
    }


def compute_tbox_drift(candidate: Graph, baseline: Graph) -> dict:
    """Diff named classes and object properties by local name.

    Returns a JSON-serializable report. Matching is by local name (not full
    IRI) so shared COMP101 vocabulary across slightly different namespaces
    still aligns.
    """
    cand_classes = _index_by_local_name(named_classes(candidate))
    base_classes = _index_by_local_name(named_classes(baseline))
    cand_props = _index_by_local_name(object_properties(candidate))
    base_props = _index_by_local_name(object_properties(baseline))

    classes_added = [
        _class_entry(candidate, cand_classes[n])
        for n in sorted(set(cand_classes) - set(base_classes))
    ]
    classes_removed = [
        _class_entry(baseline, base_classes[n])
        for n in sorted(set(base_classes) - set(cand_classes))
    ]
    properties_added = [
        _property_entry(candidate, cand_props[n])
        for n in sorted(set(cand_props) - set(base_props))
    ]
    properties_removed = [
        _property_entry(baseline, base_props[n])
        for n in sorted(set(base_props) - set(cand_props))
    ]

    return {
        "ok": True,
        "error": None,
        "classes_added": classes_added,
        "classes_removed": classes_removed,
        "properties_added": properties_added,
        "properties_removed": properties_removed,
        "summary": {
            "classes_added": len(classes_added),
            "classes_removed": len(classes_removed),
            "properties_added": len(properties_added),
            "properties_removed": len(properties_removed),
            "candidate_classes": len(cand_classes),
            "baseline_classes": len(base_classes),
            "candidate_properties": len(cand_props),
            "baseline_properties": len(base_props),
        },
    }


def load_baseline_graph(path: Path | str) -> tuple[Graph | None, str | None]:
    """Parse a baseline ontology file. Returns (graph, error)."""
    return load_graph(Path(path))


def resolve_baseline_path(
    raw: str | Path | None,
    *,
    toolkit_root: Path | None = None,
) -> Path | None:
    """Resolve config/CLI baseline path. Relative paths are from toolkit root."""
    if not raw:
        return None
    p = Path(raw)
    if p.is_file():
        return p.resolve()
    root = toolkit_root or Path(__file__).resolve().parent.parent
    alt = (root / p).resolve()
    if alt.is_file():
        return alt
    return p  # may not exist; caller reports load error


def apply_drift_to_config(candidate: Graph, config: dict, drift: dict) -> dict:
    """Shallow-copy config; soft-fill gap relation/class glosses from OWL.

    For object properties present in the candidate but absent from the
    baseline, if they have no config/default relation_semantics yet and the
    candidate declares an rdfs:comment, register that comment as the gloss
    (prefixed so humans can tell it came from drift, not curated config).

    Gap classes with comments are merged into class_semantics the same way.
    """
    from .prompts import DEFAULT_RELATION_SEMANTICS

    cfg = deepcopy(config or {})
    rel_sem = dict(cfg.get("relation_semantics") or {})
    class_sem = dict(cfg.get("class_semantics") or {})
    filled_relations: list[str] = []
    filled_classes: list[str] = []
    still_missing: list[str] = []

    known = set(rel_sem) | set(DEFAULT_RELATION_SEMANTICS)

    for prop in drift.get("properties_added") or []:
        name = prop["id"]
        if name in known:
            continue
        comment = (prop.get("comment") or "").strip()
        if comment:
            rel_sem[name] = DRIFT_GLOSS_PREFIX + comment
            filled_relations.append(name)
            known.add(name)
        else:
            still_missing.append(name)

    for cls in drift.get("classes_added") or []:
        name = cls["id"]
        if name in class_sem:
            continue
        comment = (cls.get("comment") or "").strip()
        if comment:
            class_sem[name] = comment
            filled_classes.append(name)

    cfg["relation_semantics"] = rel_sem
    cfg["class_semantics"] = class_sem
    cfg["_ontology_drift"] = drift
    cfg["_drift_filled_relations"] = filled_relations
    cfg["_drift_filled_classes"] = filled_classes
    cfg["_drift_relations_still_missing"] = still_missing
    return cfg


def format_drift_vocabulary_block(drift: dict, *, max_items: int = 40) -> str | None:
    """Compact prompt block listing gap classes/properties with descriptions."""
    props = [p for p in (drift.get("properties_added") or []) if p.get("comment")]
    classes = [c for c in (drift.get("classes_added") or []) if c.get("comment")]
    if not props and not classes:
        # Still mention unnamed gaps so the model knows the catalog is incomplete
        bare_props = drift.get("properties_added") or []
        bare_classes = drift.get("classes_added") or []
        if not bare_props and not bare_classes:
            return None
        lines = [
            "Vocabulary new relative to the baseline ontology "
            "(present in the file under review, absent from baseline):",
        ]
        if bare_props:
            names = ", ".join(p["id"] for p in bare_props[:max_items])
            lines.append(f"  New properties (no rdfs:comment on some/all): {names}")
        if bare_classes:
            names = ", ".join(c["id"] for c in bare_classes[:max_items])
            lines.append(f"  New classes (no rdfs:comment on some/all): {names}")
        lines.append(
            "Do not invent meanings for undeclared vocabulary; use uncertain/unverifiable."
        )
        return "\n".join(lines)

    lines = [
        "Vocabulary new relative to the baseline ontology "
        "(use these OWL descriptions; do not invent other meanings):",
    ]
    if props:
        lines.append("  New object properties:")
        for p in props[:max_items]:
            dom = ", ".join(p.get("domain") or []) or "?"
            rng = ", ".join(p.get("range") or []) or "?"
            lines.append(
                f'  - {p["id"]} (domain=[{dom}]; range=[{rng}]): {p["comment"]}'
            )
        if len(props) > max_items:
            lines.append(f"  - … and {len(props) - max_items} more properties")
    if classes:
        lines.append("  New classes:")
        for c in classes[:max_items]:
            lines.append(f'  - {c["id"]}: {c["comment"]}')
        if len(classes) > max_items:
            lines.append(f"  - … and {len(classes) - max_items} more classes")
    lines.append(
        "Do not invent meanings beyond this block, the relation gloss, and declared domain/range."
    )
    return "\n".join(lines)


def prepare_config_with_baseline(
    candidate: Graph,
    config: dict | None,
    baseline_path: Path | str | None,
    *,
    toolkit_root: Path | None = None,
) -> tuple[dict, dict | None]:
    """Load baseline (if any), compute drift, apply soft-fills.

    Returns (config_for_judge, drift_report_or_None).
    If baseline_path is None, returns (config, None) unchanged aside from copy.
    If baseline fails to load, drift report has ok=False and config is unchanged.
    """
    cfg = deepcopy(config or {})
    path = resolve_baseline_path(baseline_path, toolkit_root=toolkit_root)
    if path is None:
        return cfg, None

    baseline, err = load_baseline_graph(path)
    if err or baseline is None:
        drift = {
            "ok": False,
            "error": f"Failed to load baseline {path}: {err}",
            "baseline_path": str(path),
            "classes_added": [],
            "classes_removed": [],
            "properties_added": [],
            "properties_removed": [],
            "summary": {},
        }
        cfg["_ontology_drift"] = drift
        return cfg, drift

    drift = compute_tbox_drift(candidate, baseline)
    drift["baseline_path"] = str(path.resolve())
    cfg = apply_drift_to_config(candidate, cfg, drift)
    return cfg, drift
