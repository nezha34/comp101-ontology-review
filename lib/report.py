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
            or s.get("semantic_issues", 0) > 0
            or result["consistency"].get("consistent") is None):
        return "PASS WITH WARNINGS", "var(--warn)"
    return "PASS", "var(--ok)"


def summarize(result: dict) -> dict:
    structural = result["structural"]
    oops = result["oops"]
    cq = result.get("sparql_cqs", [])
    skill = result.get("skill_graph")
    semantic = result.get("semantic") or {}

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
        "semantic_flagged": semantic.get("phase1_flagged", 0),
        "semantic_issues": len(semantic.get("issues", [])),
        "semantic_unverifiable": len(semantic.get("phase2_unverifiable", [])),
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


def _render_one(result: dict, run_id: str | None = None, decisions: dict | None = None) -> str:
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
        _card("Semantic issues (LLM)", s.get("semantic_issues", 0),
              "var(--warn)" if s.get("semantic_issues", 0) else "var(--ok)"),
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

    # Ontology T-Box drift vs baseline
    drift = result.get("ontology_drift") or (result.get("semantic") or {}).get("ontology_drift")
    if drift is not None:
        parts.append(_section("Ontology T-Box Drift (vs baseline)", _render_ontology_drift(drift)))

    # Semantic judge (LLM, two-phase)
    semantic = result.get("semantic")
    if semantic is not None:
        parts.append(_section("Semantic / Logical Correctness (LLM judge)",
                               _render_semantic(semantic, run_id, decisions)))

    parts.append("</div>")
    return "".join(parts)


def _render_ontology_drift(drift: dict) -> str:
    if not drift.get("ok"):
        return f"<p class='warn'>Drift not computed: {escape(drift.get('error') or 'unknown error')}</p>"
    s = drift.get("summary") or {}
    baseline = drift.get("baseline_path") or "(baseline)"
    cards = "".join([
        _card("Classes +", s.get("classes_added", 0),
              "var(--warn)" if s.get("classes_added") else "var(--ok)"),
        _card("Classes −", s.get("classes_removed", 0),
              "var(--warn)" if s.get("classes_removed") else "var(--ok)"),
        _card("Properties +", s.get("properties_added", 0),
              "var(--warn)" if s.get("properties_added") else "var(--ok)"),
        _card("Properties −", s.get("properties_removed", 0),
              "var(--warn)" if s.get("properties_removed") else "var(--ok)"),
    ])
    body = (
        f'<div class="cards">{cards}</div>'
        f"<p>Compared candidate T-Box to baseline <code>{escape(baseline)}</code> "
        f"by local name. Gap classes/properties with <code>rdfs:comment</code> are "
        f"fed into the semantic judge so it does not invent meanings.</p>"
    )

    def _list_entities(title: str, items: list, kind: str) -> str:
        if not items:
            return ""
        rows = []
        for e in items:
            comment = escape(e.get("comment") or "(no rdfs:comment)")
            if kind == "property":
                shape = (
                    f" domain=[{escape(', '.join(e.get('domain') or []))}]"
                    f" range=[{escape(', '.join(e.get('range') or []))}]"
                )
            else:
                parents = e.get("subclass_of") or []
                shape = f" ⊑ {escape(', '.join(parents))}" if parents else ""
            rows.append(
                f"<li><code>{escape(e['id'])}</code>{shape}: {comment}</li>"
            )
        return f"<h3>{escape(title)} ({len(items)})</h3><ul>{''.join(rows)}</ul>"

    body += _list_entities("Classes only in candidate", drift.get("classes_added") or [], "class")
    body += _list_entities("Object properties only in candidate",
                           drift.get("properties_added") or [], "property")
    body += _list_entities("Classes only in baseline", drift.get("classes_removed") or [], "class")
    body += _list_entities("Object properties only in baseline",
                           drift.get("properties_removed") or [], "property")
    if not any(drift.get(k) for k in (
        "classes_added", "classes_removed", "properties_added", "properties_removed"
    )):
        body += "<p class='ok'>No T-Box vocabulary drift.</p>"
    return body


