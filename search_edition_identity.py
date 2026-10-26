"""Literal source-local names and printed edition references; never audio equivalence."""

from __future__ import annotations

import re
from typing import Any

from search_evidence_quality import SourceScope, literal_term, scoped_text
from search_models import Query, claim_identity, norm

FORMATS = {"CD", "LP", "SACD", "DVD", "SHM", "UHQCD", "UK", "US", "USA", "EU", "WG", "JP", "UDCD", "GZS", "CDP"}
NAMES = r"(?:Mobile Fidelity(?: Sound Lab)?|MFSL|MoFi|DCC(?: Compact Classics)?|Black Triangle|BT|EMI|Harvest|Geffen(?: Records)?|Parlophone|MCA|Atlantic|Virgin(?:/Charisma)?)"
CATALOGUE = r"(?:UDCD[ -]?(?:2[ -]?)?\d{3}|UDSACD[ -]?\d{4}|GZS[ -]?\d{4}|CP(?:32|35)[ -]?\d{4}|CDP[ -]?7[ -]?\d[ -]?\d[ -]?\d[ -]?\d[ -]?\d[ -]?\d|UICY[ -]?\d{4,5}|MCAD[ -]?\d{4,5})"
PRINTED = re.compile(
    r"(?<!\w)(?P<name>" + NAMES + r")"
    r"(?P<gap>(?:\s+|[(/\-]|\b(?:gold|disc|CD|LP|SACD|remaster|remastered|records|compact|classics|platinum|SHM-CD)\b){0,10})"
    r"(?P<catalogue>" + CATALOGUE + r")(?!\w)",
    re.IGNORECASE,
)


def source_context(source: dict[str, Any]) -> str:
    return scoped_text(source)


def local_mention(
    edition: dict[str, Any], source: dict[str, Any], span: str, editions: list[dict[str, Any]]
) -> tuple[str, str]:
    """Resolve a short mention only after the full name is present in THIS post."""
    descriptor = edition["descriptor"]
    text = source_context(source)
    if descriptor not in text:
        return "", ""
    possibilities: list[tuple[str, str]] = []
    catalogue = edition["features"].get("catalog_number", "")
    if catalogue and catalogue in descriptor:
        possibilities.append((catalogue, descriptor))
    for match in re.finditer(r"\b[A-Z][A-Z0-9]{1,8}\b", descriptor):
        value = match.group(0)
        if value not in FORMATS and not value.isdigit():
            possibilities.append((value, descriptor))
    for match in re.finditer(re.escape(descriptor) + r"\s*\(\s*([A-Za-z][A-Za-z0-9-]{1,15})\s*\)", text):
        possibilities.append((match.group(1), match.group(0)))
    for value, proof in sorted(possibilities, key=lambda p: -len(p[0])):
        if not literal_term(span, value):
            continue
        competing = [
            e
            for e in editions
            if e["edition_id"] != edition["edition_id"]
            and e["descriptor"] in text
            and literal_term(e["descriptor"], value)
        ]
        if competing:
            continue
        # Another printed catalogue attached to the same name in this post makes
        # even a single model-extracted edition insufficient to resolve that name.
        catalogues = {
            claim_identity("catalog_number", m.group("catalogue"))
            for m in PRINTED.finditer(text)
            if norm(m.group("name")) == norm(value)
        }
        if len(catalogues) > 1:
            continue
        return value, proof
    return "", ""


def catalogue_key(value: str) -> str:
    if re.fullmatch(CATALOGUE, value, re.IGNORECASE):
        return re.sub(r"[ -]", "", value).casefold()
    return claim_identity("catalog_number", value)


def edition_references(sources: list[dict[str, Any]], query: Query) -> list[dict[str, Any]]:
    """Collect bounded, literally printed name/catalogue pairs from own scoped text."""
    scope = SourceScope(sources, query)
    result = []
    seen: set[tuple[str, str, str]] = set()
    for source in sources:
        if scope.reason(source) != "usable":
            continue
        for match in PRINTED.finditer(source_context(source)):
            name, catalogue, quote = match.group("name"), match.group("catalogue"), match.group(0)
            key = (source["source_id"], norm(name), catalogue_key(catalogue))
            if key in seen:
                continue
            seen.add(key)
            medium = next((v for v in ("SACD", "CD", "LP") if literal_term(match.group("gap"), v)), "")
            result.append(
                {
                    "name": name,
                    "catalog_number": catalogue,
                    "medium": medium,
                    "source_id": source["source_id"],
                    "quote": quote,
                }
            )
            if len(result) == 80:
                return result
    return result


def reference_options(edition: dict[str, Any], references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cross-post name matches are suggestions, never confirmed identity/feature fills."""
    result = []
    for ref in references:
        if not literal_term(edition["descriptor"], ref["name"]):
            continue
        known = edition["features"].get("catalog_number", "")
        if known and catalogue_key(known) != catalogue_key(ref["catalog_number"]):
            continue
        result.append({**ref, "identity_binding": "unconfirmed"})
    return result
