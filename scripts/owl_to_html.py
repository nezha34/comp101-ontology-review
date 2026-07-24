#!/usr/bin/env python3
"""Render an OWL/RDF ontology as a self-contained HTML reference page.

Style matches Gnosis_core ontologies/comp101/comp101.html (static curriculum
doc): classes, object/data properties, individuals grouped by class with
outgoing relations. Dependency-light (rdflib only).

Usage:
  python scripts/owl_to_html.py examples/comp101_L3_control_structures.owl
  python scripts/owl_to_html.py examples/comp101_L3_control_structures.owl -o examples/comp101_L3_control_structures.html
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from html import escape
from pathlib import Path

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.term import BNode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.graph_utils import (  # noqa: E402
    datatype_properties,
    label,
    load_graph,
    local_name,
    named_classes,
    named_individuals,
    object_properties,
    property_shape,
)

SKIP_TYPES = {"NamedIndividual", "Thing", "Nothing", "Class", "ObjectProperty",
              "DatatypeProperty", "AnnotationProperty", "Ontology"}


def _lit(g: Graph, node, *preds) -> str | None:
    for p in preds:
        v = g.value(node, p)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _parents(g: Graph, cls) -> list[str]:
    out = []
    for p in g.objects(cls, RDFS.subClassOf):
        if isinstance(p, BNode):
            continue
        name = local_name(p)
        if name and name not in ("Thing", "Nothing"):
            out.append(name)
    return sorted(set(out)) or ["owl:Thing"]


def _primary_type(g: Graph, ind) -> str:
    types = []
    for t in g.objects(ind, RDF.type):
        if isinstance(t, BNode):
            continue
        name = local_name(t)
        if name and name not in SKIP_TYPES:
            types.append(name)
    return types[0] if types else "Thing"


def _inverse_of(g: Graph, prop) -> str | None:
    inv = g.value(prop, OWL.inverseOf)
    if inv is not None and not isinstance(inv, BNode):
        return local_name(inv)
    # also check reverse: someone declares us as their inverse
    for s in g.subjects(OWL.inverseOf, prop):
        if not isinstance(s, BNode):
            return local_name(s)
    return None


def _characteristics(g: Graph, prop) -> list[str]:
    chips = []
    checks = [
        (OWL.SymmetricProperty, "symmetric"),
        (OWL.AsymmetricProperty, "asymmetric"),
        (OWL.TransitiveProperty, "transitive"),
        (OWL.FunctionalProperty, "functional"),
        (OWL.InverseFunctionalProperty, "inverseFunctional"),
        (OWL.ReflexiveProperty, "reflexive"),
        (OWL.IrreflexiveProperty, "irreflexive"),
    ]
    for cls, name in checks:
        if (prop, RDF.type, cls) in g:
            chips.append(name)
    return chips


def _ann_props(g: Graph) -> list:
    return [s for s in g.subjects(RDF.type, OWL.AnnotationProperty) if not isinstance(s, BNode)]


def render(g: Graph, out: Path) -> None:
    onto_uri = None
    version = ""
    onto_comment = ""
    onto_label = ""
    for s in g.subjects(RDF.type, OWL.Ontology):
        onto_uri = str(s)
        version = _lit(g, s, OWL.versionInfo) or ""
        onto_comment = _lit(g, s, RDFS.comment) or ""
        onto_label = _lit(g, s, RDFS.label) or local_name(s).replace("-", " ").replace("_", " ")
        break
    if not onto_uri:
        onto_uri = "(unknown)"
    if not onto_label:
        onto_label = local_name(onto_uri) or "Ontology"

    # individuals + outgoing object-property edges
    inds = named_individuals(g)
    label_of = {local_name(i): label(g, i) for i in inds}
    by_class: dict[str, list] = defaultdict(list)
    rels: dict[str, list[tuple[str, str]]] = defaultdict(list)
    lit_props: dict[str, dict[str, str]] = defaultdict(dict)

    for ind in inds:
        iid = local_name(ind)
        by_class[_primary_type(g, ind)].append(ind)
        for p, o in g.predicate_objects(ind):
            if p in (RDF.type, RDFS.label):
                continue
            if isinstance(o, Literal):
                lit_props[iid][local_name(p)] = str(o)
            elif isinstance(o, URIRef) and not isinstance(o, BNode):
                # only object props / named targets
                if (p, RDF.type, OWL.ObjectProperty) in g or local_name(p) not in (
                    "comment", "seeAlso", "isDefinedBy"
                ):
                    # skip if p is clearly annotation-only without being OP
                    if (p, RDF.type, OWL.AnnotationProperty) in g and (
                        p, RDF.type, OWL.ObjectProperty
                    ) not in g:
                        continue
                    rels[iid].append((local_name(p), local_name(o)))

    # lectures for filter buttons
    lectures = sorted(by_class.get("Lecture", []), key=lambda x: label(g, x))
    lecture_ids = [local_name(L) for L in lectures]

    def weeks_attr(iid: str) -> str:
        weeks = set()
        for pred, tgt in rels.get(iid, []):
            if pred in ("taughtIn", "revisitedIn") and tgt in lecture_ids:
                weeks.add(tgt)
        return " ".join(sorted(weeks))

    def rel_html(iid: str) -> str:
        items = rels.get(iid, [])
        if not items:
            return "<span class='muted'>—</span>"
        parts = []
        for pred, tgt in sorted(items, key=lambda x: (x[0], x[1])):
            tgt_lbl = label_of.get(tgt, tgt)
            parts.append(
                f"<span class='rel'>{escape(pred)}</span> &rarr; {escape(tgt_lbl)}"
            )
        return "<br>".join(parts)

    css = """
    body{font-family:'Segoe UI',Arial,sans-serif;background:#f5f6f8;color:#1a1a1a;margin:0;padding:0;}
    header{background:#1a3d5c;color:#fff;padding:24px 40px;}
    header h1{margin:0;font-size:22px;}
    header .meta{font-size:13px;opacity:.85;margin-top:6px;max-width:1000px;}
    main{padding:24px 40px;max-width:1100px;}
    h2{font-size:16px;border-bottom:2px solid #1a3d5c;padding-bottom:4px;margin-top:36px;color:#1a3d5c;}
    table{border-collapse:collapse;width:100%;margin-top:12px;font-size:13px;}
    th,td{border:1px solid #ccc;padding:6px 10px;text-align:left;vertical-align:top;}
    th{background:#1a3d5c;color:#fff;}
    tr:nth-child(even){background:#fafafa;}
    .id{color:#888;font-family:Consolas,monospace;font-size:11px;}
    .rel{font-family:Consolas,monospace;color:#D7492A;font-weight:700;}
    .muted{color:#aaa;}
    .sq{font-style:italic;color:#33526b;}
    .toc a{margin-right:14px;text-decoration:none;color:#1a3d5c;font-size:13px;}
    .char{display:inline-block;background:#0d6efd;color:#fff;font-size:10px;padding:1px 6px;border-radius:3px;font-family:Consolas,monospace;}
    .inv{display:inline-block;background:#eef;color:#334;font-size:10px;padding:1px 6px;border-radius:3px;font-family:Consolas,monospace;}
    .meta-lbl{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#6f42c1;font-weight:700;}
    .week-filter{background:#fff;border:1px solid #ccc;border-radius:6px;padding:10px 14px;margin:16px 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
    .week-filter .flt{border:1px solid #1a3d5c;background:#fff;color:#1a3d5c;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:13px;}
    .week-filter .flt.active{background:#1a3d5c;color:#fff;}
    .week-filter .flt-count{font-size:12px;margin-left:auto;}
    .note{font-size:13px;color:#555;max-width:1000px;background:#fff;border-left:4px solid #1a3d5c;padding:10px 14px;margin-top:14px;}
    """

    title = onto_label.title() if onto_label.islower() else onto_label
    H: list[str] = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>",
        f"<title>{escape(title)}</title>",
        f"<style>{css}</style></head><body>",
        "<header>",
        f"<h1>{escape(title)}</h1>",
        f"<div class='meta'>IRI: {escape(onto_uri)}"
        + (f" &middot; version {escape(version)}" if version else "")
        + f" &middot; {sum(1 for _ in inds)} individuals"
        + f" &middot; {len(list(object_properties(g)))} object properties</div>",
    ]
    if onto_comment:
        H.append(f"<div class='meta'>{escape(onto_comment)}</div>")
    H.append("</header><main>")
    H.append(
        "<div class='note'>Browsable curriculum reference generated from the OWL "
        "(same layout as Gnosis <code>comp101.html</code>). "
        "Outgoing relations are the asserted A-Box edges; inverse properties "
        "with no asserted triples still appear in the T-Box table.</div>"
    )

    # TOC
    H.append("<div class='toc'><b>Jump to:</b> ")
    H.append("<a href='#classes'>Classes</a>")
    H.append("<a href='#objprops'>Object properties</a>")
    H.append("<a href='#dataprops'>Data/Annotation properties</a>")
    for cls in sorted(by_class):
        H.append(f"<a href='#cls-{escape(cls)}'>{escape(cls)}s</a>")
    H.append("</div>")

    # lecture filter
    H.append(
        "<div class='week-filter'><b>Filter individuals by lecture:</b> "
        "<button type='button' class='flt active' data-week='all'>All</button> "
    )
    for L in lectures:
        lid = local_name(L)
        H.append(
            f"<button type='button' class='flt' data-week='{escape(lid)}'>"
            f"{escape(label(g, L))}</button> "
        )
    H.append("<span class='flt-count muted'></span></div>")

    # Classes
    H.append("<h2 id='classes'>Classes</h2>")
    H.append("<table><tr><th>Class</th><th>Parent</th><th>Comment</th></tr>")
    for c in sorted(named_classes(g), key=lambda x: local_name(x)):
        parents = ", ".join(_parents(g, c))
        comment = _lit(g, c, RDFS.comment) or ""
        H.append(
            f"<tr><td><b>{escape(local_name(c))}</b></td>"
            f"<td class='id'>{escape(parents)}</td>"
            f"<td>{escape(comment)}</td></tr>"
        )
    H.append("</table>")

    # Object properties
    H.append("<h2 id='objprops'>Object properties</h2>")
    H.append(
        "<table><tr><th>Property</th><th>Domain</th><th>Range</th>"
        "<th>Characteristics</th><th>Comment</th></tr>"
    )
    for prop in sorted(object_properties(g), key=lambda x: local_name(x)):
        shape = property_shape(g, prop)
        dom = ", ".join(shape.get("domain") or []) or "—"
        rng = ", ".join(shape.get("range") or []) or "—"
        chips = [f"<span class='char'>{escape(c)}</span>" for c in _characteristics(g, prop)]
        inv = _inverse_of(g, prop)
        if inv:
            chips.append(f"<span class='inv'>inverse of {escape(inv)}</span>")
        char_html = " ".join(chips) if chips else "<span class='muted'>—</span>"
        H.append(
            f"<tr><td class='rel'>{escape(local_name(prop))}</td>"
            f"<td class='id'>{escape(dom)}</td>"
            f"<td class='id'>{escape(rng)}</td>"
            f"<td>{char_html}</td>"
            f"<td>{escape(shape.get('comment') or '')}</td></tr>"
        )
    H.append("</table>")

    # Data + annotation
    H.append("<h2 id='dataprops'>Data &amp; annotation properties</h2>")
    H.append("<table><tr><th>Property</th><th>Kind</th><th>Range</th><th>Comment</th></tr>")
    for prop in sorted(datatype_properties(g), key=lambda x: local_name(x)):
        ranges = sorted({local_name(r) for r in g.objects(prop, RDFS.range) if not isinstance(r, BNode)})
        comment = _lit(g, prop, RDFS.comment) or ""
        H.append(
            f"<tr><td class='rel'>{escape(local_name(prop))}</td><td>data</td>"
            f"<td class='id'>{escape(', '.join(ranges) or '—')}</td>"
            f"<td>{escape(comment)}</td></tr>"
        )
    for prop in sorted(_ann_props(g), key=lambda x: local_name(x)):
        comment = _lit(g, prop, RDFS.comment) or ""
        H.append(
            f"<tr><td class='rel'>{escape(local_name(prop))}</td><td>annotation</td>"
            f"<td class='id'>—</td>"
            f"<td>{escape(comment)}</td></tr>"
        )
    H.append("</table>")

    # Individuals by class
    META_KEYS = (
        ("definition", "def"),
        ("cognitiveType", "cognitive"),
        ("bestPractice", "best practice"),
        ("commonCauses", "common causes"),
        ("solvesProblem", "solves"),
        ("timeComplexity", "complexity"),
        ("courseId", "courseId"),
    )
    for cls in sorted(by_class):
        members = sorted(by_class[cls], key=lambda x: (label(g, x).lower(), local_name(x)))
        n = len(members)
        H.append("<div class='ind-section'>")
        H.append(
            f"<h2 id='cls-{escape(cls)}'>{escape(cls)} individuals "
            f"<span class='sec-count'>({n})</span></h2>"
        )
        H.append(
            "<table><tr><th>Individual</th><th>Metadata</th><th>Relations</th></tr>"
        )
        for ind in members:
            iid = local_name(ind)
            props = lit_props.get(iid, {})
            meta_bits = []
            for key, lbl in META_KEYS:
                if props.get(key):
                    meta_bits.append(
                        f"<span class='meta-lbl'>{escape(lbl)}:</span> "
                        f"{escape(props[key])}"
                    )
            # any other literals not already shown
            shown = {k for k, _ in META_KEYS} | {"label", "comment"}
            for k, v in sorted(props.items()):
                if k not in shown:
                    meta_bits.append(
                        f"<span class='meta-lbl'>{escape(k)}:</span> {escape(v)}"
                    )
            meta_html = "<br>".join(meta_bits) if meta_bits else "<span class='muted'>—</span>"
            H.append(
                f"<tr class='ind-row' data-weeks='{escape(weeks_attr(iid))}'>"
                f"<td><b>{escape(label(g, ind))}</b>"
                f"<br><span class='id'>{escape(iid)}</span></td>"
                f"<td>{meta_html}</td>"
                f"<td>{rel_html(iid)}</td></tr>"
            )
        H.append("</table></div>")

    H.append("""<script>
(function(){
  const btns=document.querySelectorAll('.week-filter .flt');
  const rows=document.querySelectorAll('tr.ind-row');
  function apply(week){
    btns.forEach(b=>b.classList.toggle('active',b.dataset.week===week));
    let shown=0;
    rows.forEach(row=>{
      const wks=(row.dataset.weeks||'').split(/\\s+/).filter(Boolean);
      const show=week==='all'||wks.includes(week)||(week!=='all'&&wks.length===0&&false);
      // individuals with no taughtIn stay visible only on All
      const show2=week==='all'||wks.includes(week);
      row.style.display=show2?'':'none';
      if(show2)shown++;
    });
    document.querySelectorAll('.ind-section').forEach(sec=>{
      const all=sec.querySelectorAll('tr.ind-row');
      const vis=[...all].filter(r=>r.style.display!=='none').length;
      const cnt=sec.querySelector('.sec-count');
      if(cnt)cnt.textContent=week==='all'?'('+all.length+')':'('+vis+' of '+all.length+')';
      if(vis===0&&week!=='all'&&all.length>0)sec.style.display='none';
      else if(all.length>0)sec.style.display='';
    });
    const fc=document.querySelector('.flt-count');
    if(fc)fc.textContent=week==='all'?'':shown+' rows shown';
  }
  btns.forEach(b=>b.addEventListener('click',()=>apply(b.dataset.week)));
})();
</script>""")
    H.append("</main></body></html>")
    out.write_text("\n".join(H), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"  individuals={len(inds)} classes={len(named_classes(g))} "
          f"object_properties={len(object_properties(g))}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("owl", type=Path, help="Input .owl / .ttl file")
    ap.add_argument("-o", "--out", type=Path, default=None, help="Output HTML path")
    args = ap.parse_args()
    owl = args.owl
    if not owl.is_file():
        print(f"Not found: {owl}", file=sys.stderr)
        return 1
    out = args.out or owl.with_suffix(".html")
    g, err = load_graph(owl)
    if err:
        print(f"Parse failed: {err}", file=sys.stderr)
        return 1
    render(g, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
