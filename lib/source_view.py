"""
source_view.py — renders the raw uploaded OWL/TTL source as text with the
lines implicated in a semantic-judge issue highlighted.

This is a heuristic, not a real RDF/XML or Turtle parser mapped back to
byte offsets: a line is considered a match for a triple if it contains
both the predicate's local name and either endpoint's local name. That
covers the common case (RDF/XML's `<ns:predicate rdf:resource="...Local"/>`
and Turtle's `:subj :pred :obj` on one line) without a full serializer-
aware line-to-triple mapper. Good enough to point a human at "roughly
here"; not a guarantee of the single exact line in every serialization.
"""

from __future__ import annotations

from html import escape


def _local(uri_str: str) -> str:
    return uri_str.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def build_source_html(raw_text: str, filename: str, result: dict) -> str:
    lines = raw_text.splitlines()
    semantic = result.get("semantic") or {}
    highlights: dict[int, dict] = {}

    def mark(records: list[dict], color: str, kind_label: str):
        for rec in records:
            pred = rec["predicate"]
            obj_local = _local(rec["object_uri"])
            subj_local = _local(rec["subject_uri"])
            tooltip = (
                f'{rec["subject"]} --{pred}--> {rec["object"]}: '
                f'{rec.get("issue_summary") or rec.get("resolution_explanation", "")}'
            )
            for i, line in enumerate(lines):
                if pred in line and (obj_local in line or subj_local in line):
                    highlights.setdefault(i, {"color": color, "label": kind_label, "tooltip": tooltip})

    # Mark resolved first so confirmed issues (marked second) win on overlap.
    mark(semantic.get("phase2_resolved", []), "#d29922", "resolved by context")
    mark(semantic.get("issues", []), "#f85149", "confirmed issue")

    rendered = []
    for i, line in enumerate(lines):
        text = escape(line) if line.strip() else "&nbsp;"
        hl = highlights.get(i)
        if hl:
            rendered.append(
                f'<div class="line hl" style="border-left-color:{hl["color"]};background:{hl["color"]}1a" '
                f'title="{escape(hl["tooltip"])}"><span class="ln">{i+1}</span>{text}</div>'
            )
        else:
            rendered.append(f'<div class="line"><span class="ln">{i+1}</span>{text}</div>')

    n_issue = sum(1 for h in highlights.values() if h["label"] == "confirmed issue")
    n_resolved = sum(1 for h in highlights.values() if h["label"] == "resolved by context")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(filename)} — Source</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: ui-monospace, SFMono-Regular, "Courier New", monospace; margin: 0; background: Canvas; color: CanvasText; }}
  .topbar {{ position: sticky; top: 0; background: Canvas; border-bottom: 1px solid rgba(128,128,128,.3);
             padding: .6rem 1rem; font-family: -apple-system, sans-serif; font-size: .85rem; }}
  .topbar a {{ margin-right: 1rem; }}
  .legend span {{ display: inline-flex; align-items: center; gap: .3rem; margin-right: 1rem; }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
  pre.wrap {{ margin: 0; padding: 0 0 2rem; }}
  .line {{ white-space: pre; padding: 0 .6rem; border-left: 3px solid transparent; font-size: .82rem; }}
  .line.hl {{ cursor: help; }}
  .ln {{ display: inline-block; width: 3.5rem; color: gray; user-select: none; }}
</style>
</head>
<body>
  <div class="topbar">
    <a href="javascript:history.back()">&larr; back</a>
    <b>{escape(filename)}</b>
    <div class="legend" style="margin-top:.3rem">
      <span><span class="dot" style="background:#f85149"></span>{n_issue} confirmed issue line(s)</span>
      <span><span class="dot" style="background:#d29922"></span>{n_resolved} resolved-by-context line(s)</span>
      <span style="color:gray">(heuristic line matching — hover a highlighted line for detail)</span>
    </div>
  </div>
  <pre class="wrap">{''.join(rendered)}</pre>
</body>
</html>"""
