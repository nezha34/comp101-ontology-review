"""Local web UI: drag-drop validate one OWL, or compare two (FastAPI).

Restored from the pre-131f795 UI and wired to the current lib/ + validate.py
pipeline (including the semantic judge prompts).

Run:
  python -m web.app
  # → http://127.0.0.1:8765
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import validate  # noqa: E402
from lib.graph_utils import detect_namespace, load_graph  # noqa: E402
from lib.ontology_drift import compute_tbox_drift  # noqa: E402
from lib.report import render_html, summarize, write_html, write_json  # noqa: E402
from lib.review_store import record_decision as remember_review  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOADS_DIR = ROOT / "uploads"
RESULTS_DIR = ROOT / "results"
CHANGES_DIR = ROOT / "results" / "changes_to_make"
ONTOLOGY_EXTS = validate.ONTOLOGY_EXTS

# In-memory per-run state, keyed by run_id (the upload's timestamp stamp).
# Fast layers (1-6) run synchronously and get shown right away; if the
# semantic judge was requested it runs afterward in the background and
# patches this dict in place, same split CLI/validate_fast + webapp already
# use -- /api/validate/status/<run_id> is how the frontend polls for it.
_RUNS: dict[str, dict] = {}

# Per-run accept/dismiss button state, {run_id: {str(issue_idx): "accepted"|"dismissed"}}.
# Same spirit as webapp/app.py's decisions.json but in-memory since runs here
# already live in _RUNS rather than a per-run directory on disk.
_DECISIONS: dict[str, dict] = {}

app = FastAPI(title="COMP101 Ontology Review", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _save_upload(upload: UploadFile, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = Path(upload.filename or "upload.owl").name.replace(" ", "_")
    if Path(safe).suffix.lower() not in ONTOLOGY_EXTS:
        safe = f"{Path(safe).stem or 'upload'}.owl"
    path = dest_dir / safe
    path.write_bytes(upload.file.read())
    return path


def _flag(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def strip_runtime(result: dict) -> dict:
    return {k: v for k, v in result.items() if not k.startswith("_")}


def _changes_file(result: dict) -> Path:
    from werkzeug.utils import secure_filename  # already a dependency (flask/webapp)

    slug = secure_filename(result.get("name") or "ontology") or "ontology"
    return CHANGES_DIR / f"{slug}.json"


def _load_changes(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _render_drift_section(drift: dict, title: str = "T-Box vocabulary drift") -> str:
    if not drift or not drift.get("ok"):
        err = escape((drift or {}).get("error") or "unavailable")
        return f"<h2>{escape(title)}</h2><p class='warn'>{err}</p>"
    s = drift.get("summary") or {}
    lines = [
        f"<h2>{escape(title)}</h2>",
        "<ul>",
        f"<li>Classes +{s.get('classes_added', 0)} / −{s.get('classes_removed', 0)}</li>",
        f"<li>Object properties +{s.get('properties_added', 0)} / −{s.get('properties_removed', 0)}</li>",
        "</ul>",
    ]

    def block(label: str, items: list, kind: str) -> str:
        if not items:
            return ""
        rows = []
        for e in items:
            comment = escape(e.get("comment") or "(no rdfs:comment)")
            if kind == "prop":
                shape = (
                    f" domain=[{escape(', '.join(e.get('domain') or []))}]"
                    f" range=[{escape(', '.join(e.get('range') or []))}]"
                )
            else:
                shape = ""
            rows.append(f"<li><code>{escape(e['id'])}</code>{shape}: {comment}</li>")
        return f"<h3>{escape(label)} ({len(items)})</h3><ul>{''.join(rows)}</ul>"

    lines.append(block("Only in candidate (A)", drift.get("classes_added") or [], "class"))
    lines.append(block("Only in candidate (A) — properties",
                       drift.get("properties_added") or [], "prop"))
    lines.append(block("Only in baseline (B)", drift.get("classes_removed") or [], "class"))
    lines.append(block("Only in baseline (B) — properties",
                       drift.get("properties_removed") or [], "prop"))
    return "\n".join(lines)


def _render_file_identity(label: str, info: dict) -> str:
    if info.get("parse_error"):
        return (
            f"<h3>{escape(label)}: {escape(info.get('name', ''))}</h3>"
            f"<p class='bad'>Parse error: {escape(info['parse_error'])}</p>"
        )
    return (
        f"<h3>{escape(label)}: {escape(info.get('name', ''))}</h3>"
        f"<p class='meta'>Namespace: <code>{escape(info.get('namespace') or '—')}</code> &middot; "
        f"Triples: {info.get('triple_count', 0)}</p>"
    )


def render_compare_html(payload: dict) -> str:
    left = payload.get("left") or {}
    right = payload.get("right") or {}
    drift = payload.get("pairwise_vocab") or {}
    body = (
        f"<h1>Compare: {escape(left.get('name', 'A'))} vs {escape(right.get('name', 'B'))}</h1>"
        f"<p class='meta'>{escape(payload.get('timestamp') or '')}</p>"
        + _render_file_identity("A", left)
        + _render_file_identity("B", right)
        + _render_drift_section(drift, "T-Box vocabulary drift (A vs B)")
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>Compare report</title><style>{__import__('lib.report', fromlist=['REPORT_CSS']).REPORT_CSS}</style>"
        f"</head><body>{body}</body></html>"
    )


def write_compare_html(payload: dict, out_dir: Path, stamp: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"compare_report_{stamp}.html"
    path.write_text(render_compare_html(payload), encoding="utf-8")
    return path


def _run_validate_fast(path: Path, *, use_oops: bool, use_reasoner: bool) -> dict:
    """Layers 1-6 only -- seconds, not minutes. `result["semantic"]` comes
    back as a "pending" placeholder; the semantic judge (if requested) is
    run separately, in the background, so the fast result can be shown
    immediately instead of blocking the whole request on the LLM."""
    return validate.validate_fast(
        path,
        None,  # always auto-match the config by namespace
        use_oops,
        use_reasoner,
        "",
    )


def _run_semantic_and_patch(run_id: str, path: Path, *, semantic_provider: str, semantic_model: str) -> dict:
    """Runs just the semantic judge (re-parsing the graph fresh — rdflib
    Graphs aren't guaranteed thread-safe to share) and patches it into the
    already-stored fast result for this run.

    Never lets an exception (e.g. Ollama restarting mid-request, a dropped
    connection) leave the run stuck in "pending" forever -- the poller has
    no other way to learn the run died, so a crash here must still produce
    a terminal (non-"pending") semantic state."""
    try:
        semantic = validate.run_semantic_only(path, None, semantic_model, semantic_provider)
    except Exception as exc:  # noqa: BLE001
        semantic = {
            "ok": False, "error": f"Semantic judge crashed: {exc}",
            "model": semantic_model, "provider": semantic_provider,
            "phase1_total_claims": 0, "phase1_flagged": 0,
            "phase2_resolved": [], "issues": [],
        }
    result = _RUNS[run_id]
    result["semantic"] = semantic
    result["summary"] = summarize(result)
    _RUNS[run_id] = result
    return result


def _describe_for_compare(path: Path) -> tuple[dict, object | None]:
    """Parse-only identity for one side of a compare — no validation pipeline.

    Full per-file validation already lives in Validate mode; Compare only
    needs the graphs themselves to diff the T-Box.
    """
    g, err = load_graph(path)
    if err or g is None:
        return {"name": path.name, "source_path": str(path), "parse_error": err or "failed to parse"}, None
    return {
        "name": path.name,
        "source_path": str(path),
        "namespace": detect_namespace(g),
        "triple_count": len(g),
        "parse_error": None,
    }, g


def _run_compare(path_a: Path, path_b: Path) -> dict:
    left, ga = _describe_for_compare(path_a)
    right, gb = _describe_for_compare(path_b)
    pairwise = None
    if ga is not None and gb is not None:
        pairwise = compute_tbox_drift(ga, gb)
        pairwise["baseline_path"] = str(path_b)
        pairwise["candidate_path"] = str(path_a)
    return {
        "mode": "compare",
        "left": left,
        "right": right,
        "pairwise_vocab": pairwise,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


async def _run_semantic_background_task(
    run_id: str, path: Path, stamp: str, *, semantic_provider: str, semantic_model: str
) -> None:
    result = await asyncio.to_thread(
        _run_semantic_and_patch, run_id, path,
        semantic_provider=semantic_provider, semantic_model=semantic_model,
    )
    write_html([result], RESULTS_DIR, stamp)
    write_json([result], RESULTS_DIR, stamp)


@app.post("/api/validate")
async def api_validate(
    file: UploadFile = File(...),
    use_oops: str = Form("0"),
    use_reasoner: str = Form("0"),
    use_semantic: str = Form("0"),
    semantic_provider: str = Form("ollama"),
    semantic_model: str = Form("gemma4:26b"),
):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work = UPLOADS_DIR / f"validate_{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        path = _save_upload(file, work)
        sem = _flag(use_semantic)
        provider = semantic_provider.strip() or "ollama"
        model = semantic_model.strip() or "gemma4:26b"

        def _run():
            raw = _run_validate_fast(path, use_oops=_flag(use_oops), use_reasoner=_flag(use_reasoner))
            result = strip_runtime(raw)
            results = [result]
            html_path = write_html(results, RESULTS_DIR, stamp)
            json_path = write_json(results, RESULTS_DIR, stamp)
            return result, results, html_path, json_path

        result, results, html_path, json_path = await asyncio.to_thread(_run)
        run_id = stamp
        _RUNS[run_id] = result

        semantic_pending = sem and not result.get("parse_error")
        if semantic_pending:
            asyncio.create_task(
                _run_semantic_background_task(
                    run_id, path, stamp, semantic_provider=provider, semantic_model=model,
                )
            )

        return JSONResponse(
            {
                "ok": True,
                "mode": "validate",
                "run_id": run_id,
                "semantic_pending": semantic_pending,
                "html": render_html(results, run_id=run_id, decisions=_DECISIONS.get(run_id, {})),
                "result": result,
                "report_html": str(html_path),
                "report_json": str(json_path),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/validate/status/{run_id}")
def api_validate_status(run_id: str):
    result = _RUNS.get(run_id)
    if result is None:
        return JSONResponse({"ok": False, "error": "unknown run_id"}, status_code=404)
    semantic = result.get("semantic") or {}
    ready = semantic.get("error") != "pending"
    return JSONResponse(
        {
            "ok": True,
            "ready": ready,
            "html": render_html([result], run_id=run_id, decisions=_DECISIONS.get(run_id, {})) if ready else None,
            "result": result if ready else None,
        }
    )


@app.post("/decision/{run_id}/{idx}")
def record_decision(run_id: str, idx: int, action: str = Body(..., embed=True)):
    """Accept or dismiss one semantic-judge finding for a run — same
    contract as webapp/app.py's route of the same name/path (the review
    controls' JS baked into lib/report.py's REPORT_CSS/REVIEW_SCRIPT is
    shared by both UIs and posts to this exact path).

    Accept: appends the finding to results/changes_to_make/<ontology>.json
    (a human-reviewable queue of proposed edits — nothing here touches the
    ontology file itself). Dismiss: just records disagreement. Either way,
    also persists to the namespace-scoped review store so a future run
    (this UI, the CLI, or a coworker's) skips re-judging this exact triple."""
    result = _RUNS.get(run_id)
    if result is None:
        return JSONResponse({"ok": False, "error": f"No run '{run_id}'"}, status_code=404)
    if action not in ("accept", "dismiss"):
        return JSONResponse({"ok": False, "error": "action must be 'accept' or 'dismiss'"}, status_code=400)

    issues = (result.get("semantic") or {}).get("issues", [])
    if idx < 0 or idx >= len(issues):
        return JSONResponse(
            {"ok": False, "error": f"No semantic issue at index {idx} for run '{run_id}'"}, status_code=404
        )
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
    CHANGES_DIR.mkdir(parents=True, exist_ok=True)
    changes_path.write_text(json.dumps(changes, indent=2, ensure_ascii=False))

    _DECISIONS.setdefault(run_id, {})[str(idx)] = status

    ns = result.get("namespace")
    if ns and issue.get("subject_uri") and issue.get("object_uri"):
        remember_review(ns, issue["subject_uri"], issue.get("predicate", ""),
                         issue["object_uri"], status, note=issue.get("issue_summary", ""))

    return JSONResponse({"ok": True, "status": status})


@app.post("/api/compare")
async def api_compare(
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work = UPLOADS_DIR / f"compare_{stamp}"
    (work / "a").mkdir(parents=True, exist_ok=True)
    (work / "b").mkdir(parents=True, exist_ok=True)
    try:
        path_a = _save_upload(file_a, work / "a")
        path_b = _save_upload(file_b, work / "b")

        def _run():
            payload = _run_compare(path_a, path_b)
            html = render_compare_html(payload)
            html_path = write_compare_html(payload, RESULTS_DIR, stamp)
            json_path = RESULTS_DIR / f"compare_report_{stamp}.json"
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            json_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            return payload, html, html_path, json_path

        payload, html, html_path, json_path = await asyncio.to_thread(_run)
        return JSONResponse(
            {
                "ok": True,
                "mode": "compare",
                "html": html,
                "result": payload,
                "report_html": str(html_path),
                "report_json": str(json_path),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


def main() -> None:
    import uvicorn

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    CHANGES_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run("web.app:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
