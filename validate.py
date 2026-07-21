#!/usr/bin/env python3.11
"""
validate.py — generic structural validation pipeline for OWL/RDF ontology files.

Runs, per ontology file:
  1. Parse / syntax validity
  2. Structural integrity   — dangling refs, self-loops, punning, orphans, labels
  3. OWL DL consistency     — HermiT via owlready2 (needs a JVM on PATH)
  4. OOPS! pitfall scan     — live REST API (https://oops.linkeddata.es/rest)
  5. OntoQA metrics         — schema + instance richness (ontometrics/ontology_metrics.py)
  6. SPARQL competency Qs   — optional, config-driven (configs/*.json)
  7. Skill-graph DAG checks — optional, config-driven
  8. Semantic judge (opt-in, --semantic) — two-phase LLM review of whether
     each asserted edge is actually TRUE, not just well-formed. Phase 1
     screens every edge in isolation; phase 2 re-checks flags against the
     local neighborhood and writes an evidence-based proposed fix. Never
     modifies the ontology file — read-only, report-only. Needs a local
     Ollama server (http://localhost:11434) with the model pulled.

Requires python3.11 (has rdflib/owlready2/owlrl installed on this machine —
check with `python3.11 -c "import rdflib, owlready2"` if unsure).

Usage:
  python3.11 validate.py                                    # validate the two known COMP101 ontologies
  python3.11 validate.py path/to/onto.owl                    # validate a single file
  python3.11 validate.py a.owl b.ttl some_dir/                # validate multiple files / all owl+ttl under a dir
  python3.11 validate.py onto.owl --config configs/x.json    # force a specific config
  python3.11 validate.py onto.owl --no-oops                  # skip the live OOPS! API call
  python3.11 validate.py onto.owl --no-reasoner               # skip the HermiT consistency check
  python3.11 validate.py onto.owl --oops-pitfalls P04,P11,P13 # restrict OOPS! to specific pitfalls
  python3.11 validate.py onto.owl --semantic                  # also run the LLM semantic judge (local Ollama)
  python3.11 validate.py onto.owl --semantic --semantic-model gemma4:26b
  python3.11 validate.py onto.owl --semantic --semantic-provider nvidia_nim \\
      --semantic-model mistralai/mistral-medium-3.5-128b       # hosted NVIDIA NIM (needs NVIDIA_API_KEY env var)
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from lib.graph_utils import detect_namespace, load_graph
from lib.llm_judge import run_semantic_judge
from lib.metrics import compute_ontoqa_metrics
from lib.oops_client import call_oops_api
from lib.reasoner import check_consistency
from lib.report import summarize, write_html, write_json
from lib.skill_graph import check_skill_graph
from lib.sparql_cq import run_sparql_cqs
from lib.structural import check_structural

ROOT = Path(__file__).parent
CONFIGS_DIR = ROOT / "configs"
RESULTS_DIR = ROOT / "results"

DEFAULT_TARGETS = [
    ROOT.parent / "final/oop/comp101_oop.owl",
    ROOT.parent / "final/ics/comp101_ics.owl",
]

ONTOLOGY_EXTS = {".owl", ".ttl", ".rdf", ".n3", ".nt", ".jsonld"}


def find_config_for_namespace(ns_uri: str) -> dict | None:
    if not ns_uri:
        return None
    for cfg_path in sorted(CONFIGS_DIR.glob("*.json")):
        try:
            cfg = json.loads(cfg_path.read_text())
        except json.JSONDecodeError:
            continue
        if cfg.get("namespace") == ns_uri:
            return cfg
    return None


def collect_targets(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in ONTOLOGY_EXTS))
        elif p.exists():
            files.append(p)
        else:
            print(f"WARNING: path not found, skipping: {p}", file=sys.stderr)
    return files


def validate_fast(path: Path, config_override: dict | None, use_oops: bool,
                   use_reasoner: bool, oops_pitfalls: str) -> dict:
    """Steps 1-6 -- everything except the semantic judge. Seconds, not
    minutes, so a caller can show this immediately while the (slow,
    LLM-backed) semantic judge runs separately -- see run_semantic_only().
    `result["semantic"]` is left as a 'pending' placeholder for the caller
    to fill in; validate_one() below does that synchronously for the CLI,
    the webapp does it in a background thread instead."""
    print(f"\n{'='*70}\n  {path}\n{'='*70}")
    ts = datetime.now().isoformat()

    g, err = load_graph(path)
    if err:
        print(f"  ✗ PARSE FAILED: {err}")
        return {
            "name": config_override.get("name") if config_override else path.stem,
            "source_path": str(path), "namespace": None,
            "timestamp": ts, "triple_count": 0, "parse_error": err,
        }

    ns_uri = (config_override or {}).get("namespace") or detect_namespace(g)
    config = config_override or find_config_for_namespace(ns_uri) or {}
    name = config.get("name") or path.stem
    id_property = config.get("id_property")

    print(f"  Namespace: {ns_uri or '(none detected)'}")
    print(f"  Triples:   {len(g)}")
    print(f"  Config:    {'matched — ' + config['name'] if config.get('name') else 'none (generic checks only)'}")

    print("  [1/6] Structural integrity...", end=" ", flush=True)
    structural = check_structural(g, ns_uri, id_property)
    print(f"{len(structural['issues'])} issues, {len(structural['warnings'])} warnings")

    if use_reasoner:
        print("  [2/6] OWL DL consistency (HermiT)...", end=" ", flush=True)
        consistency = check_consistency(g)
        print("consistent" if consistency.get("consistent") else (consistency.get("error") or "unknown"))
    else:
        consistency = {"ok": False, "consistent": None, "unsatisfiable_classes": [],
                        "error": "skipped (--no-reasoner)"}
        print("  [2/6] OWL DL consistency... skipped")

    if use_oops:
        print("  [3/6] OOPS! pitfall scan (live API)...", end=" ", flush=True)
        content = g.serialize(format="xml")
        oops = call_oops_api(content, pitfalls=oops_pitfalls)
        if oops["ok"]:
            print(f"{len(oops['pitfalls'])} pitfalls, {len(oops['suggestions'])} suggestions")
        else:
            print(f"unavailable ({oops['error']})")
    else:
        oops = {"ok": False, "error": "skipped (--no-oops)", "pitfalls": [], "warnings": [], "suggestions": []}
        print("  [3/6] OOPS! pitfall scan... skipped")

    print("  [4/6] OntoQA metrics...", end=" ", flush=True)
    ontoqa = compute_ontoqa_metrics(g, path)
    print(f"RR={ontoqa['schema']['relationship_richness']} CR={ontoqa['instance']['class_richness']}")

    print("  [5/6] SPARQL competency questions...", end=" ", flush=True)
    sparql_cqs = run_sparql_cqs(g, ns_uri, config["competency_questions"]) if config.get("competency_questions") else []
    print(f"{sum(1 for c in sparql_cqs if c.get('passed'))}/{len(sparql_cqs)} passed" if sparql_cqs else "no config, skipped")

    print("  [6/7] Skill graph...", end=" ", flush=True)
    skill_graph = check_skill_graph(g, ns_uri, config["skill_graph"]) if config.get("skill_graph") else None
    print(f"{len(skill_graph['issues'])} issues" if skill_graph else "no config, skipped")

    semantic_pending = {"ok": False, "error": "pending", "model": None, "provider": None,
                         "phase1_total_claims": 0, "phase1_flagged": 0,
                         "phase2_resolved": [], "issues": []}

    result = {
        "name": name,
        "source_path": str(path),
        "namespace": ns_uri,
        "timestamp": ts,
        "triple_count": len(g),
        "parse_error": None,
        "structural": structural,
        "consistency": consistency,
        "oops": oops,
        "ontoqa": ontoqa,
        "sparql_cqs": sparql_cqs,
        "skill_graph": skill_graph,
        "semantic": semantic_pending,
    }
    result["summary"] = summarize(result)
    return result


def run_semantic_only(path: Path, config_override: dict | None,
                       semantic_model: str = "gemma4:26b",
                       semantic_provider: str = "ollama") -> dict:
    """Just the semantic judge, re-parsing the graph fresh. Kept separate
    from validate_fast so a caller can run it in a background thread well
    after the fast result was already shown -- rdflib Graph reads aren't
    guaranteed thread-safe, so this re-parses rather than sharing a Graph
    object across threads."""
    g, err = load_graph(path)
    if err:
        return {"ok": False, "error": err, "model": semantic_model, "provider": semantic_provider,
                "phase1_total_claims": 0, "phase1_flagged": 0, "phase2_resolved": [], "issues": []}
    ns_uri = (config_override or {}).get("namespace") or detect_namespace(g)
    config = config_override or find_config_for_namespace(ns_uri) or {}
    return run_semantic_judge(g, config, provider_type=semantic_provider, model=semantic_model, ns_uri=ns_uri)


def validate_one(path: Path, config_override: dict | None, use_oops: bool,
                  use_reasoner: bool, oops_pitfalls: str,
                  use_semantic: bool = False, semantic_model: str = "gemma4:26b",
                  semantic_provider: str = "ollama") -> dict:
    """CLI entry point: fast checks then (optionally) semantic, synchronously
    in one call -- same blocking behaviour as before this was split. The
    webapp instead calls validate_fast() + run_semantic_only() separately so
    it can show fast results before semantic finishes."""
    result = validate_fast(path, config_override, use_oops, use_reasoner, oops_pitfalls)
    if result.get("parse_error"):
        return result

    if use_semantic:
        print(f"  [7/7] Semantic judge (LLM, {semantic_provider})...", end=" ", flush=True)
        semantic = run_semantic_only(path, config_override, semantic_model, semantic_provider)
        if semantic["ok"]:
            n_reviewed = len(semantic.get("reviewed_skipped", []))
            n_gated = semantic.get("gated_count", 0)
            reviewed_note = f", {n_reviewed} already reviewed (skipped)" if n_reviewed else ""
            gated_note = f", {n_gated} gated (ungroundable relation)" if n_gated else ""
            print(f"{semantic['phase1_flagged']}/{semantic['phase1_total_claims']} flagged, "
                  f"{len(semantic['issues'])} confirmed, "
                  f"{len(semantic.get('phase2_unverifiable', []))} unverifiable after re-check"
                  f"{reviewed_note}{gated_note}")
        else:
            print(f"unavailable ({semantic['error']})")
    else:
        semantic = {"ok": False, "error": "skipped (pass --semantic to enable)", "model": None,
                    "provider": None, "phase1_total_claims": 0, "phase1_flagged": 0,
                    "phase2_resolved": [], "issues": []}
        print("  [7/7] Semantic judge... skipped")

    result["semantic"] = semantic
    result["summary"] = summarize(result)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Generic OWL/RDF ontology structural validation pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("targets", nargs="*", help="OWL/TTL file(s) or directories to validate")
    parser.add_argument("--config", type=Path, help="Force a specific config JSON for all targets")
    parser.add_argument("--no-oops", action="store_true", help="Skip the live OOPS! REST API call")
    parser.add_argument("--no-reasoner", action="store_true", help="Skip the HermiT consistency check")
    parser.add_argument("--oops-pitfalls", default="", help="Comma-separated pitfall codes, e.g. P04,P11")
    parser.add_argument("--semantic", action="store_true",
                         help="Run the two-phase LLM semantic judge")
    parser.add_argument("--semantic-provider", choices=["ollama", "nvidia_nim"], default="ollama",
                         help="Backend for the semantic judge: local Ollama (default) or hosted NVIDIA NIM "
                              "(needs NVIDIA_API_KEY env var)")
    parser.add_argument("--semantic-model", default="gemma4:26b",
                         help="Model tag/id — an Ollama tag (e.g. gemma4:26b) or a NIM model id "
                              "(e.g. mistralai/mistral-medium-3.5-128b)")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR, help="Output directory for reports")
    args = parser.parse_args()

    targets = collect_targets(args.targets) if args.targets else DEFAULT_TARGETS
    if not targets:
        print("No ontology files found to validate.", file=sys.stderr)
        sys.exit(1)

    config_override = json.loads(args.config.read_text()) if args.config else None

    results = [
        validate_one(t, config_override, not args.no_oops, not args.no_reasoner, args.oops_pitfalls,
                     use_semantic=args.semantic, semantic_model=args.semantic_model,
                     semantic_provider=args.semantic_provider)
        for t in targets
    ]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = write_json(results, args.out, stamp)
    html_path = write_html(results, args.out, stamp)

    print(f"\n{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")
    for r in results:
        if r.get("parse_error"):
            print(f"  ✗ {r['name']}: PARSE FAILED")
            continue
        s = r["summary"]
        print(f"  {r['name']}: struct={s['structural_issues']}issues/{s['structural_warnings']}warn  "
              f"oops={s['oops_critical']}crit/{s['oops_important']}imp/{s['oops_minor']}minor  "
              f"consistent={r['consistency'].get('consistent')}  "
              f"cq={s['cq_passed']}/{s['cq_total']}  "
              f"semantic_issues={s.get('semantic_issues', 0)}")

    print(f"\n  JSON report: {json_path}")
    print(f"  HTML report: {html_path}")


if __name__ == "__main__":
    main()
