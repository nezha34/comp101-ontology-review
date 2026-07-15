"""
reasoner.py — OWL DL consistency check via owlready2 + HermiT.

This is the check most "structural validity" scripts skip: it doesn't just
verify the file is well-formed RDF, it verifies the axioms are logically
satisfiable. An inconsistent ontology (or one with unsatisfiable classes —
classes forced equivalent to owl:Nothing) will sail through every syntax
and shape check and still be broken for reasoning purposes.

Requires a JVM on PATH (HermiT ships bundled with owlready2 and runs via
Java) and the `owlready2` package.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from rdflib import Graph


def check_consistency(g: Graph) -> dict:
    """Run HermiT against a serialized copy of the graph in an isolated World.

    Returns:
      ok:                     bool — did the reasoner run at all
      consistent:             bool | None — None if it couldn't run
      unsatisfiable_classes:  list[str] of IRIs equivalent to owl:Nothing
      error:                  str | None
    """
    try:
        import owlready2
    except ImportError:
        return {
            "ok": False, "consistent": None, "unsatisfiable_classes": [],
            "error": "owlready2 not installed (pip install owlready2; also needs a JVM on PATH for HermiT)",
        }

    with tempfile.TemporaryDirectory() as tmp:
        owl_path = Path(tmp) / "onto.owl"
        try:
            g.serialize(destination=str(owl_path), format="xml")
        except Exception as e:
            return {"ok": False, "consistent": None, "unsatisfiable_classes": [],
                     "error": f"Could not serialize graph to RDF/XML for the reasoner: {e}"}

        world = owlready2.World()
        try:
            onto = world.get_ontology(f"file://{owl_path}").load()
        except Exception as e:
            return {"ok": False, "consistent": None, "unsatisfiable_classes": [],
                     "error": f"owlready2 failed to load serialized ontology: {e}"}

        try:
            with onto:
                owlready2.sync_reasoner_hermit(world, infer_property_values=False, debug=0)
        except owlready2.OwlReadyInconsistentOntologyError:
            return {
                "ok": True, "consistent": False, "unsatisfiable_classes": [],
                "error": "Ontology is logically INCONSISTENT under OWL DL semantics "
                         "(owl:Nothing would have instances)",
            }
        except Exception as e:
            return {"ok": False, "consistent": None, "unsatisfiable_classes": [],
                     "error": f"HermiT failed to run (is Java on PATH?): {type(e).__name__}: {e}"}

        unsatisfiable = [c.iri for c in world.inconsistent_classes()]

        return {
            "ok": True,
            "consistent": len(unsatisfiable) == 0,
            "unsatisfiable_classes": unsatisfiable,
            "error": None,
        }
