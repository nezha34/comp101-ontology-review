#!/usr/bin/env python3.11
"""
app.py — simple Flask front-end for the ontology validation pipeline.

Upload an .owl/.ttl/.rdf file, run the full validate.py pipeline against
it (structural + consistency + OOPS! + OntoQA + CQs/skill-graph if a
config matches + the optional two-phase LLM semantic judge), then browse
the results as: a dashboard, an interactive graph with flagged edges
highlighted, and the raw source with flagged lines highlighted.

Run with:
  python3.11 app.py
then open http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from datetime import datetime
from html import escape
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, request, url_for
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent.parent  # validation/
sys.path.insert(0, str(ROOT))

import validate  # noqa: E402
from lib import graph_view, report, source_view  # noqa: E402
from lib.graph_utils import load_graph  # noqa: E402
from lib.review_store import record_decision as remember_review  # noqa: E402

app = Flask(__name__)

RUNS_DIR = Path(__file__).parent / "runs"
RUNS_DIR.mkdir(exist_ok=True)

# Human review queue for semantic-judge findings: Accept writes the finding
# here as a proposed change; Dismiss just records disagreement (nothing
# queued). Nothing in this app ever edits the ontology file itself — these
# are inputs for a human to apply by hand, same spirit as the semantic
# judge's own output being "proposals, not auto-applied changes".
CHANGES_DIR = Path(__file__).parent.parent / "results" / "changes_to_make"
CHANGES_DIR.mkdir(parents=True, exist_ok=True)

ONTOLOGY_EXTS = validate.ONTOLOGY_EXTS


def _run_dir(run_id: str) -> Path:
    d = RUNS_DIR / run_id
    if not d.is_dir():
        abort(404, f"No run '{run_id}'")
    return d


def _source_file(run_dir: Path) -> Path:
    matches = [f for f in run_dir.iterdir() if f.stem == "source"]
    if not matches:
        abort(404, "Source file missing for this run")
    return matches[0]


def _load_result(run_dir: Path) -> dict:
    return json.loads((run_dir / "result.json").read_text())


def _decisions_path(run_dir: Path) -> Path:
    return run_dir / "decisions.json"


def _load_decisions(run_dir: Path) -> dict:
    p = _decisions_path(run_dir)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _changes_file(result: dict) -> Path:
    slug = secure_filename(result.get("name") or "ontology") or "ontology"
    return CHANGES_DIR / f"{slug}.json"


def _load_changes(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


UPLOAD_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ontology Validator</title>
<style>{css}</style>
</head>
<body>
  <div class="top-bar">
    <div>
      <div class="top-title">Ontology Validator</div>
      <div class="top-meta">Upload an OWL/TTL/RDF file to run the full validation pipeline.</div>
    </div>
  </div>
  <div class="ontology">
    <form method="post" action="{action}" enctype="multipart/form-data">
      <p><input type="file" name="file" accept=".owl,.ttl,.rdf,.n3,.nt,.jsonld" required></p>
      <p><label><input type="checkbox" name="oops" checked> OOPS! pitfall scan (live API, needs internet)</label></p>
      <p><label><input type="checkbox" name="reasoner" checked> OWL DL consistency (HermiT, needs Java)</label></p>
      <p><label><input type="checkbox" name="semantic"> Semantic judge — two-phase LLM review (slow, several minutes)</label></p>
      <div style="margin-left:1.6rem">
        <p>
          <label><input type="radio" name="semantic_provider" value="ollama" checked> Local Ollama</label>
          &nbsp;&nbsp;
          <label><input type="radio" name="semantic_provider" value="nvidia_nim"> NVIDIA NIM (hosted)</label>
        </p>
        <p><label>Ollama model tag: <input type="text" name="ollama_model" value="gemma4:26b" size="24"></label></p>
        <p><label>NIM model id: <input type="text" name="nim_model" value="mistralai/mistral-medium-3.5-128b" size="36"></label>
           <br><span class="meta">Needs <code>NVIDIA_API_KEY</code> set in the environment the Flask app was started from — never entered here.</span></p>
      </div>
      <p><button type="submit" class="nav-link" style="cursor:pointer">Validate</button></p>
    </form>
    {error}
  </div>
</body>
</html>"""