def _render_review_controls(run_id: str, idx: int, status: str | None) -> str:
    """Accept/dismiss controls for one semantic-judge finding. Accept queues
    it into the changes-to-make store (see app.py); dismiss just records
    that a human looked at it and disagreed with the model. `status` is the
    previously-saved decision ('accepted'/'dismissed'/None), so a reload of
    the results page reflects prior review instead of resetting the buttons."""
    return (
        f'<div class="review-row" data-run="{escape(run_id)}" data-idx="{idx}" '
        f'data-status="{escape(status or "")}">'
        f'<button type="button" class="review-btn accept-btn" '
        f'onclick="reviewDecision(this,\'accept\')">Accept</button>'
        f'<button type="button" class="review-btn dismiss-btn" '
        f'onclick="reviewDecision(this,\'dismiss\')">Dismiss</button>'
        f'<span class="review-status"></span>'
        f'</div>'
    )


def _render_semantic(semantic: dict, run_id: str | None = None, decisions: dict | None = None) -> str:
    if semantic.get("error") == "pending":
        return ("<p class='warn'>Semantic judge is running in the background "
                "(this can take a few minutes) — this page will refresh automatically "
                "once it's done.</p>")
    if not semantic.get("ok") and semantic.get("error"):
        return f"<p class='warn'>Semantic judge not run: {escape(semantic['error'])}</p>"

    total = semantic.get("phase1_total_claims", 0)
    flagged = semantic.get("phase1_flagged", 0)
    issues = semantic.get("issues", [])
    resolved = semantic.get("phase2_resolved", [])
    unverifiable = semantic.get("phase2_unverifiable", [])

    n_skip_batch = len(semantic.get("skipped_batches", []))
    n_skip_batch_claims = sum(b.get("batch_size", 0) for b in semantic.get("skipped_batches", []))
    n_skip_claim = len(semantic.get("skipped_claims", []))
    n_attempted = total + n_skip_batch_claims + n_skip_claim
    coverage_pct = round(100 * total / n_attempted) if n_attempted else 100

    gated = semantic.get("gated_count", 0)
    gated_note = ""
    if gated:
        gated_note = (
            f" <b>{gated}</b> more edge(s) weren't screened at all — their relation type "
            f"(e.g. taughtIn) can only be verified against external material like lecture "
            f"content, which isn't wired in yet, so asking the model would just manufacture "
            f"unverifiable flags."
        )

    reviewed_skipped = semantic.get("reviewed_skipped", [])
    reviewed_note = ""
    if reviewed_skipped:
        reviewed_note = (
            f" <b>{len(reviewed_skipped)}</b> more were skipped entirely because a human already "
            f"reviewed them on a prior run (see the review store) — not re-judged, not re-argued."
        )

    provider_label = {"ollama": "Ollama (local)", "nvidia_nim": "NVIDIA NIM (hosted)"}.get(
        semantic.get("provider"), semantic.get("provider") or ""
    )

    cards = "".join([
        _card("Coverage", f"{coverage_pct}%", "var(--ok)" if coverage_pct == 100 else "var(--warn)"),
        _card("Confirmed issues", len(issues), "var(--bad)" if issues else "var(--ok)"),
        _card("Unverifiable", len(unverifiable), "var(--warn)" if unverifiable else "var(--ok)"),
        _card("Resolved by context", len(resolved)),
    ])

    body = (
        f'<div class="cards">{cards}</div>'
        f"<p>Phase 1 screened <b>{total}</b> asserted edges, flagged <b>{flagged}</b> for a closer look."
        f"{reviewed_note}{gated_note} "
        f"Phase 2 re-checked each flag against its local context: <b>{len(resolved)}</b> were resolved by "
        f"context alone, <b>{len(unverifiable)}</b> can't be judged from graph context alone (see below), "
        f"<b>{len(issues)}</b> survived as confirmed issues and are written up below. "
        f"Model: <code>{escape(semantic.get('model') or '')}</code> via {escape(provider_label)}. "
        f"These are proposals for human review, not auto-applied changes."
    )
    if n_skip_batch or n_skip_claim:
        body += (
            f" <span class='warn'>({n_skip_batch} phase-1 batch(es) covering ~{n_skip_batch_claims} claims "
            f"and {n_skip_claim} phase-2 claim(s) failed after retries and were skipped — "
            f"see the coverage figure above.)</span>"
        )
    body += "</p>"

    if not issues:
        body += "<p class='ok'>No confirmed semantic issues.</p>"
    else:
        decisions = decisions or {}
        for i, issue in enumerate(issues):
            fix_action = issue.get("proposed_fix_action", "none")
            fix_line = ""
            if fix_action and fix_action != "none":
                fix_line = (
                    f"<p><b>Proposed fix</b> ({escape(fix_action)}): "
                    f"<code>{escape(issue.get('proposed_fix_triple',''))}</code><br>"
                    f"<span class='meta'>{escape(issue.get('proposed_fix_rationale',''))}</span></p>"
                )
            review_line = ""
            if run_id:
                status = decisions.get(str(i))
                review_line = _render_review_controls(run_id, i, status)
            trunc_note = ""
            if issue.get("context_truncated"):
                ct = issue.get("context_truncation") or {}
                trunc_note = (
                    f'<p class="warn"><b>Context truncated</b> for this flag '
                    f'(subject neighborhood {ct.get("subject_neighborhood_shown")}/'
                    f'{ct.get("subject_neighborhood_total")}; '
                    f'object {ct.get("object_neighborhood_shown")}/'
                    f'{ct.get("object_neighborhood_total")}). '
                    f'Double-check before accepting a fix.</p>'
                )
            body += (
                f'<div class="issue-box" id="semantic-issue-{i}">'
                f'<h3>{escape(issue["subject"])} <code>--{escape(issue["predicate"])}--&gt;</code> {escape(issue["object"])}</h3>'
                f'<p><b>{escape(issue.get("issue_summary",""))}</b></p>'
                f'{trunc_note}'
                f'<p><b>Evidence:</b> {escape(issue.get("evidence",""))}</p>'
                f'<p><b>Reasoning:</b> {escape(issue.get("phase2_reasoning",""))}</p>'
                f'{fix_line}'
                f'<p class="meta">Phase 1 verdict: {escape(issue.get("phase1_verdict",""))} — '
                f'{escape(issue.get("phase1_reasoning",""))}</p>'
                f'{review_line}'
                f'</div>'
            )

    if unverifiable:
        rows = "".join(
            f"<li><b>{escape(u['subject'])} --{escape(u['predicate'])}--&gt; {escape(u['object'])}</b>: "
            f"{escape(u.get('phase2_reasoning',''))}</li>"
            for u in unverifiable
        )
        body += (
            f"<details><summary>{len(unverifiable)} edge(s) unverifiable from graph context alone "
            f"(needs a human check against real source material — syllabus, lecture notes, etc.)</summary>"
            f"<ul>{rows}</ul></details>"
        )

    if resolved:
        rows = "".join(
            f"<li>{escape(r['subject'])} --{escape(r['predicate'])}--&gt; {escape(r['object'])}: "
            f"{escape(r.get('resolution_explanation',''))}</li>"
            for r in resolved
        )
        body += f"<details><summary>{len(resolved)} flag(s) resolved by context (click to expand)</summary><ul>{rows}</ul></details>"

    return body


