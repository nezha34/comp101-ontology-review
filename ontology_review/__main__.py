#!/usr/bin/env python3
"""
Drop an OWL/TTL ontology → structural checks, HermiT (opt), OOPS (opt),
OntoQA KPIs, SPARQL CQs, skill-graph checks, and vocab diff vs shared T-Box.

Usage:
  python -m ontology_review examples/comp101_oop.owl
  python -m ontology_review path/to/module.owl --no-oops --no-reasoner
  python -m ontology_review examples/ --out results/
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ontology_review.lib.report import write_html, write_json
from ontology_review.pipeline import (
    DEFAULT_BASELINE,
    DEFAULT_RESULTS,
    ONTOLOGY_EXTS,
    strip_runtime,
    validate_one,
)


def collect_targets(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in ONTOLOGY_EXTS))
        elif p.exists():
            files.append(p)
        else:
            print(f"WARNING: path not found, skipping: {p}", file=sys.stderr)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="COMP101 ontology review: drop OWL → KPIs + analysis (merge deferred)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="OWL/TTL file(s) or directories (default: examples/)",
    )
    parser.add_argument("--config", type=Path, help="Force a specific configs/*.json file")
    parser.add_argument(
        "--config-id",
        default="auto",
        help="Config pack id: auto | comp101_oop | comp101_ics | comp101_generic",
    )
    parser.add_argument("--no-oops", action="store_true", help="Skip live OOPS! API")
    parser.add_argument("--no-reasoner", action="store_true", help="Skip HermiT")
    parser.add_argument("--oops-pitfalls", default="", help="e.g. P04,P11")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="Shared T-Box JSON for vocab diff",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_RESULTS, help="Report output directory")
    args = parser.parse_args(argv)

    if args.targets:
        targets = collect_targets(args.targets)
    else:
        examples = REPO / "examples"
        targets = collect_targets([str(examples)]) if examples.is_dir() else []

    if not targets:
        print("No ontology files found. Pass a .owl path or put files in examples/.", file=sys.stderr)
        return 1

    config_override = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None

    results = [
        strip_runtime(
            validate_one(
                t,
                config_override=config_override,
                config_id=None if args.config else args.config_id,
                use_oops=not args.no_oops,
                use_reasoner=not args.no_reasoner,
                oops_pitfalls=args.oops_pitfalls,
                baseline=args.baseline.expanduser().resolve(),
            )
        )
        for t in targets
    ]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = write_json(results, args.out, stamp)
    html_path = write_html(results, args.out, stamp)

    print(f"\n{'=' * 70}\n  SUMMARY\n{'=' * 70}")
    for r in results:
        if r.get("parse_error"):
            print(f"  FAIL {r['name']}: PARSE FAILED")
            continue
        s = r["summary"]
        print(
            f"  {r['name']}: struct={s['structural_issues']}issues/"
            f"{s['structural_warnings']}warn  "
            f"oops={s['oops_critical']}crit/{s['oops_important']}imp/{s['oops_minor']}minor  "
            f"consistent={r['consistency'].get('consistent')}  "
            f"cq={s['cq_passed']}/{s['cq_total']}  "
            f"vocab=+{s.get('vocab_classes_added')}cls/+{s.get('vocab_props_added')}prop"
        )

    print(f"\n  JSON report: {json_path}")
    print(f"  HTML report: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
