"""
report.py — turns a list of per-ontology result dicts into a JSON file and a
single self-contained HTML dashboard (no external CSS/JS/CDN dependencies).
"""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path

SEVERITY_ORDER = {"Critical": 0, "Important": 1, "Minor": 2, "Unknown": 3}
SEVERITY_COLOR = {
    "Critical": "var(--bad)",
    "Important": "var(--warn)",
    "Minor": "var(--muted)",
    "Unknown": "var(--muted)",
}


def overall_verdict(result: dict) -> tuple[str, str]:
    """Returns (verdict_label, css-color-var) for the top-level badge."""
    if result.get("parse_error"):
        return "FAIL — could not parse", "var(--bad)"

    s = result["summary"]
    if s["structural_issues"] > 0 or s["oops_critical"] > 0 or result["consistency"].get("consistent") is False:
        return "FAIL", "var(--bad)"
    if (s["structural_warnings"] > 0 or s["oops_important"] > 0 or s["oops_minor"] > 0
            or s.get("cq_failed", 0) > 0 or s["skill_issues"] > 0
            or result["consistency"].get("consistent") is None):
        return "PASS WITH WARNINGS", "var(--warn)"
    return "PASS", "var(--ok)"


def summarize(result: dict) -> dict:
    structural = result["structural"]
    oops = result["oops"]
    cq = result.get("sparql_cqs", [])
    skill = result.get("skill_graph")

    pitfalls = oops.get("pitfalls", [])
    return {
        "structural_issues": len(structural["issues"]),
        "structural_warnings": len(structural["warnings"]),
        "oops_critical": sum(1 for p in pitfalls if p["severity"] == "Critical"),
        "oops_important": sum(1 for p in pitfalls if p["severity"] == "Important"),
        "oops_minor": sum(1 for p in pitfalls if p["severity"] == "Minor"),
        "cq_passed": sum(1 for c in cq if c.get("passed")),
        "cq_failed": sum(1 for c in cq if not c.get("passed")),
        "cq_total": len(cq),
        "skill_issues": len(skill["issues"]) if skill else 0,
    }


