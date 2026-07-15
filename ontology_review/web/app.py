"""Local web UI: drag-drop validate one OWL, or compare two."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ontology_review.lib.report import (
    render_compare_html,
    render_html,
    write_compare_html,
    write_html,
    write_json,
)
from ontology_review.pipeline import (
    DEFAULT_BASELINE,
    DEFAULT_RESULTS,
    ONTOLOGY_EXTS,
    compare_two,
    load_all_configs,
    strip_runtime,
    validate_one,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOADS_DIR = DEFAULT_RESULTS.parent / "uploads"

app = FastAPI(title="COMP101 Ontology Review", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _save_upload(upload: UploadFile, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = Path(upload.filename or "upload.owl").name.replace(" ", "_")
    if Path(safe).suffix.lower() not in ONTOLOGY_EXTS:
        safe = f"{Path(safe).stem or 'upload'}.owl"
    path = dest_dir / safe
    # UploadFile.file is a SpooledTemporaryFile; read all bytes once.
    data = upload.file.read()
    path.write_bytes(data)
    return path


def _flag(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/configs")
def api_configs():
    packs = []
    for cfg in load_all_configs():
        packs.append(
            {
                "id": cfg["_id"],
                "name": cfg.get("name"),
                "namespace": cfg.get("namespace"),
                "match": cfg.get("match"),
                "cq_count": len(cfg.get("competency_questions") or []),
            }
        )
    return JSONResponse({"ok": True, "configs": packs})


@app.post("/api/validate")
async def api_validate(
    file: UploadFile = File(...),
    use_oops: str = Form("0"),
    use_reasoner: str = Form("0"),
    config_id: str = Form("auto"),
):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    work = UPLOADS_DIR / f"validate_{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        path = _save_upload(file, work)
        raw = validate_one(
            path,
            config_id=config_id,
            use_oops=_flag(use_oops),
            use_reasoner=_flag(use_reasoner),
            baseline=DEFAULT_BASELINE,
            quiet=True,
        )
        result = strip_runtime(raw)
        results = [result]
        html_path = write_html(results, DEFAULT_RESULTS, stamp)
        json_path = write_json(results, DEFAULT_RESULTS, stamp)
        return JSONResponse(
            {
                "ok": True,
                "mode": "validate",
                "html": render_html(results),
                "result": result,
                "report_html": str(html_path),
                "report_json": str(json_path),
            }
        )
    except Exception as exc:  # noqa: BLE001 — surface to UI
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/compare")
async def api_compare(
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
    use_oops: str = Form("0"),
    use_reasoner: str = Form("0"),
    config_id: str = Form("auto"),
):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    work = UPLOADS_DIR / f"compare_{stamp}"
    (work / "a").mkdir(parents=True, exist_ok=True)
    (work / "b").mkdir(parents=True, exist_ok=True)
    try:
        path_a = _save_upload(file_a, work / "a")
        path_b = _save_upload(file_b, work / "b")
        payload = compare_two(
            path_a,
            path_b,
            config_id=config_id,
            use_oops=_flag(use_oops),
            use_reasoner=_flag(use_reasoner),
            baseline=DEFAULT_BASELINE,
            quiet=True,
        )
        html = render_compare_html(payload)
        html_path = write_compare_html(payload, DEFAULT_RESULTS, stamp)
        json_path = DEFAULT_RESULTS / f"compare_report_{stamp}.json"
        DEFAULT_RESULTS.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
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
    DEFAULT_RESULTS.mkdir(parents=True, exist_ok=True)
    uvicorn.run(
        "ontology_review.web.app:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
    )


if __name__ == "__main__":
    main()