@app.route("/", methods=["GET"])
def index():
    return Response(
        UPLOAD_PAGE.format(css=report.REPORT_CSS, action=url_for("validate_upload"), error=""),
        mimetype="text/html",
    )


@app.route("/validate", methods=["POST"])
def validate_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return Response(
            UPLOAD_PAGE.format(css=report.REPORT_CSS, action=url_for("validate_upload"),
                                error="<p class='bad'>No file selected.</p>"),
            mimetype="text/html",
        )

    filename = secure_filename(file.filename)
    ext = Path(filename).suffix.lower()
    if ext not in ONTOLOGY_EXTS:
        return Response(
            UPLOAD_PAGE.format(css=report.REPORT_CSS, action=url_for("validate_upload"),
                                error=f"<p class='bad'>Unsupported extension '{escape(ext)}'. "
                                      f"Expected one of: {', '.join(sorted(ONTOLOGY_EXTS))}</p>"),
            mimetype="text/html",
        )

    run_id = uuid.uuid4().hex[:12]
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True)
    source_path = run_dir / f"source{ext}"
    file.save(source_path)

    use_oops = request.form.get("oops") == "on"
    use_reasoner = request.form.get("reasoner") == "on"
    use_semantic = request.form.get("semantic") == "on"
    semantic_provider = request.form.get("semantic_provider", "ollama")
    if semantic_provider == "nvidia_nim":
        semantic_model = request.form.get("nim_model", "").strip() or "mistralai/mistral-medium-3.5-128b"
    else:
        semantic_model = request.form.get("ollama_model", "").strip() or "gemma4:26b"

    # Fast checks (structural/consistency/OOPS/OntoQA/CQs/skill-graph) run
    # here and get shown immediately -- they're seconds, not minutes. The
    # semantic judge (slow, LLM-backed) is kicked off in a background thread
    # below instead of blocking this request; result.json's "semantic" key
    # starts as a "pending" placeholder and the results page polls until a
    # background write replaces it with the real thing.
    result = validate.validate_fast(
        source_path, config_override=None, use_oops=use_oops,
        use_reasoner=use_reasoner, oops_pitfalls="",
    )
    result["original_filename"] = filename
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))

    if use_semantic and not result.get("parse_error"):
        threading.Thread(
            target=_run_semantic_in_background,
            args=(run_dir, source_path, semantic_model, semantic_provider),
            daemon=True,
        ).start()

    return redirect(url_for("results_page", run_id=run_id))


def _run_semantic_in_background(run_dir: Path, source_path: Path,
                                 semantic_model: str, semantic_provider: str) -> None:
    """Runs the semantic judge and patches it into an already-written
    result.json once done. Separate thread, separate Graph parse (see
    validate.run_semantic_only's docstring for why) -- the fast result is
    already on disk and visible to the results page before this even starts."""
    semantic = validate.run_semantic_only(source_path, None, semantic_model, semantic_provider)
    result = _load_result(run_dir)
    result["semantic"] = semantic
    result["summary"] = report.summarize(result)
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))


@app.route("/results/<run_id>", methods=["GET"])
def results_page(run_id: str):
    run_dir = _run_dir(run_id)
    result = _load_result(run_dir)
    decisions = _load_decisions(run_dir)

    nav_links = [
        ("Graph view", url_for("graph_page", run_id=run_id)),
        ("Source view", url_for("source_page", run_id=run_id)),
        ("Validate another file", url_for("index")),
    ]
    meta = (
        f"File: <code>{escape(result.get('original_filename', result['source_path']))}</code> &middot; "
        f"Validated {escape(result['timestamp'])}"
    )
    body = report.render_body([result], run_id=run_id, decisions=decisions)
    if (result.get("semantic") or {}).get("error") == "pending":
        body += (
            f"<script>(function poll(){{"
            f"fetch('{url_for('status', run_id=run_id)}').then(r=>r.json()).then(d=>{{"
            f"if(d.semantic_ready) location.reload(); else setTimeout(poll, 4000);"
            f"}}).catch(()=>setTimeout(poll, 4000));"
            f"}})();</script>"
        )
    html = report.render_page(
        f"Validation — {result['name']}", meta, body, nav_links=nav_links,
    )
    return Response(html, mimetype="text/html")


