"""Shared validation pipeline used by CLI and the web UI."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ontology_review.lib.graph_utils import detect_namespace, load_graph
from ontology_review.lib.metrics import compute_ontoqa_metrics
from ontology_review.lib.oops_client import call_oops_api
from ontology_review.lib.reasoner import check_consistency
from ontology_review.lib.report import summarize
from ontology_review.lib.skill_graph import check_skill_graph
from ontology_review.lib.sparql_cq import run_sparql_cqs
from ontology_review.lib.structural import check_structural
from ontology_review.lib.vocab_diff import compare_owl_vocabs, vocab_diff

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
CONFIGS_DIR = ROOT / "configs"
BASELINES_DIR = ROOT / "baselines"
DEFAULT_BASELINE = BASELINES_DIR / "comp101_shared_tbox.json"
DEFAULT_RESULTS = REPO / "results"
ONTOLOGY_EXTS = {".owl", ".ttl", ".rdf", ".n3", ".nt", ".jsonld"}


def load_all_configs() -> list[dict]:
    configs: list[dict] = []
    for cfg_path in sorted(CONFIGS_DIR.glob("*.json")):
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        cfg["_path"] = str(cfg_path)
        cfg["_id"] = cfg_path.stem
        configs.append(cfg)
    return configs


def find_config_by_id(config_id: str | None) -> dict | None:
    if not config_id or config_id in {"auto", ""}:
        return None
    for cfg in load_all_configs():
        if cfg["_id"] == config_id or cfg.get("name") == config_id:
            return {k: v for k, v in cfg.items() if not k.startswith("_")}
    return None


def find_fallback_config() -> dict | None:
    for cfg in load_all_configs():
        if cfg.get("match") == "fallback" or cfg["_id"] == "comp101_generic":
            return {k: v for k, v in cfg.items() if not k.startswith("_")}
    return None


def find_config_for_namespace(ns_uri: str) -> dict | None:
    if not ns_uri:
        return None
    for cfg in load_all_configs():
        if cfg.get("namespace") == ns_uri:
            return {k: v for k, v in cfg.items() if not k.startswith("_")}
    return None


def resolve_config(
    ns_uri: str | None,
    *,
    config_override: dict | None = None,
    config_id: str | None = None,
) -> tuple[dict, str]:
    """
    Returns (config, how_matched).
    Preference: explicit override → config_id → namespace match → generic fallback → {}.
    """
    if config_override:
        return config_override, "override"
    forced = find_config_by_id(config_id)
    if forced is not None:
        return forced, f"forced:{config_id}"
    matched = find_config_for_namespace(ns_uri or "")
    if matched:
        return matched, "namespace"
    fallback = find_fallback_config()
    if fallback:
        return fallback, "generic_fallback"
    return {}, "none"


def _log(quiet: bool, msg: str, end: str = "\n", flush: bool = False) -> None:
    if not quiet:
        print(msg, end=end, flush=flush)


def validate_one(
    path: Path,
    *,
    config_override: dict | None = None,
    config_id: str | None = None,
    use_oops: bool = True,
    use_reasoner: bool = True,
    oops_pitfalls: str = "",
    baseline: Path | None = None,
    quiet: bool = False,
) -> dict:
    baseline = baseline or DEFAULT_BASELINE
    ts = datetime.now().isoformat()
    _log(quiet, f"\n{'=' * 70}\n  {path}\n{'=' * 70}")

    g, err = load_graph(path)
    if err:
        _log(quiet, f"  PARSE FAILED: {err}")
        return {
            "name": (config_override or {}).get("name") or path.stem,
            "source_path": str(path),
            "namespace": None,
            "timestamp": ts,
            "triple_count": 0,
            "parse_error": err,
        }

    detected_ns = detect_namespace(g)
    ns_uri = (config_override or {}).get("namespace") or detected_ns
    config, match_how = resolve_config(
        detected_ns, config_override=config_override, config_id=config_id
    )
    # Module CQs (OOP/ICS) hardcode individuals under config.namespace.
    # Generic CQs use the uploaded file's namespace.
    cq_ns = config.get("namespace") or detected_ns
    graph_ns = detected_ns or cq_ns
    name = path.stem if match_how in {"generic_fallback", "none"} else (config.get("name") or path.stem)
    id_property = config.get("id_property")

    _log(quiet, f"  Namespace: {graph_ns or '(none detected)'}")
    _log(quiet, f"  Triples:   {len(g)}")
    _log(
        quiet,
        f"  Config:    {config.get('name') or '(none)'} [{match_how}]",
    )

    _log(quiet, "  [1/7] Structural integrity...", end=" ", flush=True)
    structural = check_structural(g, graph_ns, id_property)
    _log(
        quiet,
        f"{len(structural['issues'])} issues, {len(structural['warnings'])} warnings",
    )

    if use_reasoner:
        _log(quiet, "  [2/7] OWL DL consistency (HermiT)...", end=" ", flush=True)
        consistency = check_consistency(g)
        _log(
            quiet,
            "consistent"
            if consistency.get("consistent")
            else (consistency.get("error") or "unknown"),
        )
    else:
        consistency = {
            "ok": False,
            "consistent": None,
            "unsatisfiable_classes": [],
            "error": "skipped (--no-reasoner)",
        }
        _log(quiet, "  [2/7] OWL DL consistency... skipped")

    if use_oops:
        _log(quiet, "  [3/7] OOPS! pitfall scan (live API)...", end=" ", flush=True)
        content = g.serialize(format="xml")
        oops = call_oops_api(content, pitfalls=oops_pitfalls)
        if oops["ok"]:
            _log(
                quiet,
                f"{len(oops['pitfalls'])} pitfalls, {len(oops['suggestions'])} suggestions",
            )
        else:
            _log(quiet, f"unavailable ({oops['error']})")
    else:
        oops = {
            "ok": False,
            "error": "skipped (--no-oops)",
            "pitfalls": [],
            "warnings": [],
            "suggestions": [],
        }
        _log(quiet, "  [3/7] OOPS! pitfall scan... skipped")

    _log(quiet, "  [4/7] OntoQA metrics...", end=" ", flush=True)
    ontoqa = compute_ontoqa_metrics(g, path)
    _log(
        quiet,
        f"RR={ontoqa['schema']['relationship_richness']} "
        f"CR={ontoqa['instance']['class_richness']}",
    )

    _log(quiet, "  [5/7] SPARQL competency questions...", end=" ", flush=True)
    sparql_cqs = (
        run_sparql_cqs(g, cq_ns, config["competency_questions"])
        if config.get("competency_questions") and cq_ns
        else []
    )
    if sparql_cqs:
        _log(
            quiet,
            f"{sum(1 for c in sparql_cqs if c.get('passed'))}/{len(sparql_cqs)} passed",
        )
    else:
        _log(quiet, "no config, skipped")

    _log(quiet, "  [6/7] Skill graph...", end=" ", flush=True)
    skill_graph = (
        check_skill_graph(g, graph_ns, config["skill_graph"])
        if config.get("skill_graph") and graph_ns
        else None
    )
    _log(
        quiet,
        f"{len(skill_graph['issues'])} issues" if skill_graph else "no config, skipped",
    )

    _log(quiet, "  [7/7] Vocab diff vs shared T-Box...", end=" ", flush=True)
    if baseline.is_file():
        vdiff = vocab_diff(g, baseline, other_id=name)
        _log(
            quiet,
            f"+{vdiff['summary']['classes_added']} classes, "
            f"+{vdiff['summary']['object_properties_added']} props, "
            f"+{vdiff['summary']['edge_types_added']} edge types",
        )
    else:
        vdiff = {"error": f"baseline missing: {baseline}"}
        _log(quiet, "skipped (baseline missing)")

    result = {
        "name": name,
        "source_path": str(path),
        "namespace": graph_ns,
        "config_name": config.get("name"),
        "config_match": match_how,
        "timestamp": ts,
        "triple_count": len(g),
        "parse_error": None,
        "structural": structural,
        "consistency": consistency,
        "oops": oops,
        "ontoqa": ontoqa,
        "sparql_cqs": sparql_cqs,
        "skill_graph": skill_graph,
        "vocab_diff": vdiff,
        "_graph": g,  # for pairwise compare; strip before JSON write
    }
    result["summary"] = summarize(result)
    result["summary"]["vocab_classes_added"] = (vdiff.get("summary") or {}).get("classes_added")
    result["summary"]["vocab_props_added"] = (vdiff.get("summary") or {}).get(
        "object_properties_added"
    )
    return result


def strip_runtime(result: dict) -> dict:
    """Drop non-JSON runtime fields before writing reports."""
    return {k: v for k, v in result.items() if not k.startswith("_")}


def compare_two(
    path_a: Path,
    path_b: Path,
    *,
    config_id: str | None = None,
    use_oops: bool = False,
    use_reasoner: bool = False,
    baseline: Path | None = None,
    quiet: bool = True,
) -> dict:
    """Validate each file and compute A↔B vocabulary diff."""
    a = validate_one(
        path_a,
        config_id=config_id,
        use_oops=use_oops,
        use_reasoner=use_reasoner,
        baseline=baseline,
        quiet=quiet,
    )
    b = validate_one(
        path_b,
        config_id=config_id,
        use_oops=use_oops,
        use_reasoner=use_reasoner,
        baseline=baseline,
        quiet=quiet,
    )
    pairwise = None
    if not a.get("parse_error") and not b.get("parse_error"):
        pairwise = compare_owl_vocabs(
            a["_graph"],
            b["_graph"],
            id_a=a["name"],
            id_b=b["name"],
        )
    return {
        "mode": "compare",
        "left": strip_runtime(a),
        "right": strip_runtime(b),
        "pairwise_vocab": pairwise,
        "timestamp": datetime.now().isoformat(),
    }
