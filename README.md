# COMP101 Ontology Validation

Toolkit for validating COMP101 week ontologies (OWL/RDF): structural
integrity, OWL DL consistency, the OOPS! pitfall scanner, OntoQA metrics,
SPARQL competency questions, skill-graph DAG checks, and an optional
two-phase LLM semantic judge that checks whether asserted relationships
are actually *true*, not just well-formed. See `VALIDATION_CHECKS.txt`
for what each layer does in detail.

## Quick start

```bash
python3.11 -m pip install -r requirements.txt   # rdflib, owlready2, owlrl, flask, requests

# CLI — validate one file, structural layers only
python3.11 validate.py path/to/onto.owl --no-oops --no-reasoner

# CLI — full pass (needs a JVM on PATH for HermiT; internet for OOPS!)
python3.11 validate.py path/to/onto.owl

# CLI — with the semantic judge (local Ollama, needs the model pulled)
python3.11 validate.py path/to/onto.owl --semantic --semantic-model gemma4:26b

# CLI — T-Box drift vs a baseline; gap class/relation comments feed the judge
python3.11 validate.py path/to/onto.owl --semantic \
    --semantic-baseline examples/comp101_oop.owl

# CLI — semantic judge via hosted NVIDIA NIM instead of local Ollama
export NVIDIA_API_KEY="nvapi-..."   # never hardcode this — env var only
python3.11 validate.py path/to/onto.owl --semantic --semantic-provider nvidia_nim \
    --semantic-model mistralai/mistral-medium-3.5-128b

# Web UI — drag-drop validate / compare (FastAPI, primary)
python -m web.app
# open http://127.0.0.1:8765

# Legacy Flask UI
cd webapp && python app.py
# open http://127.0.0.1:5000
```

Reports land in `results/validation_report_*.{html,json}` (gitignored).
Web UI runs are stored per-upload under `webapp/runs/<id>/` (also
gitignored — these are runtime artifacts, not source).

## What you get

| Layer | What | Module |
|---|---|---|
| 1. Parse | rdflib load — fail fast on broken RDF/XML or Turtle | `lib/graph_utils.py` |
| 2. Structural | dangling refs, self-loops, punning, orphans, missing labels, duplicate IDs | `lib/structural.py` |
| 3. OWL DL consistency | HermiT reasoner via owlready2 — is it logically satisfiable? (`--no-reasoner` to skip, needs a JVM) | `lib/reasoner.py` |
| 4. OOPS! pitfall scan | live call to the real OOPS! REST API, full P01–P41 catalogue (`--no-oops` to skip) | `lib/oops_client.py` |
| 5. OntoQA metrics | schema/instance richness (RR, AR, IR, CR, avg population, depth) | `lib/metrics.py` |
| 6. SPARQL competency Qs | config-driven, only runs if `configs/*.json` matches the ontology's namespace | `lib/sparql_cq.py` |
| 7. Skill graph | DAG + concept-link checks, config-driven | `lib/skill_graph.py` |
| 8. Semantic judge | **opt-in** (`--semantic`), two-phase LLM review of whether each edge is actually true | `lib/llm_judge.py` |
| T-Box drift | optional (`--semantic-baseline` or config `semantic_baseline`) — gap classes/properties vs a baseline OWL, with comments fed into the judge | `lib/ontology_drift.py` |

Layers 1–7 run in seconds. Layer 8 is slow (LLM calls) and always opt-in.

## The semantic judge (layer 8)

None of layers 1–7 check whether an asserted relationship is *true* in
the domain — `SKOOP004 dependsOn COOP013` can be perfectly well-formed
and still be pedagogically wrong. The semantic judge catches that:

- **Phase 1 — screen.** Every asserted edge is judged in isolation,
  batched by relation type, against a plain-English definition of what
  that relation is supposed to mean (`configs/*.json` → `relation_semantics`).
  High recall, cheap: over-flagging here is fine.
- **Phase 2 — re-verify.** Every phase-1 flag gets re-checked against the
  local 1–2 hop neighborhood of both endpoints. Anything resolved by
  context is dropped; anything that survives gets a full write-up —
  exact evidence, the argument, and one concrete proposed fix — for a
  human to accept or reject. **Nothing is ever auto-applied to the
  ontology file.**
- **Human decisions persist.** Accepting or dismissing a finding in the
  web UI writes it to `lib/review_store.py`'s per-namespace store, so a
  re-run doesn't re-flag (and re-argue) something already reviewed.
- **Two backends**, same interface (`lib/llm_providers.py`): local Ollama
  (free, needs a model pulled — default `gemma4:26b`) or hosted NVIDIA
  NIM (needs `NVIDIA_API_KEY` in the environment — never passed via the
  UI or written to disk).
- **Caching + parallelism.** An on-disk verdict cache
  (`results/.semantic_cache.json`) means reruns only pay for claims that
  actually changed; LLM calls run in a thread pool (`SEMANTIC_WORKERS`
  env var, default 6).
- **Async in the web UI.** Layers 1–7 return in seconds and render
  immediately; the semantic judge runs afterward in a background thread
  and streams findings in as they're ready.

## Layout

```text
validate.py            CLI entry point
lib/                    all validation layers + the semantic judge
  graph_utils.py          parsing, namespace detection
  structural.py           layer 2
  reasoner.py              layer 3 (HermiT)
  oops_client.py           layer 4 (live OOPS! API)
  oops_catalogue.py        P01-P41 severity/description table
  metrics.py               layer 5 (OntoQA)
  sparql_cq.py             layer 6
  skill_graph.py           layer 7
  prompts.py               layer 8 — phase 1 / phase 2 prompts
  llm_providers.py         layer 8 — Ollama / NVIDIA NIM backends
  llm_judge.py             layer 8 — orchestration, cache, parallelism
  review_store.py          persists human accept/dismiss decisions
  report.py                shared HTML/JSON report rendering
  graph_view.py            interactive graph view (flagged edges highlighted)
  source_view.py           raw-source view (flagged lines highlighted)
webapp/                 Flask UI (upload -> dashboard / graph / source views)
configs/                per-ontology namespace + CQs + skill_graph + relation_semantics
data/AA/                reference COMP101 OOP/ICS ontologies (kept in sync with poster/final/)
examples/               sample OWL modules to try the tool on
results/                generated reports (gitignored)
```

## Adding a new ontology

1. Drop your `.owl`/`.ttl` file anywhere and point the CLI/UI at it —
   structural/consistency/OOPS!/OntoQA all run with zero config.
2. For SPARQL CQs and skill-graph checks, add `configs/your_module.json`
   with `namespace`, `competency_questions`, and/or `skill_graph`. The
   namespace is auto-matched — no CLI flag needed.
3. For the semantic judge to have a real definition of your relations
   (rather than a generic fallback), add a `relation_semantics` block to
   the same config.
