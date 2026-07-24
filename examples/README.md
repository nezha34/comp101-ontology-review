WHAT GOES IN examples/
=======================

This folder collects lecture-level ontology drafts from everyone on the
team, before they get merged into the shared COMP101 ontology. The structure below is required so the merge step doesn't
turn into archaeology. If you're about to drop files in here, follow
this exactly.

FOLDER STRUCTURE
----------------

    examples/
        Lecture 1/
          comp101_L1.owl
          comp101_L1.json
        Lecture 9/
          comp101_L9.owl
          comp101_L9.json
        ...


- One folder per lecture, named literally "Lecture <N>" (e.g.
  "Lecture 1", "Lecture 9"). Not "lecture1", not "L9", not "Week 9" —
  "Lecture N" so folders sort and grep the same way for everyone.

THE .owl FILE
--------------

- Must be named exactly comp101_L<N>.owl, matching the lecture number
  of the folder it's in (Lecture 9 -> comp101_L9.owl). This naming is
  load-bearing: the merge tooling and the config namespace matching in
  validate.py key off this pattern, so "comp101L9.owl",
  "comp101_lecture9.owl" etc. will get missed.

- RDF/XML (or Turtle), same style as the existing files in this folder
  (comp101_oop.owl, comp101_ics.owl) — rdflib-loadable, every
  class/property/individual has an rdfs:label.

- Every individual should carry a taughtIn (or equivalent) link to its
  lecture individual, and a stable ID via whatever id_property the
  course config uses (e.g. courseId) so duplicate-ID checks and merges
  can key off it.

THE .json FILE
---------------

Same basename as the .owl file (comp101_L9.json next to
comp101_L9.owl). This is the sidecar that makes merging tractable: it
tells the person merging which entity types and relations are being
reused from the existing shared schema vs. newly introduced in this
lecture, plus the formal bits (domain/range/characteristics) that
aren't always obvious just from staring at the OWL file.

Required shape:

{
  "lecture": 9,
  "author": "<your name>",
  "source_owl": "comp101_L9.owl",
  "namespace": "http://comp101.sase.um6p.ma/ontology/<module>#",

  "entity_types": [
    {
      "name": "OOPMechanism",
      "status": "old",              // "old" = reused from the existing
                                      // shared schema, "new" = introduced
                                      // by this lecture
      "definition": "A language mechanism that implements an OOP concept.",
      "parent_class": null            // subclass-of, if any; else null
    },
    {
      "name": "DesignPattern",
      "status": "new",
      "definition": "A reusable solution template to a recurring design problem.",
      "parent_class": null
    }
  ],

  "relations": [
    {
      "name": "dependsOn",
      "status": "old",
      "definition": "Subject cannot be reasonably understood/performed until the object has already been learned.",
      "domain": "Skill",
      "range": "Skill",
      "characteristics": {
        "symmetric": false,
        "transitive": true,
        "functional": false,
        "inverse_transitive": false,
        "inverse_of": null
      }
    },
    {
      "name": "isVariantOf",
      "status": "new",
      "definition": "Subject design pattern is a specialization/variant of the object pattern.",
      "domain": "DesignPattern",
      "range": "DesignPattern",
      "characteristics": {
        "symmetric": false,
        "transitive": false,
        "functional": false,
        "inverse_of": "hasVariant"
      }
    }
  ]
}

Notes on the fields:
- status ("old" vs "new") is the whole point of this file. "old" means
  it already exists in the shared schema (concept, algorithm, dtatype etc) and you're just reusing it — merging should dedupe
  against the existing definition, not create a second one. "new"
  means you introduced it for this lecture and it needs a merge
  decision (accept as-is, rename to avoid collision, or fold into an
  existing type/relation).

- List every entity type (class) and every relation (object/datatype
  property) actually used in the .owl file, not just the new ones —
  the merger needs the full picture to spot domain/range mismatches
  against the existing schema.

- domain/range: use the class name as it appears in the OWL file
  (rdfs:domain / rdfs:range), not a description.

- characteristics: fill in whichever apply (symmetric, transitive,
  functional, inverse_of, reflexive, ...). Leave out ones that don't
  apply rather than guessing — an omitted field is safer than a wrong
  one.

- If a relation is the inverse of another one you're also defining,
  point inverse_of at it by name so the merge doesn't end up with two
  unlinked halves of the same pair (see isThrownBy/throwsError,
  taughtIn/teaches in the existing OOP/ICS ontologies for the pattern).

ANYTHING ELSE WORTH FLAGGING
-----------------------------

If something about your lecture's ontology doesn't fit the fields
above but matters for merging — e.g. "I reused the Skill class but
gave SK_L9_03 a courseId prefix that collides with an existing ID",
"this lecture's ontology doesn't pass the HermiT reasoner yet",
"I intentionally renamed an old relation because its old definition
was wrong" :

=>
add a short "notes" field (free text) to the .json rather
than leaving it undocumented. The goal of all of this is simply to
make the eventual merge of everyone's per-lecture ontologies into one
course ontology as low-friction as possible: no surprises, no silent
naming collisions, no guessing which parts are shared vocabulary and
which parts are one lecture's invention.
