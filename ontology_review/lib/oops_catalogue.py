"""
oops_catalogue.py — static severity lookup for OOPS! pitfall codes.

The OOPS! REST API response does not include an importance level per
pitfall (see https://oops.linkeddata.es/webservice.html), only per the
published catalogue (https://oops.linkeddata.es/catalogue.jsp). We mirror
that table here so the report can sort/color by severity.
"""

PITFALL_CATALOGUE = {
    "P01": ("Creating polysemous elements", "Critical"),
    "P02": ("Creating synonyms as classes", "Minor"),
    "P03": ('Creating the relationship "is" instead of using OWL primitives', "Critical"),
    "P04": ("Creating unconnected ontology elements", "Minor"),
    "P05": ("Defining wrong inverse relationships", "Critical"),
    "P06": ("Including cycles in a class hierarchy", "Critical"),
    "P07": ("Merging different concepts in the same class", "Minor"),
    "P08": ("Missing annotations", "Minor"),
    "P09": ("Missing domain information", "Minor"),
    "P10": ("Missing disjointness", "Important"),
    "P11": ("Missing domain or range in properties", "Important"),
    "P12": ("Equivalent properties not explicitly declared", "Important"),
    "P13": ("Inverse relationships not explicitly declared", "Minor"),
    "P14": ('Misusing "owl:allValuesFrom"', "Critical"),
    "P15": ('Using "some not" in place of "not some"', "Critical"),
    "P16": ("Using a primitive class in place of a defined one", "Critical"),
    "P17": ("Overspecializing a hierarchy", "Important"),
    "P18": ("Overspecializing the domain or range", "Important"),
    "P19": ("Defining multiple domains or ranges in properties", "Critical"),
    "P20": ("Misusing ontology annotations", "Minor"),
    "P21": ("Using a miscellaneous class", "Minor"),
    "P22": ("Using different naming conventions in the ontology", "Minor"),
    "P23": ("Duplicating a datatype already provided by the implementation language", "Important"),
    "P24": ("Using recursive definitions", "Important"),
    "P25": ("Defining a relationship as inverse to itself", "Important"),
    "P26": ("Defining inverse relationships for a symmetric one", "Important"),
    "P27": ("Defining wrong equivalent properties", "Critical"),
    "P28": ("Defining wrong symmetric relationships", "Critical"),
    "P29": ("Defining wrong transitive relationships", "Critical"),
    "P30": ("Equivalent classes not explicitly declared", "Important"),
    "P31": ("Defining wrong equivalent classes", "Critical"),
    "P32": ("Several classes with the same label", "Minor"),
    "P33": ("Creating a property chain with just one property", "Minor"),
    "P34": ("Untyped class", "Important"),
    "P35": ("Untyped property", "Important"),
    "P36": ("URI contains file extension", "Minor"),
    "P37": ("Ontology not available on the Web", "Critical"),
    "P38": ("No OWL ontology declaration", "Important"),
    "P39": ("Ambiguous namespace", "Critical"),
    "P40": ("Namespace hijacking", "Critical"),
    "P41": ("No license declared", "Important"),
}


def severity_of(code: str) -> str:
    entry = PITFALL_CATALOGUE.get(code.upper())
    return entry[1] if entry else "Unknown"


def title_of(code: str) -> str:
    entry = PITFALL_CATALOGUE.get(code.upper())
    return entry[0] if entry else ""
