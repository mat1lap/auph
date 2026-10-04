#!/usr/bin/env python3
"""Field-level, deterministic overwrite policy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from metadata_schema import (
    FIELD_SPECS,
    STABLE_ID_FIELDS,
    Cardinality,
    Scope,
    canonical_field_name,
    normalize_value,
    values_equal,
)


class Confidence(IntEnum):
    UNKNOWN = 0
    LOW = 25
    MEDIUM = 50
    HIGH = 75
    VERIFIED = 100


class DecisionAction(StrEnum):
    KEEP = "KEEP"
    ADD = "ADD"
    REPLACE = "REPLACE"
    MERGE = "MERGE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class FieldDecision:
    field: str
    action: DecisionAction
    values: tuple[str, ...]
    reason: str


def dedupe(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value and not any(values_equal(value, old) for old in result):
            result.append(value)
    return tuple(result)


def decide_field(
    field: str,
    existing: Iterable[object],
    incoming: Iterable[object],
    *,
    incoming_confidence: Confidence = Confidence.HIGH,
    existing_confidence: Confidence = Confidence.HIGH,
    release_confidence: Confidence = Confidence.HIGH,
    stable_ids_agree: bool | None = None,
    scope_agreement: Mapping[Scope, bool | None] | None = None,
    force: bool = False,
) -> FieldDecision:
    """Choose one of KEEP/ADD/REPLACE/MERGE/REVIEW_REQUIRED.

    Unknown fields are never managed here.  Stable identifier conflicts are
    review-only even in forced mode: callers must resolve identity first.
    """
    name = canonical_field_name(field)
    old = dedupe(normalize_value(name, v) for v in existing)
    new = dedupe(normalize_value(name, v) for v in incoming)
    new = tuple(v for v in new if v)
    old = tuple(v for v in old if v)
    if not new:
        return FieldDecision(name, DecisionAction.KEEP, old, "incoming value is empty")
    spec = FIELD_SPECS.get(name)
    if spec is None:
        if not old:
            return FieldDecision(name, DecisionAction.ADD, new, "new extension field")
        return FieldDecision(name, DecisionAction.KEEP, old, "unknown fields are preserved")
    scope_agrees = (scope_agreement or {}).get(spec.scope, stable_ids_agree)
    # A known stable-ID contradiction invalidates the candidate as an entity,
    # not merely the one identifier field.  Do not fill empty release/track/
    # recording fields from a candidate that belongs to another edition.
    # File-local analysis and the transaction link remain independent.
    if scope_agrees is False and spec.scope is not Scope.FILE:
        return FieldDecision(
            name, DecisionAction.REVIEW_REQUIRED, old, "stable identity conflicts with existing metadata"
        )
    if not old:
        # Filling an empty tag is still destructive in the semantic sense: it
        # can assert a wrong edition for years.  Require evidence appropriate
        # to the field's entity instead of treating absence as permission.
        minimum = Confidence.MEDIUM if spec.scope in {Scope.FILE, Scope.RECORDING} else Confidence.HIGH
        if incoming_confidence < minimum or (
            spec.scope not in {Scope.FILE, Scope.RECORDING} and release_confidence < Confidence.HIGH
        ):
            return FieldDecision(
                name, DecisionAction.REVIEW_REQUIRED, old, "insufficient confidence to fill empty scoped field"
            )
        return FieldDecision(name, DecisionAction.ADD, new, "field is absent")
    if len(old) == len(new) and all(any(values_equal(v, n) for n in new) for v in old):
        return FieldDecision(name, DecisionAction.KEEP, old, "values already agree")

    if name in STABLE_ID_FIELDS and scope_agrees is not True:
        return FieldDecision(name, DecisionAction.REVIEW_REQUIRED, old, "conflicting stable identifier")
    if spec.preserve_existing and not force:
        return FieldDecision(name, DecisionAction.KEEP, old, "preservation field already exists")
    # Only inherently set-like credits can be merged.  Release identifiers,
    # labels/catalogues, genres/styles and artist/id pairs need provenance and
    # an explicit same-entity agreement rather than a blind union.
    mergeable = {
        "COMPOSER",
        "COMPOSERSORT",
        "LYRICIST",
        "WRITER",
        "CONDUCTOR",
        "PERFORMER",
        "REMIXER",
        "PRODUCER",
        "ARRANGER",
        "LANGUAGE",
        "ENSEMBLE",
        "ORCHESTRA",
        "CHOIR",
        "CHORUS",
        "SOLOIST",
        "LIBRETTIST",
    }
    if (
        spec.cardinality is Cardinality.MULTI
        and name in mergeable
        and scope_agrees is True
        and incoming_confidence >= Confidence.HIGH
    ):
        merged = dedupe((*old, *new))
        return FieldDecision(
            name, DecisionAction.MERGE, merged, "same-entity repeatable credit merged without flattening"
        )
    # A pre-existing value without per-field provenance is neither wrong nor
    # safely replaceable.  Keep it visible for review instead of pretending
    # every legacy tag was HIGH confidence.
    if existing_confidence is Confidence.UNKNOWN and not force:
        return FieldDecision(name, DecisionAction.REVIEW_REQUIRED, old, "existing value has unknown provenance")
    if force or (
        incoming_confidence > existing_confidence
        and release_confidence >= Confidence.HIGH
        and scope_agrees is not False
    ):
        replacement = new[:1] if spec.cardinality is Cardinality.SINGLE else new
        return FieldDecision(name, DecisionAction.REPLACE, replacement, "higher-confidence compatible source")
    return FieldDecision(name, DecisionAction.REVIEW_REQUIRED, old, "conflict lacks sufficient identity confidence")
