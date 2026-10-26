"""The offline audit must preserve evidence and never invoke providers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from audit_mastering_quality import audit
from mastering_benchmark import fingerprint, write_json
from test_search_mastering import A, B, Q, candidates, decision, evidence, extraction


def write_case(base: Path, cid: str, r: Any) -> dict[str, Any]:
    snap = {
        "query": {"text": Q.text, "artist": Q.artist, "title": Q.title, "release_type": Q.release_type},
        "research": r.public(),
        "candidates": [c.public() for c in candidates()],
    }
    write_json(base / "corpus" / (cid + ".json"), snap)
    return {"case_id": cid, "file": cid + ".json", "fingerprint": fingerprint(snap)}


def test_audit_withdraws_a_positive_only_old_choice_and_does_not_change_corpus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import requests

    monkeypatch.setattr(
        requests.Session, "request", lambda *args, **kwargs: pytest.fail("Offline audit must not request providers")
    )
    base = tmp_path / "artifacts"
    r = evidence(f"I like {A}. {B} is another version.")
    first = write_case(base, "album-01", r)
    second = write_case(base, "album-02", r)
    payload = extraction(r)
    payload["opinions"][0].update(
        quote=f"I like {A}.", comparison_type="not_established", compared_edition_id="", characteristics=[]
    )
    old_review = {**decision(), "preferred_edition": A, "evidence_graph": payload, "retained_sources": 1}
    write_json(base / "corpus/manifest.json", {"cases": [first, second]})
    write_json(
        base / "final-comparison/summary.json",
        {
            "results": [
                {
                    "model": "gemini:gemini-3.5-flash-lite",
                    "case_id": "album-01",
                    "metrics": {"status": "preference"},
                    "review": old_review,
                },
                {
                    "model": "gemini:gemini-3.5-flash-lite",
                    "case_id": "album-02",
                    "metrics": {"status": "insufficient_evidence"},
                    "review": {"evidence_graph": {"editions": [], "opinions": [], "equivalences": []}},
                },
            ]
        },
    )
    before = {p: p.read_bytes() for p in (base / "corpus").glob("*.json")}
    result = audit(base, tmp_path / "audit")
    assert result["recommendation_verdicts"] == {"endorsement_only": 1}
    assert result["abstentions_audited"] == 1 and result["new_provider_calls"] == 0
    assert all(p.read_bytes() == value for p, value in before.items())
    withdrawal = result["cases"][0]["offline_withdrawal"]
    assert withdrawal["status"] == "insufficient_evidence" and withdrawal["candidate_id"] == ""
    assert withdrawal["endorsements"][0]["stance"] == "endorse"
    assert result["cases"][1]["original_extraction_available"] is False
    assert result["cases"][1]["manual_observations_applied"] is False
