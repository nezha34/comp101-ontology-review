"""
path_order.py — load a module PATH (authored total order) for the semantic
judge so recommendedBefore can be checked against "PATH already decides".
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path


# Predicates whose glosses reference PATH ordering. Judging them without a
# loaded path_file is a config gap — fail loud rather than weak unverifiable.
PATH_ORDER_RELATIONS: frozenset[str] = frozenset({"recommendedBefore"})


def resolve_path_file(
    raw: str | Path | None,
    *,
    toolkit_root: Path | None = None,
) -> Path | None:
    if not raw:
        return None
    p = Path(raw)
    if p.is_file():
        return p.resolve()
    root = toolkit_root or Path(__file__).resolve().parent.parent
    alt = (root / p).resolve()
    if alt.is_file():
        return alt
    return p


def load_path_steps(path: Path | str) -> tuple[list[str] | None, str | None]:
    """Return (steps, error). Expects JSON with a top-level \"steps\" list of ids."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        return None, "PATH JSON missing non-empty 'steps' list"
    out = [str(s).strip() for s in steps if str(s).strip()]
    if not out:
        return None, "PATH JSON 'steps' contained no usable ids"
    return out, None


def path_index(steps: list[str]) -> dict[str, int]:
    return {sid: i for i, sid in enumerate(steps)}


def format_path_excerpt_for_pair(
    steps: list[str],
    subject_id: str,
    object_id: str,
    *,
    window: int = 2,
) -> str:
    """Compact PATH snippet for a recommendedBefore subject/object pair.

    Matching is by PATH step id (== ontology local name). If either endpoint
    is off-PATH, that is stated explicitly so the model can use unverifiable
    rather than invent order.
    """
    idx = path_index(steps)
    si = idx.get(subject_id)
    oi = idx.get(object_id)
    lines = [
        "Authored PATH (tutor spine) order for this module "
        "(earlier index = taught earlier):",
    ]
    if si is None and oi is None:
        lines.append(
            f'  Neither "{subject_id}" nor "{object_id}" appears on PATH. '
            "You cannot use PATH to decide order for this pair."
        )
        return "\n".join(lines)
    if si is None:
        lines.append(f'  "{subject_id}" is NOT on PATH.')
    else:
        lines.append(f'  "{subject_id}" PATH index = {si}')
    if oi is None:
        lines.append(f'  "{object_id}" is NOT on PATH.')
    else:
        lines.append(f'  "{object_id}" PATH index = {oi}')

    if si is not None and oi is not None:
        if si < oi:
            lines.append(
                f'  PATH already teaches "{subject_id}" before "{object_id}" '
                f"(indices {si} → {oi})."
            )
        elif oi < si:
            lines.append(
                f'  PATH already teaches "{object_id}" before "{subject_id}" '
                f"(indices {oi} → {si})."
            )
        else:
            lines.append("  Same PATH index (unexpected) — treat as PATH-undecided.")
        if abs(si - oi) == 1:
            lines.append(
                "  These are consecutive PATH beats — recommendedBefore is "
                "usually unnecessary (tutor already walks this order)."
            )

    # Local window around whichever endpoints are on PATH
    centers = [i for i in (si, oi) if i is not None]
    lo = max(0, min(centers) - window)
    hi = min(len(steps), max(centers) + window + 1)
    lines.append("  Nearby PATH excerpt:")
    for i in range(lo, hi):
        mark = ""
        if i == si:
            mark = "  ← subject"
        elif i == oi:
            mark = "  ← object"
        lines.append(f"    [{i}] {steps[i]}{mark}")
    return "\n".join(lines)


def attach_path_to_config(
    config: dict | None,
    *,
    toolkit_root: Path | None = None,
    path_file_override: str | Path | None = None,
) -> tuple[dict, dict | None]:
    """Load PATH into config['_path_steps'] / ['_path_index'].

    Returns (config_copy, path_meta) where path_meta is None if no path
    configured, or a small report dict (ok/error/path/n_steps).
    """
    cfg = deepcopy(config or {})
    raw = path_file_override or cfg.get("path_file") or cfg.get("semantic_path")
    resolved = resolve_path_file(raw, toolkit_root=toolkit_root)
    if resolved is None:
        return cfg, None

    steps, err = load_path_steps(resolved)
    if err or steps is None:
        meta = {
            "ok": False,
            "error": f"Failed to load PATH {resolved}: {err}",
            "path_file": str(resolved),
            "n_steps": 0,
        }
        cfg["_path_meta"] = meta
        return cfg, meta

    cfg["_path_steps"] = steps
    cfg["_path_index"] = path_index(steps)
    meta = {
        "ok": True,
        "error": None,
        "path_file": str(resolved.resolve()),
        "n_steps": len(steps),
    }
    cfg["_path_meta"] = meta
    return cfg, meta