def write_json(results: list[dict], out_dir: Path, stamp: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"validation_report_{stamp}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    return path


def _card(label: str, value, color: str = "") -> str:
    style = f'style="color:{color}"' if color else ""
    return (
        f'<div class="card"><div class="card-value" {style}>{escape(str(value))}</div>'
        f'<div class="card-label">{escape(label)}</div></div>'
    )


def _section(title: str, body: str) -> str:
    return f'<section><h2>{escape(title)}</h2>{body}</section>'


def _pills(items: list[str]) -> str:
    if not items:
        return "<p class='ok'>None</p>"
    return (
        '<div class="pills">'
        + "".join(f"<code class='pill'>{escape(i)}</code>" for i in items)
        + "</div>"
    )


def _render_one(result: dict) -> str:
    name = result["name"]
    verdict, color = overall_verdict(result)
    s = result["summary"]

    parts = [f'<div class="ontology">']
    parts.append(
        f'<div class="onto-header"><h1>{escape(name)}</h1>'
        f'<span class="badge" style="background:{color}">{escape(verdict)}</span></div>'
    )
    parts.append(
        f'<div class="meta">Source: <code>{escape(result["source_path"])}</code> &middot; '
        f'Namespace: <code>{escape(result.get("namespace") or "—")}</code> &middot; '
        f'Triples: {result.get("triple_count", 0)} &middot; '
        f'CQ pack: <code>{escape(str(result.get("config_name") or "—"))}</code>'
        f' ({escape(str(result.get("config_match") or "—"))}) &middot; '
        f'Validated: {escape(result["timestamp"])}</div>'
    )

    if result.get("parse_error"):
        parts.append(f'<div class="error-box">Parse error: {escape(result["parse_error"])}</div>')
        parts.append("</div>")
        return "".join(parts)

    cards = "".join([
        _card("Structural issues", s["structural_issues"], "var(--bad)" if s["structural_issues"] else "var(--ok)"),
        _card("Structural warnings", s["structural_warnings"], "var(--warn)" if s["structural_warnings"] else "var(--ok)"),
        _card("OOPS critical", s["oops_critical"], "var(--bad)" if s["oops_critical"] else "var(--ok)"),
        _card("OOPS important/minor", f'{s["oops_important"]}/{s["oops_minor"]}'),
        _card("Consistency",
              {"True": "consistent", "False": "INCONSISTENT", "None": "unknown"}[str(result["consistency"].get("consistent"))],
              "var(--ok)" if result["consistency"].get("consistent") else ("var(--bad)" if result["consistency"].get("consistent") is False else "var(--muted)")),
        _card("Competency Qs", f'{s["cq_passed"]}/{s["cq_total"]}' if s["cq_total"] else "n/a"),
    ])
    parts.append(f'<div class="cards">{cards}</div>')

    # Structural detail
    st = result["structural"]
    body = ""
    if st["issues"]:
        body += "<h3>Issues</h3><ul>" + "".join(f"<li>{escape(i)}</li>" for i in st["issues"]) + "</ul>"
    if st["warnings"]:
        body += "<h3>Warnings</h3><ul>" + "".join(f"<li>{escape(w)}</li>" for w in st["warnings"]) + "</ul>"
    if not body:
        body = "<p class='ok'>No structural issues found.</p>"
    parts.append(_section("Structural Integrity", body))

    # Consistency detail
    cons = result["consistency"]
    if cons.get("consistent") is True:
        cbody = "<p class='ok'>Ontology is logically consistent (HermiT).</p>"
    elif cons.get("consistent") is False:
        cbody = f"<p class='bad'>{escape(cons.get('error') or 'Ontology is inconsistent.')}</p>"
        if cons.get("unsatisfiable_classes"):
            cbody += "<ul>" + "".join(f"<li>{escape(c)}</li>" for c in cons["unsatisfiable_classes"]) + "</ul>"
    else:
        cbody = f"<p class='warn'>Reasoner did not run: {escape(cons.get('error') or 'unknown reason')}</p>"
    parts.append(_section("OWL DL Consistency (HermiT)", cbody))

    # OOPS
    oops = result["oops"]
    if not oops.get("ok"):
        obody = f"<p class='warn'>OOPS! API unavailable: {escape(oops.get('error') or 'unknown error')}</p>"
    else:
        pitfalls = sorted(oops["pitfalls"], key=lambda p: SEVERITY_ORDER.get(p["severity"], 9))
        if not pitfalls:
            obody = "<p class='ok'>No pitfalls detected.</p>"
        else:
            rows = ""
            for p in pitfalls:
                sev_color = SEVERITY_COLOR.get(p["severity"], "var(--muted)")
                affected = ", ".join(p["affected_elements"][:8])
                more = f" (+{len(p['affected_elements'])-8} more)" if len(p["affected_elements"]) > 8 else ""
                rows += (
                    f'<tr><td><span class="sev" style="background:{sev_color}">{escape(p["severity"])}</span></td>'
                    f'<td><b>{escape(p["code"])}</b> {escape(p["name"])}</td>'
                    f'<td>{escape(p["description"])}</td>'
                    f'<td>{escape(affected)}{escape(more)}</td></tr>'
                )
            obody = (
                '<table><thead><tr><th>Severity</th><th>Pitfall</th>'
                '<th>Description</th><th>Affected elements</th></tr></thead>'
                f'<tbody>{rows}</tbody></table>'
            )
        if oops.get("suggestions"):
            obody += "<h3>Suggestions</h3><ul>" + "".join(
                f"<li>{escape(sg['name'])}: {escape(sg.get('description',''))}</li>" for sg in oops["suggestions"]
            ) + "</ul>"
    parts.append(_section("OOPS! Pitfall Scan (live API)", obody))

    # OntoQA metrics
    m = result["ontoqa"]
    sch, inst = m["schema"], m["instance"]
    mbody = (
        '<table class="metrics"><tbody>'
        f'<tr><td>Classes</td><td>{sch["class_count"]}</td>'
        f'<td>Object Properties</td><td>{sch["object_property_count"]}</td></tr>'
        f'<tr><td>Datatype Properties</td><td>{sch["datatype_property_count"]}</td>'
        f'<td>Subclass Edges</td><td>{sch["subclass_edges"]}</td></tr>'
        f'<tr><td>Relationship Richness</td><td>{sch["relationship_richness"]}</td>'
        f'<td>Attribute Richness</td><td>{sch["attribute_richness"]}</td></tr>'
        f'<tr><td>Inheritance Richness</td><td>{sch["inheritance_richness"]}</td>'
        f'<td>Max Depth</td><td>{sch["max_inheritance_depth"]}</td></tr>'
        f'<tr><td>Individuals</td><td>{inst["individual_count"]}</td>'
        f'<td>Class Richness</td><td>{inst["class_richness"]}</td></tr>'
        f'<tr><td>Average Population</td><td>{inst["average_population"]}</td>'
        f'<td>Classes w/ Instances</td><td>{inst["classes_with_instances"]}</td></tr>'
        '</tbody></table>'
    )
    parts.append(_section("OntoQA Metrics", mbody))

    # SPARQL CQs
    cqs = result.get("sparql_cqs")
    if cqs:
        rows = ""
        for cq in cqs:
            ok = cq.get("passed")
            rows += (
                f'<tr><td>{"✓" if ok else "✗"}</td><td>{escape(cq["id"])}</td>'
                f'<td>{escape(cq["question"])}</td>'
                f'<td>{cq.get("results", "ERR")}/{cq.get("expected_min","")}</td>'
                f'<td>{escape(", ".join(cq.get("sample", [])))}</td></tr>'
            )
        cqbody = (
            '<table><thead><tr><th></th><th>ID</th><th>Question</th><th>Results/Min</th><th>Sample</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
        )
        parts.append(_section("SPARQL Competency Questions", cqbody))

    # Skill graph
    sg = result.get("skill_graph")
    if sg:
        sgbody = ""
        if sg["issues"]:
            sgbody += "<ul>" + "".join(f"<li class='bad'>{escape(i)}</li>" for i in sg["issues"]) + "</ul>"
        else:
            sgbody += "<p class='ok'>DAG is well-formed.</p>"
        n_root = sum(1 for sk in sg["skills"] if sk["is_root"])
        sgbody += f"<p>{len(sg['skills'])} skills total, {n_root} root(s).</p>"
        parts.append(_section("Skill Graph", sgbody))

    # Vocab diff vs shared T-Box
    vd = result.get("vocab_diff") or {}
    if vd.get("error"):
        parts.append(_section("Vocabulary Diff (shared T-Box)", f"<p class='warn'>{escape(vd['error'])}</p>"))
    elif vd:
        vbody = (
            f"<p>Baseline <code>{escape(str(vd.get('baseline_id')))}</code> "
            f"v{escape(str(vd.get('baseline_version')))} vs "
            f"<code>{escape(str(vd.get('other_id')))}</code></p>"
            f"<h3>Classes only in this module ({len(vd.get('classes_only_in_other') or [])})</h3>"
            + _pills(vd.get("classes_only_in_other") or [])
            + f"<h3>Object properties only in this module ({len(vd.get('object_properties_only_in_other') or [])})</h3>"
            + _pills(vd.get("object_properties_only_in_other") or [])
            + f"<h3>Edge types only in this module ({len(vd.get('edge_types_only_in_other') or [])})</h3>"
            + _pills(vd.get("edge_types_only_in_other") or [])
            + f"<h3>Classes in baseline missing here ({len(vd.get('classes_only_in_baseline') or [])})</h3>"
            + _pills(vd.get("classes_only_in_baseline") or [])
        )
        parts.append(_section("Vocabulary Diff (shared T-Box)", vbody))

    parts.append("</div>")
    return "".join(parts)


def render_html(results: list[dict]) -> str:
    body = "".join(_render_one(r) for r in results)
    generated = datetime.now().isoformat(timespec="seconds")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ontology Validation Report</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #ffffff; --fg: #1f2328; --muted: #57606a; --border: rgba(31,35,40,.15);
    --card-bg: #f6f8fa; --code-bg: rgba(175,184,193,.25);
    --ok: #1a7f37; --bad: #d1242f; --warn: #9a6700; --error-bg: rgba(209,36,47,.1);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --border: rgba(240,246,252,.18);
      --card-bg: #161b22; --code-bg: rgba(110,118,129,.35);
      --ok: #3fb950; --bad: #f85149; --warn: #d29922; --error-bg: rgba(248,81,73,.12);
    }}
  }}
  :root[data-theme="dark"] {{
    --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --border: rgba(240,246,252,.18);
    --card-bg: #161b22; --code-bg: rgba(110,118,129,.35);
    --ok: #3fb950; --bad: #f85149; --warn: #d29922; --error-bg: rgba(248,81,73,.12);
  }}
  :root[data-theme="light"] {{
    --bg: #ffffff; --fg: #1f2328; --muted: #57606a; --border: rgba(31,35,40,.15);
    --card-bg: #f6f8fa; --code-bg: rgba(175,184,193,.25);
    --ok: #1a7f37; --bad: #d1242f; --warn: #9a6700; --error-bg: rgba(209,36,47,.1);
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
          max-width: 1100px; margin: 2rem auto; padding: 0 1.5rem; line-height: 1.5;
          background: var(--bg); color: var(--fg); }}
  h1 {{ font-size: 1.5rem; margin: 0; }}
  h2 {{ font-size: 1.1rem; border-bottom: 1px solid var(--border); padding-bottom: .3rem; margin-top: 2rem; }}
  h3 {{ font-size: .95rem; margin-bottom: .3rem; }}
  .top-bar {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; }}
  .top-title {{ font-size: 1.6rem; font-weight: 600; margin-bottom: .2rem; }}
  .top-meta {{ color: var(--muted); margin-bottom: 1.5rem; }}
  #theme-toggle {{ border: 1px solid var(--border); background: var(--card-bg); color: var(--fg);
                    border-radius: 8px; padding: .4rem .7rem; font-size: .9rem; cursor: pointer; flex-shrink: 0; }}
  #theme-toggle:hover {{ opacity: .8; }}
  .ontology {{ border: 1px solid var(--border); border-radius: 10px; padding: 1.2rem 1.5rem; margin-bottom: 2rem; }}
  .onto-header {{ display: flex; align-items: center; gap: .8rem; }}
  .badge {{ color: white; padding: .25rem .6rem; border-radius: 6px; font-size: .8rem; font-weight: 600; }}
  .meta {{ color: var(--muted); font-size: .85rem; margin: .3rem 0 1rem; }}
  .meta code {{ font-size: .8rem; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: .8rem; margin-bottom: 1rem; }}
  .card {{ border: 1px solid var(--border); background: var(--card-bg); border-radius: 8px; padding: .6rem 1rem; min-width: 130px; }}
  .card-value {{ font-size: 1.3rem; font-weight: 700; }}
  .card-label {{ font-size: .75rem; color: var(--muted); }}
  .pills {{ display: flex; flex-wrap: wrap; gap: .35rem; }}
  .pill {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 4px;
           padding: .1rem .4rem; font-size: .8rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .85rem; margin-top: .5rem; }}
  th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--border); vertical-align: top; }}
  table.metrics td:nth-child(odd) {{ color: var(--muted); width: 22%; }}
  .sev {{ color: white; padding: .1rem .5rem; border-radius: 5px; font-size: .75rem; white-space: nowrap; }}
  .ok {{ color: var(--ok); }}
  .bad {{ color: var(--bad); }}
  .warn {{ color: var(--warn); }}
  .error-box {{ background: var(--error-bg); border: 1px solid var(--bad); border-radius: 8px; padding: .8rem; color: var(--bad); }}
  code {{ background: var(--code-bg); padding: .05rem .3rem; border-radius: 4px; }}
