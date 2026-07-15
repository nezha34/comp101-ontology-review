"""
oops_client.py — thin client for the live OOPS! (OntOlogy Pitfall Scanner!)
REST API: https://oops.linkeddata.es/webservice.html

POSTs the ontology's serialized RDF/XML content (not just a URL, since
these ontologies live on local disk) and parses the XML pitfall report.
Network errors are caught and surfaced as a soft failure so the rest of
the pipeline can still run offline.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .oops_catalogue import severity_of

OOPS_ENDPOINT = "https://oops.linkeddata.es/rest"
OOPS_NS = "{http://www.oeg-upm.net/oops}"


def call_oops_api(ontology_content: str, pitfalls: str = "", timeout: int = 90) -> dict:
    """Submit RDF/XML content to the OOPS! REST API.

    Returns a dict: {ok, error, pitfalls: [...], warnings: [...], suggestions: [...]}
    Each pitfall dict has: code, name, description, severity, affected_elements.
    """
    try:
        import requests
    except ImportError:
        return {
            "ok": False,
            "error": "requests not installed (pip install requests) — or pass --no-oops",
            "pitfalls": [],
            "warnings": [],
            "suggestions": [],
        }

    request_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<OOPSRequest>\n"
        "  <OntologyUrl></OntologyUrl>\n"
        f"  <OntologyContent><![CDATA[{ontology_content}]]></OntologyContent>\n"
        f"  <Pitfalls>{pitfalls}</Pitfalls>\n"
        "  <OutputFormat>XML</OutputFormat>\n"
        "</OOPSRequest>"
    )

    empty = {"ok": False, "error": None, "pitfalls": [], "warnings": [], "suggestions": []}

    try:
        resp = requests.post(
            OOPS_ENDPOINT,
            data=request_xml.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        empty["error"] = f"OOPS! API request failed: {e}"
        return empty

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        empty["error"] = f"OOPS! API returned unparsable XML: {e}"
        empty["raw_response"] = resp.text[:2000]
        return empty

    def affected(el) -> list[str]:
        return [e.text.strip() for e in el.findall(f".//{OOPS_NS}AffectedElement") if e.text]

    pitfalls_out = []
    for el in root.findall(f"{OOPS_NS}Pitfall"):
        code = (el.findtext(f"{OOPS_NS}Code") or "").strip()
        pitfalls_out.append({
            "code": code,
            "name": (el.findtext(f"{OOPS_NS}Name") or "").strip(),
            "description": (el.findtext(f"{OOPS_NS}Description") or "").strip(),
            "severity": severity_of(code) if code else "Unknown",
            "affected_elements": affected(el),
        })

    warnings_out = [
        {
            "name": (el.findtext(f"{OOPS_NS}Name") or "").strip(),
            "affected_elements": affected(el),
        }
        for el in root.findall(f"{OOPS_NS}Warning")
    ]

    suggestions_out = [
        {
            "name": (el.findtext(f"{OOPS_NS}Name") or "").strip(),
            "description": (el.findtext(f"{OOPS_NS}Description") or "").strip(),
            "affected_elements": affected(el),
        }
        for el in root.findall(f"{OOPS_NS}Suggestion")
    ]

    return {
        "ok": True,
        "error": None,
        "pitfalls": pitfalls_out,
        "warnings": warnings_out,
        "suggestions": suggestions_out,
    }
