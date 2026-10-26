"""Merge validated batches and account for sources without inventing missing opinions."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from typing import Any

from search_edition_identity import edition_references, reference_options
from search_evidence_quality import SourceScope
from search_models import Query
from search_research import Research


def merge_graphs(batches: list[tuple[str, dict[str, Any]]], research: Research, query: Query) -> dict[str, Any]:
    """Only identical literal descriptors with identical features share an edition.

    Brand matches, aliases and incomplete vs full identities are never merged here.
    Every input record has already passed validation within its own batch.
    """
    if len(batches) == 1:
        graph = deepcopy(batches[0][1])
    else:
        graph = {"editions": [], "opinions": [], "equivalences": [], "rejected_items": {}, "rejection_details": []}
        identities: dict[str, dict[str, Any]] = {}
        rejected: Counter[str] = Counter()
        for batch_id, batch in batches:
            ids = {}
            for index, edition in enumerate(batch["editions"]):
                key = json.dumps([edition["descriptor"], edition["features"]], sort_keys=True)
                if key not in identities:
                    item = deepcopy(edition)
                    item.setdefault("feature_evidence", [])
                    item["edition_id"] = f"{batch_id}:e{index}"
                    identities[key] = item
                    graph["editions"].append(item)
                else:
                    item = identities[key]
                    item["source_ids"] = list(dict.fromkeys([*item["source_ids"], *edition["source_ids"]]))
                    for proof in edition.get("feature_evidence", []):
                        if proof not in item["feature_evidence"]:
                            item["feature_evidence"].append(deepcopy(proof))
                ids[edition["edition_id"]] = item["edition_id"]
            for index, opinion in enumerate(batch["opinions"]):
                item = deepcopy(opinion)
                item.update(
                    opinion_id=f"{batch_id}:o{index}",
                    edition_id=ids[opinion["edition_id"]],
                    compared_edition_id=ids[opinion["compared_edition_id"]] if opinion["compared_edition_id"] else "",
                )
                graph["opinions"].append(item)
            for link in batch["equivalences"]:
                left, right = ids[link["left"]], ids[link["right"]]
                if left != right:
                    graph["equivalences"].append({**link, "left": left, "right": right})
            rejected.update(batch.get("rejected_items", {}))
            graph["rejection_details"].extend({**d, "batch_id": batch_id} for d in batch.get("rejection_details", []))
        graph["rejected_items"] = dict(rejected)
        parent = {e["edition_id"]: e["edition_id"] for e in graph["editions"]}

        def root(eid: str) -> str:
            while parent[eid] != eid:
                eid = parent[eid]
            return eid

        for link in graph["equivalences"]:
            parent[root(link["right"])] = root(link["left"])
        groups: dict[str, list[dict[str, Any]]] = {}
        for edition in graph["editions"]:
            groups.setdefault(root(edition["edition_id"]), []).append(edition)
        for members in groups.values():
            keys = sorted(json.dumps([e["descriptor"], e["features"]], sort_keys=True) for e in members)
            gid = "mastering:" + hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]
            for edition in members:
                edition["mastering_group_id"] = gid
        graph["interpretation"] = "Mastering links are explicit reported equivalences, never measured audio identity."
    # These remain unconfirmed lookup hints, even if their post was in another batch.
    graph["edition_references"] = edition_references(research.sources, query)
    for edition in graph["editions"]:
        edition["identity_reference_options"] = reference_options(edition, graph["edition_references"])
    graph["capacity_exceeded"] = any(
        len(graph[field]) > bound for field, bound in (("editions", 200), ("opinions", 400), ("equivalences", 150))
    )
    return graph


def batch_sources(
    batch_id: str, source_ids: list[str], payload: object, graph: dict[str, Any] | None, status: str
) -> list[dict[str, Any]]:
    """No proposed opinion means unknown coverage, not proof that a post is neutral."""
    raw = payload.get("opinions", []) if isinstance(payload, dict) else []
    proposed = Counter(
        o["source_id"]
        for o in (raw if isinstance(raw, list) else [])
        if isinstance(o, dict) and isinstance(o.get("source_id"), str)
    )
    accepted = Counter(o["source_id"] for o in graph["opinions"]) if graph else Counter()
    rows = []
    for sid in source_ids:
        rules = sorted(
            {
                d["rule"]
                for d in (graph.get("rejection_details", []) if graph else [])
                if d["kind"] == "opinions" and sid in d["source_ids"]
            }
        )
        rows.append(
            {
                "source_id": sid,
                "batch_id": batch_id,
                "status": status
                if status != "succeeded"
                else "accepted"
                if accepted[sid]
                else "opinion_rejected"
                if proposed[sid]
                else "no_opinion_proposed",
                "proposed_opinions": proposed[sid],
                "accepted_opinions": accepted[sid],
                "rejected_opinions": max(0, proposed[sid] - accepted[sid]),
                "rejection_rules": rules,
            }
        )
    return rows


def coverage_report(
    research: Research, query: Query, selected: list[str], batches: list[dict[str, Any]]
) -> dict[str, Any]:
    scope = SourceScope(research.sources, query)
    rows = {r["source_id"]: r for batch in batches for r in batch["sources"]}
    selected_ids = set(selected)
    for source in research.sources:
        sid = source["source_id"]
        if sid not in rows:
            reason = scope.reason(source)
            rows[sid] = {
                "source_id": sid,
                "status": "not_selected" if reason == "usable" else "excluded_scope",
                "relevance": reason,
            }
        rows[sid]["selected"] = sid in selected_ids
    counts = dict(Counter(r["status"] for r in rows.values()))
    return {
        "selected_sources": len(selected),
        "source_status_counts": counts,
        "sources": list(rows.values()),
        "batches": batches,
        "all_usable_sources_represented": not any(
            counts.get(v, 0)
            for v in ("not_selected", "request_failed", "not_attempted", "opinion_rejected", "no_opinion_proposed")
        ),
        "interpretation": "Coverage of retained model records, not expert recall or proof that unrepresented posts lack opinions.",
    }


def add_literal_coverage(coverage: dict[str, Any], recovered: list[tuple[str, dict[str, Any]]]) -> None:
    """Keep the model's outcome visible alongside independently recovered own prose."""
    counts = Counter(o["source_id"] for _, graph in recovered for o in graph["opinions"])
    coverage["model_source_status_counts"] = dict(coverage["source_status_counts"])
    coverage["literal_opinion_count"] = sum(counts.values())
    coverage["literal_recovered_sources"] = list(counts)
    for row in coverage["sources"]:
        sid = row["source_id"]
        if sid in counts:
            row.update(
                model_status=row["status"],
                model_accepted_opinions=row.get("accepted_opinions", 0),
                literal_opinions=counts[sid],
                accepted_opinions=row.get("accepted_opinions", 0) + counts[sid],
                status="accepted",
            )
    coverage["source_status_counts"] = dict(Counter(r["status"] for r in coverage["sources"]))
    coverage["all_usable_sources_represented"] = not any(
        coverage["source_status_counts"].get(v, 0)
        for v in ("not_selected", "request_failed", "not_attempted", "opinion_rejected", "no_opinion_proposed")
    )