def render_body(results: list[dict], run_id: str | None = None, decisions: dict | None = None) -> str:
    return "".join(_render_one(r, run_id, decisions) for r in results)


REPORT_CSS = """
  :root {
    color-scheme: light dark;
    --bg: #ffffff; --fg: #1f2328; --muted: #57606a; --border: rgba(31,35,40,.15);
    --card-bg: #f6f8fa; --code-bg: rgba(175,184,193,.25);
    --ok: #1a7f37; --bad: #d1242f; --warn: #9a6700; --error-bg: rgba(209,36,47,.1);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --border: rgba(240,246,252,.18);
      --card-bg: #161b22; --code-bg: rgba(110,118,129,.35);
      --ok: #3fb950; --bad: #f85149; --warn: #d29922; --error-bg: rgba(248,81,73,.12);
    }
  }
  :root[data-theme="dark"] {
    --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --border: rgba(240,246,252,.18);
    --card-bg: #161b22; --code-bg: rgba(110,118,129,.35);
    --ok: #3fb950; --bad: #f85149; --warn: #d29922; --error-bg: rgba(248,81,73,.12);
  }
  :root[data-theme="light"] {
    --bg: #ffffff; --fg: #1f2328; --muted: #57606a; --border: rgba(31,35,40,.15);
    --card-bg: #f6f8fa; --code-bg: rgba(175,184,193,.25);
    --ok: #1a7f37; --bad: #d1242f; --warn: #9a6700; --error-bg: rgba(209,36,47,.1);
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
          max-width: 1100px; margin: 2rem auto; padding: 0 1.5rem; line-height: 1.5;
          background: var(--bg); color: var(--fg); }
  h1 { font-size: 1.5rem; margin: 0; }
  h2 { font-size: 1.1rem; border-bottom: 1px solid var(--border); padding-bottom: .3rem; margin-top: 2rem; }
  h3 { font-size: .95rem; margin-bottom: .3rem; }
  .top-bar { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; flex-wrap: wrap; }
  .top-title { font-size: 1.6rem; font-weight: 600; margin-bottom: .2rem; }
  .top-meta { color: var(--muted); margin-bottom: 1.5rem; }
  .top-actions { display: flex; gap: .6rem; align-items: center; }
  .nav-link, #theme-toggle { border: 1px solid var(--border); background: var(--card-bg); color: var(--fg);
                    border-radius: 8px; padding: .4rem .7rem; font-size: .85rem; cursor: pointer; flex-shrink: 0;
                    text-decoration: none; display: inline-block; }
  .nav-link:hover, #theme-toggle:hover { opacity: .8; }
  .ontology { border: 1px solid var(--border); border-radius: 10px; padding: 1.2rem 1.5rem; margin-bottom: 2rem; }
  .onto-header { display: flex; align-items: center; gap: .8rem; }
  .badge { color: white; padding: .25rem .6rem; border-radius: 6px; font-size: .8rem; font-weight: 600; }
  .meta { color: var(--muted); font-size: .85rem; margin: .3rem 0 1rem; }
  .meta code { font-size: .8rem; }
  .cards { display: flex; flex-wrap: wrap; gap: .8rem; margin-bottom: 1rem; }
  .card { border: 1px solid var(--border); background: var(--card-bg); border-radius: 8px; padding: .6rem 1rem; min-width: 130px; }
  .card-value { font-size: 1.3rem; font-weight: 700; }
  .card-label { font-size: .75rem; color: var(--muted); }
  table { border-collapse: collapse; width: 100%; font-size: .85rem; margin-top: .5rem; }
  th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  table.metrics td:nth-child(odd) { color: var(--muted); width: 22%; }
  .sev { color: white; padding: .1rem .5rem; border-radius: 5px; font-size: .75rem; white-space: nowrap; }
  .ok { color: var(--ok); }
  .bad { color: var(--bad); }
  .warn { color: var(--warn); }
  .error-box { background: var(--error-bg); border: 1px solid var(--bad); border-radius: 8px; padding: .8rem; color: var(--bad); }
  .issue-box { border: 1px solid var(--warn); border-radius: 8px; padding: .8rem 1rem; margin: .8rem 0; background: rgba(154,103,0,.06); }
  .issue-box h3 { margin: 0 0 .5rem; font-size: .95rem; display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
  .issue-box p { margin: .35rem 0; font-size: .88rem; }
  details { margin-top: .8rem; font-size: .85rem; }
  summary { cursor: pointer; color: var(--muted); }
  code { background: var(--code-bg); padding: .05rem .3rem; border-radius: 4px; }
  .review-row { display: flex; align-items: center; gap: .5rem; margin-top: .7rem;
                padding-top: .6rem; border-top: 1px dashed var(--border); }
  .review-btn { border: 1px solid var(--border); border-radius: 6px; padding: .3rem .7rem;
                font-size: .8rem; cursor: pointer; background: var(--bg); color: var(--fg); }
  .review-btn:hover { opacity: .8; }
  .review-btn.active-accept { background: var(--ok); color: white; border-color: var(--ok); }
  .review-btn.active-dismiss { background: var(--muted); color: white; border-color: var(--muted); }
  .review-btn:disabled { cursor: default; opacity: .55; }
  .review-status { font-size: .8rem; color: var(--muted); }
"""