@app.route("/status/<run_id>", methods=["GET"])
def status(run_id: str):
    run_dir = _run_dir(run_id)
    result = _load_result(run_dir)
    ready = (result.get("semantic") or {}).get("error") != "pending"
    return jsonify({"semantic_ready": ready})


@app.route("/decision/<run_id>/<int:idx>", methods=["POST"])
def record_decision(run_id: str, idx: int):
    """Accept or dismiss one semantic-judge finding for a run.

    Accept: appends the finding to results/changes_to_make/<ontology>.json
    (a human-reviewable queue of proposed edits — nothing here touches the
    ontology file itself). Dismiss: just records disagreement. Re-clicking
    toggles cleanly, including un-queuing a change if you accepted then
    changed your mind.
    """
    run_dir = _run_dir(run_id)
    result = _load_result(run_dir)
    action = (request.get_json(silent=True) or {}).get("action")
    if action not in ("accept", "dismiss"):
        abort(400, "action must be 'accept' or 'dismiss'")

    issues = (result.get("semantic") or {}).get("issues", [])
    if idx < 0 or idx >= len(issues):
        abort(404, f"No semantic issue at index {idx} for run '{run_id}'")
    issue = issues[idx]

    changes_path = _changes_file(result)
    changes = _load_changes(changes_path)
    changes = [c for c in changes if not (c["run_id"] == run_id and c["issue_index"] == idx)]

    status = "accepted" if action == "accept" else "dismissed"
    if action == "accept":
        changes.append({
            "run_id": run_id,
            "issue_index": idx,
            "queued_at": datetime.now().isoformat(timespec="seconds"),
            "ontology": result.get("name"),
            "namespace": result.get("namespace"),
            "subject": issue.get("subject"),
            "predicate": issue.get("predicate"),
            "object": issue.get("object"),
            "subject_uri": issue.get("subject_uri"),
            "object_uri": issue.get("object_uri"),
            "issue_summary": issue.get("issue_summary"),
            "proposed_fix_action": issue.get("proposed_fix_action"),
            "proposed_fix_triple": issue.get("proposed_fix_triple"),
            "proposed_fix_rationale": issue.get("proposed_fix_rationale"),
        })
    changes_path.write_text(json.dumps(changes, indent=2, ensure_ascii=False))

    decisions = _load_decisions(run_dir)
    decisions[str(idx)] = status
    _decisions_path(run_dir).write_text(json.dumps(decisions, indent=2))

    # Persist to the namespace-scoped review store so future runs (CLI or
    # webapp, this ontology or a coworker's different one) skip re-judging
    # this exact triple instead of re-flagging a call that's already settled.
    ns = result.get("namespace")
    if ns and issue.get("subject_uri") and issue.get("object_uri"):
        remember_review(ns, issue["subject_uri"], issue.get("predicate", ""),
                         issue["object_uri"], status, note=issue.get("issue_summary", ""))

    return jsonify({"ok": True, "status": status})


@app.route("/graph/<run_id>", methods=["GET"])
def graph_page(run_id: str):
    run_dir = _run_dir(run_id)
    result = _load_result(run_dir)
    g, err = load_graph(_source_file(run_dir))
    if err:
        abort(400, f"Could not reload graph: {err}")
    return Response(graph_view.build_graph_html(g, result, result["name"]), mimetype="text/html")


@app.route("/source/<run_id>", methods=["GET"])
def source_page(run_id: str):
    run_dir = _run_dir(run_id)
    result = _load_result(run_dir)
    src = _source_file(run_dir)
    raw_text = src.read_text(encoding="utf-8", errors="replace")
    filename = result.get("original_filename", src.name)
    return Response(source_view.build_source_html(raw_text, filename, result), mimetype="text/html")


if __name__ == "__main__":
    print(f"Runs stored under: {RUNS_DIR}")
    app.run(debug=True, port=5000, threaded=True)
