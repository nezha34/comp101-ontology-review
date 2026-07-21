"""
graph_view.py — renders the ontology as an interactive vis-network graph
(same library the existing COMP101_KnowledgeGraph.html files use, loaded
from the unpkg CDN) with edges implicated in a semantic-judge issue
highlighted. Clicking a highlighted edge shows the judge's write-up.
"""

from __future__ import annotations

import json
from html import escape

from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.term import BNode

from .graph_utils import label, object_properties


def _local(uri) -> str:
    s = str(uri)
    return s.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _primary_type(g: Graph, node) -> str:
    for t in g.objects(node, RDF.type):
        name = _local(t)
        if name != "NamedIndividual":
            return name
    return "Thing"


def _index_issues(result: dict) -> dict:
    """Map (subject_uri, predicate_local, object_uri) -> issue record + kind."""
    semantic = result.get("semantic") or {}
    index = {}
    for rec in semantic.get("issues", []):
        key = (rec["subject_uri"], rec["predicate"], rec["object_uri"])
        index[key] = {"kind": "issue", **rec}
    for rec in semantic.get("phase2_unverifiable", []):
        key = (rec["subject_uri"], rec["predicate"], rec["object_uri"])
        index.setdefault(key, {"kind": "unverifiable", **rec})
    for rec in semantic.get("phase2_resolved", []):
        key = (rec["subject_uri"], rec["predicate"], rec["object_uri"])
        index.setdefault(key, {"kind": "resolved", **rec})
    return index


_KIND_COLOR = {"issue": "#f85149", "unverifiable": "#d29922", "resolved": "#58a6ff"}
_KIND_WIDTH = {"issue": 3, "unverifiable": 2, "resolved": 1}


def build_graph_html(g: Graph, result: dict, title: str) -> str:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    issue_index = _index_issues(result)

    def add_node(uri: URIRef):
        key = str(uri)
        if key in nodes:
            return
        nodes[key] = {"id": key, "label": label(g, uri), "group": _primary_type(g, uri)}

    for prop in object_properties(g):
        pred_name = _local(prop)
        for s, o in g.subject_objects(prop):
            if isinstance(s, BNode) or isinstance(o, BNode):
                continue
            add_node(s)
            add_node(o)
            match = issue_index.get((str(s), pred_name, str(o)))
            edge = {
                "from": str(s), "to": str(o), "label": pred_name,
                "arrows": "to", "color": {"color": "#8b949e"}, "width": 1,
            }
            if match:
                kind = match["kind"]
                edge["color"] = {"color": _KIND_COLOR[kind]}
                edge["width"] = _KIND_WIDTH[kind]
                edge["issue"] = match
            edges.append(edge)

    nodes_json = json.dumps(list(nodes.values()))
    edges_json = json.dumps(edges, default=str)

    n_issues = sum(1 for e in edges if e.get("issue", {}).get("kind") == "issue")
    n_unverifiable = sum(1 for e in edges if e.get("issue", {}).get("kind") == "unverifiable")
    n_resolved = sum(1 for e in edges if e.get("issue", {}).get("kind") == "resolved")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)} — Graph</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
          display: flex; height: 100vh; background: Canvas; color: CanvasText; }}
  #graph {{ flex: 1; }}
  #panel {{ width: 340px; border-left: 1px solid rgba(128,128,128,.3); padding: 1rem; overflow-y: auto; }}
  #panel h2 {{ font-size: 1rem; margin-top: 0; }}
  #panel .field {{ margin-bottom: .7rem; font-size: .85rem; }}
  #panel .field b {{ display: block; color: gray; font-size: .72rem; text-transform: uppercase; margin-bottom: .15rem; }}
  .legend {{ font-size: .8rem; padding: .5rem 1rem; border-bottom: 1px solid rgba(128,128,128,.3); }}
  .legend span {{ display: inline-flex; align-items: center; gap: .3rem; margin-right: 1rem; }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
  a.back {{ font-size: .8rem; }}
</style>
</head>
<body>
  <div id="graph"></div>
  <div id="panel">
    <a class="back" href="javascript:history.back()">&larr; back</a>
    <h2>{escape(title)}</h2>
    <div class="legend">
      <span><span class="dot" style="background:#f85149"></span>{n_issues} confirmed issue(s)</span>
      <span><span class="dot" style="background:#d29922"></span>{n_unverifiable} unverifiable</span>
      <span><span class="dot" style="background:#58a6ff"></span>{n_resolved} resolved by context</span>
    </div>
    <p id="detail">Click a highlighted edge to see the semantic judge's write-up. Other edges have no flagged issue.</p>
  </div>
<script>
  const nodes = new vis.DataSet({nodes_json});
  const edges = new vis.DataSet({edges_json});
  const network = new vis.Network(document.getElementById('graph'), {{nodes, edges}}, {{
    physics: {{ stabilization: true, barnesHut: {{ springLength: 120 }} }},
    edges: {{ font: {{ size: 10, align: 'middle' }}, smooth: {{ type: 'dynamic' }} }},
    nodes: {{ shape: 'dot', size: 12, font: {{ size: 13 }} }},
  }});

  function esc(s) {{ const d = document.createElement('div'); d.innerText = s || ''; return d.innerHTML; }}

  network.on('click', function(params) {{
    if (!params.edges.length) return;
    const edge = edges.get(params.edges[0]);
    const detail = document.getElementById('detail');
    if (!edge.issue) {{
      detail.innerHTML = '<p>No semantic issue flagged for <code>' + esc(edge.label) + '</code> on this edge.</p>';
      return;
    }}
    const iss = edge.issue;
    if (iss.kind === 'resolved') {{
      detail.innerHTML = '<div class="field"><b>Status</b>Flagged in phase 1, resolved by context in phase 2</div>' +
        '<div class="field"><b>Phase 1 verdict</b>' + esc(iss.phase1_verdict) + ' — ' + esc(iss.phase1_reasoning) + '</div>' +
        '<div class="field"><b>Resolution</b>' + esc(iss.resolution_explanation) + '</div>';
    }} else if (iss.kind === 'unverifiable') {{
      detail.innerHTML = '<div class="field"><b>Status</b>Can\\'t be judged from graph context alone — needs a human check against real source material</div>' +
        '<div class="field"><b>Phase 1 verdict</b>' + esc(iss.phase1_verdict) + ' — ' + esc(iss.phase1_reasoning) + '</div>' +
        '<div class="field"><b>Why unverifiable</b>' + esc(iss.evidence) + ' ' + esc(iss.phase2_reasoning) + '</div>';
    }} else {{
      detail.innerHTML = '<div class="field"><b>Issue</b>' + esc(iss.issue_summary) + '</div>' +
        '<div class="field"><b>Evidence</b>' + esc(iss.evidence) + '</div>' +
        '<div class="field"><b>Reasoning</b>' + esc(iss.phase2_reasoning) + '</div>' +
        '<div class="field"><b>Proposed fix (' + esc(iss.proposed_fix_action) + ')</b>' + esc(iss.proposed_fix_triple) + '<br><i>' + esc(iss.proposed_fix_rationale) + '</i></div>';
    }}
  }});
</script>
</body>
</html>"""