REPORT_THEME_SCRIPT = """
    (function () {
      var root = document.documentElement;
      var btn = document.getElementById('theme-toggle');
      var stored = null;
      try { stored = localStorage.getItem('oops-report-theme'); } catch (e) {}
      if (stored === 'dark' || stored === 'light') root.setAttribute('data-theme', stored);

      btn.addEventListener('click', function () {
        var current = root.getAttribute('data-theme');
        if (!current) {
          current = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        }
        var next = current === 'dark' ? 'light' : 'dark';
        root.setAttribute('data-theme', next);
        try { localStorage.setItem('oops-report-theme', next); } catch (e) {}
      });
    })();
"""


REVIEW_SCRIPT = """
    function applyReviewStatus(row, status) {
      var acceptBtn = row.querySelector('.accept-btn');
      var dismissBtn = row.querySelector('.dismiss-btn');
      var label = row.querySelector('.review-status');
      row.setAttribute('data-status', status || '');
      acceptBtn.classList.toggle('active-accept', status === 'accepted');
      dismissBtn.classList.toggle('active-dismiss', status === 'dismissed');
      if (status === 'accepted') label.textContent = 'Queued for changes.';
      else if (status === 'dismissed') label.textContent = 'Dismissed — model disagreed with.';
      else label.textContent = '';
    }

    function reviewDecision(btn, action) {
      var row = btn.closest('.review-row');
      var run = row.getAttribute('data-run');
      var idx = row.getAttribute('data-idx');
      var label = row.querySelector('.review-status');
      label.textContent = 'Saving…';
      fetch('/decision/' + encodeURIComponent(run) + '/' + encodeURIComponent(idx), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action })
      }).then(function (r) {
        if (!r.ok) throw new Error('request failed');
        return r.json();
      }).then(function (data) {
        applyReviewStatus(row, data.status);
      }).catch(function () {
        label.textContent = 'Save failed — try again.';
      });
    }

    document.querySelectorAll('.review-row').forEach(function (row) {
      applyReviewStatus(row, row.getAttribute('data-status'));
    });
"""


