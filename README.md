# COMP101 Ontology Review

Personal / shared team toolkit for **reviewing week ontologies as OWL**.

**Flow:** drop `.owl` / `.ttl` → structural + KPIs + optional HermiT/OOPS + CQs + **vocab diff** vs shared COMP101 T-Box → HTML/JSON report.

**Not in this repo:** Gnosis merge, Neo4j load, tutor APIs. Those stay in `Gnosis_core` (JSON CI + dry-merge).

## Quick start

```bash
cd ~/Documents/comp101-ontology-review
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Web UI — drag & drop validate one, or compare two
python -m ontology_review.web.app
# open http://127.0.0.1:8765

# CLI (offline-friendly)
python -m ontology_review examples/comp101_oop.owl --no-oops --no-reasoner

# Full pass (needs JVM for HermiT; network for OOPS!)
python -m ontology_review examples/comp101_oop.owl
```

Reports land in `results/validation_report_*.{html,json}` (and `compare_report_*` for pairwise).

## What you get

| Step | What |
|------|------|
| Parse | rdflib load — fail fast on broken RDF |
| Structural | dangling refs, punning, self-loops, orphans, labels… |
| HermiT | OWL DL consistency (`--no-reasoner` to skip) |
| OOPS! | pitfall scan (`--no-oops` offline) |
| OntoQA | schema/instance richness KPIs |
| SPARQL CQs | if `configs/*.json` matches the ontology namespace |
| Skill graph | DAG + concept links (config-driven) |
| **Vocab diff** | classes / object properties / edge types vs `baselines/comp101_shared_tbox.json` |

## Layout

```text
ontology_review/
  __main__.py          CLI entry
  pipeline.py          shared validate / compare
  web/                 drag-drop UI (FastAPI)
  lib/                 validation layers + vocab_diff
  semantic/            LLM lane stubs (neighbors → review)
  configs/             per-module CQ + skill_graph (oop, ics, generic, …)
  baselines/           shared COMP101 T-Box for diff
examples/              sample OWL modules
results/               generated reports (gitignored)
uploads/               UI drops (gitignored)
```

### UI modes

| Mode | What happens |
|------|----------------|
| **Validate one** | Full pipeline + vocab diff vs shared T-Box |
| **Compare two** | Both validated; pairwise A↔B class / property / edge diff on top |

## Competency questions (CQ packs)

| Pack | When to use |
|------|-------------|
| `comp101_oop` | Only the OOP module (hardcodes `SKOOP001`, `L10`, …) |
| `comp101_ics` | Only the ICS module |
| `comp101_generic` | **Any** week OWL — shape checks (`Concept`/`Skill`/`dependsOn`/…), no instance IDs |
| Auto | Match namespace → else generic |

UI: **CQ pack** dropdown. CLI: `--config-id comp101_generic`.

## Adding your module

1. Save Protégé export as `my_week.owl`.
2. Prefer `configs/comp101_<week>.json` with module CQs; until then Auto/generic still runs shape checks.
3. Run: `python -m ontology_review my_week.owl --config-id auto --no-oops --no-reasoner`
4. Open the HTML report — structural, CQs pack used, vocab extras vs shared T-Box.

## Shared T-Box

`ontology_review/baselines/comp101_shared_tbox.json` lists the **core classes and object properties** the team agrees on. Module-local extras (e.g. `HardwareComponent`, `partOf`) show up in the vocab-diff panel — expected until the baseline is updated by DE.

## LLM edge review (planned)

Deterministic layers first. A later optional pass can score asserted edges (`dependsOn`, `taughtIn`, …) as `ok | suspect | unclear` for human accept/reject — **not** a merge gate.

## Relation to Gnosis

| This repo | Gnosis_core |
|-----------|-------------|
| OWL author QA | JSON P0 + dry-merge + tutor |
| Shared with week authors | Serving API / Neo4j |

When a module is approved here, convert OWL → Gnosis JSON and run `/ontology-ci` dry-merge there.