</style>
</head>
<body>
  <div class="top-bar">
    <div>
      <div class="top-title">Ontology Validation Report</div>
      <div class="top-meta">Generated {escape(generated)} &middot; {len(results)} ontology file(s)</div>
    </div>
    <button id="theme-toggle" type="button">🌓 Toggle theme</button>
  </div>
  {body}
  <script>
    (function () {{
      var root = document.documentElement;
      var btn = document.getElementById('theme-toggle');
      var stored = null;
      try {{ stored = localStorage.getItem('oops-report-theme'); }} catch (e) {{}}
      if (stored === 'dark' || stored === 'light') root.setAttribute('data-theme', stored);

      btn.addEventListener('click', function () {{
        var current = root.getAttribute('data-theme');
        if (!current) {{
          current = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        }}
        var next = current === 'dark' ? 'light' : 'dark';
        root.setAttribute('data-theme', next);
        try {{ localStorage.setItem('oops-report-theme', next); }} catch (e) {{}}
      }});
    }})();
  </script>
</body>
</html>"""


def write_html(results: list[dict], out_dir: Path, stamp: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"validation_report_{stamp}.html"
    path.write_text(render_html(results), encoding="utf-8")
    return path


def _pair_block(title: str, pack: dict, id_a: str, id_b: str) -> str:
    s = pack["summary"]
    return (
        f"<h3>{escape(title)}</h3>"
        f'<div class="cards">'
        f'{_card(f"Only in {id_a}", s["only_in_a"], "var(--warn)" if s["only_in_a"] else "var(--ok)")}'
        f'{_card("Shared", s["shared"])}'
        f'{_card(f"Only in {id_b}", s["only_in_b"], "var(--warn)" if s["only_in_b"] else "var(--ok)")}'
        f"</div>"
        f"<h4>Only in {escape(id_a)}</h4>{_pills(pack.get('only_in_a') or [])}"
        f"<h4>Shared</h4>{_pills(pack.get('shared') or [])}"
        f"<h4>Only in {escape(id_b)}</h4>{_pills(pack.get('only_in_b') or [])}"
    )


def render_compare_html(payload: dict) -> str:
    """Full HTML page for two-ontology compare (validation + pairwise vocab)."""
    left = payload["left"]
    right = payload["right"]
    pair = payload.get("pairwise_vocab")
    generated = payload.get("timestamp") or datetime.now().isoformat(timespec="seconds")
    id_a = (pair or {}).get("id_a") or left.get("name") or "A"
    id_b = (pair or {}).get("id_b") or right.get("name") or "B"

    if pair:
        pair_html = (
            f'<div class="ontology">'
            f'<div class="onto-header"><h1>Vocabulary: {escape(id_a)} ↔ {escape(id_b)}</h1></div>'
            f"{_pair_block('Classes', pair['classes'], id_a, id_b)}"
            f"{_pair_block('Object properties', pair['object_properties'], id_a, id_b)}"
            f"{_pair_block('Edge types used', pair['edge_types'], id_a, id_b)}"
            f"</div>"
        )
    else:
        pair_html = (
            '<div class="error-box">Could not compare vocabularies (parse failed on one side).</div>'
        )

    # Build via render_html shell, prepend pairwise block after meta strip
    page = render_html([left, right])
    page = page.replace(
        "<title>Ontology Validation Report</title>",
        f"<title>Ontology Compare — {escape(id_a)} vs {escape(id_b)}</title>",
        1,
    )
    page = page.replace(
        '<div class="top-title">Ontology Validation Report</div>',
        f'<div class="top-title">Ontology Compare — {escape(id_a)} vs {escape(id_b)}</div>',
        1,
    )
    page = page.replace(
        f"Generated {escape(datetime.now().isoformat(timespec='seconds'))} &middot; 2 ontology file(s)",
        f"Pairwise · {escape(generated)}",
        1,
    )
    # Fallback: insert after top-meta regardless of timestamp mismatch
    marker = '<button id="theme-toggle"'
    idx = page.find(marker)
    if idx != -1:
        # find end of top-bar
        end = page.find("</div>", page.find("</div>", idx) + 1)
        if end != -1:
            page = page[: end + 6] + pair_html + page[end + 6 :]
    return page


def write_compare_html(payload: dict, out_dir: Path, stamp: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"compare_report_{stamp}.html"
    path.write_text(render_compare_html(payload), encoding="utf-8")
    return path