def render_page(title: str, meta_line: str, body: str, nav_links: list[tuple[str, str]] | None = None) -> str:
    """Wrap arbitrary body HTML in the shared report page chrome (theme toggle, CSS)."""
    links_html = "".join(
        f'<a class="nav-link" href="{escape(url)}">{escape(label)}</a>' for label, url in (nav_links or [])
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>{REPORT_CSS}</style>
</head>
<body>
  <div class="top-bar">
    <div>
      <div class="top-title">{escape(title)}</div>
      <div class="top-meta">{meta_line}</div>
    </div>
    <div class="top-actions">
      {links_html}
      <button id="theme-toggle" type="button">🌓 Toggle theme</button>
    </div>
  </div>
  {body}
  <script>{REPORT_THEME_SCRIPT}</script>
  <script>{REVIEW_SCRIPT}</script>
</body>
</html>"""


def render_html(results: list[dict], run_id: str | None = None, decisions: dict | None = None) -> str:
    generated = datetime.now().isoformat(timespec="seconds")
    meta = f"Generated {escape(generated)} &middot; {len(results)} ontology file(s)"
    return render_page("Ontology Validation Report", meta, render_body(results, run_id, decisions))


def write_html(results: list[dict], out_dir: Path, stamp: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"validation_report_{stamp}.html"
    path.write_text(render_html(results), encoding="utf-8")
    return path
