"""Regressions for live topic transport, diagnostic privacy and title identity."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_config import SearchPolicy
from release_search import workflow
from search_models import Query, assess_relevance
from search_providers import Jackett, parse_details
from test_release_search import Network, candidate, feed, policy, topic

# Reuse the external-boundary fixture, without replacing production parsing.
__all__ = ["policy"]


def title_candidate(policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch, title: str) -> Any:
    Network(monkeypatch, lambda *args: (200, feed([(2, title)])))
    return Jackett(policy).search(Query("Queen Jazz"), 1, 1, 1)[0]


@pytest.mark.parametrize(
    "title",
    [
        "[TR24][OF] The Queen's Cartoonists, Joel Pierson - Mozart's Jazz Requiem - 2023 (Jazz)",
        "The Queen’s Cartoonists — Mozart’s Jazz Requiem",
        "Queen - Mozart's Jazz Requiem",
        "Queen Bee - Jazz",
    ],
)
def test_title_word_overlap_is_not_work_identity(
    policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch, title: str
) -> None:
    c = title_candidate(policy, monkeypatch, title)
    assess_relevance(c, Query("", "Queen", "Jazz", "album"), policy)
    assert not c.eligible
    assert c.identity_confidence == "tentative"


@pytest.mark.parametrize(
    "title,medium,codec,depth,rate",
    [
        (
            "(Hard Rock, Pop Rock) [CD] Queen - Jazz (MQA x UHQCD) - 2018 (1978), FLAC (image+.cue), lossless",
            "CD",
            "FLAC",
            None,
            None,
        ),
        ("(Rock) [LP] [24/192] Queen - Jazz - 1978, FLAC (tracks+.cue)", "LP", "FLAC", "24 bit", "192 kHz"),
        (
            "(Rock) [LP] [32f/192] Queen - Jazz - 1978 (Japan), WavPack (image+.cue)",
            "LP",
            "WAVPACK",
            "32f bit",
            "192 kHz",
        ),
        ("(Rock) [CD] [16/44,1] Queen – Jazz - 1978, FLAC", "CD", "FLAC", "16 bit", "44.1 kHz"),
        ("[TR24][OF] Queen - Jazz - 1978", None, None, None, None),
    ],
)
def test_title_prefixes_and_explicit_claims(
    policy: SearchPolicy,
    monkeypatch: pytest.MonkeyPatch,
    title: str,
    medium: str | None,
    codec: str | None,
    depth: str | None,
    rate: str | None,
) -> None:
    c = title_candidate(policy, monkeypatch, title)
    assess_relevance(c, Query("", "Queen", "Jazz", "album"), policy)
    assert (c.artist, c.release_title) == ("Queen", "Jazz")
    assert c.eligible and c.identity_confidence == "tentative"
    assert (c.medium, c.codec, c.bit_depth, c.sample_rate) == (medium, codec, depth, rate)
    assert c.release_year is None and c.complete is None
    assert all(e["status"] == "title-derived, unverified" for e in c.evidence.values() if e["source"] == "search_title")


def test_title_claims_not_promoted_by_unrelated_topic_fields(
    policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = title_candidate(policy, monkeypatch, "[CD] Queen - Jazz - 1978, FLAC")
    parse_details(c, b'<div class="post_body">Country: UK</div>')
    assert c.evidence["codec"]["source"] == c.evidence["lossless"]["source"] == "search_title"
    parse_details(c, topic(codec="MP3"))
    assert c.codec == "MP3" and c.lossless is False
    assert c.evidence["codec"]["source"] == "topic_detail"


@pytest.mark.parametrize(
    "status,body,headers,expected,marker",
    [
        (
            403,
            b"<title>Just a moment...</title>",
            {"Content-Type": "text/html", "cf-mitigated": "challenge"},
            "BLOCKED",
            "cf-mitigated: challenge",
        ),
        (403, b"Forbidden", {"Content-Type": "text/html"}, "BLOCKED", "HTTP 403"),
        (200, b"<title>Just a moment...</title>", {"Content-Type": "text/html"}, "BLOCKED", "<title>just a moment"),
        (200, b'<input name="login_username">', {"Content-Type": "text/html"}, "AUTHENTICATION_ERROR", "authorized"),
        (
            302,
            b"",
            {"Location": "https://rutracker.org/forum/login.php?token=PRIVATE"},
            "AUTHENTICATION_ERROR",
            "login.php",
        ),
        (302, b"", {"Location": "https://other.test/topic?token=PRIVATE"}, "REDIRECT_REFUSED", "other.test/topic"),
        (200, b"{}", {"Content-Type": "application/json"}, "INVALID_RESPONSE", "not HTML"),
        (200, b"<html>unexpected</html>", {"Content-Type": "text/html"}, "PARSER_ERROR", "no recognized topic post"),
    ],
)
def test_detail_failure_diagnostics(
    policy: SearchPolicy,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    headers: dict[str, str],
    expected: str,
    marker: str,
) -> None:
    net = Network(monkeypatch, lambda *args: (status, body, headers))
    c = candidate()
    Jackett(policy).details(c)
    assert c.detail_status == expected
    assert c.detail_diagnostics["http_status"] == status
    assert c.detail_diagnostics["final_location"] == "rutracker.org/forum/viewtopic.php"
    assert marker in " ".join(c.warnings)
    assert "PRIVATE" not in json.dumps(c.public())
    assert len(net.calls) == 1 and net.calls[0][2]["allow_redirects"] is False


def solution(body: str | None = None, **changes: Any) -> dict[str, Any]:
    data = {
        "url": candidate().source_url,
        "status": 200,
        "headers": {"content-type": "text/html"},
        "response": body or topic().decode(),
        "userAgent": "FixtureBrowser/1.0",
        "cookies": [
            {"name": "cf_clearance", "value": "private-clearance", "domain": ".rutracker.org", "path": "/"},
            {"name": "bb_session", "value": "anonymous", "domain": "rutracker.org", "path": "/"},
            {"name": "foreign", "value": "excluded", "domain": "other.test", "path": "/"},
        ],
    }
    data.update(changes)
    return {"status": "ok", "solution": data}


def solver_policy(policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch) -> SearchPolicy:
    monkeypatch.setenv("RUTRACKER_COOKIE", "bb_session=private-session; bb_data=private-data")
    return replace(policy, flaresolverr_url="http://solver.test:8191", flaresolverr_timeout=20)


def test_solver_uses_clearance_identity_and_original_encoding(
    policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = solver_policy(policy, monkeypatch)
    body = '<meta charset="windows-1251"><div class="post_body">Исполнитель: Queen<br>Название альбома: Jazz<br>Страна: Япония<br>Треклист:<br>01. Песня</div>'
    net = Network(
        monkeypatch,
        lambda method, url, kw: (
            (200, solution(body))
            if method == "POST"
            else (200, body.encode("cp1251"), {"Content-Type": "text/html; charset=windows-1251"})
        ),
    )
    c = candidate()
    Jackett(policy).details(c)
    assert c.detail_status == "FOUND" and c.track_list == ["01. Песня"] and c.country == "Япония"
    method, url, kw = net.calls[0]
    assert len(net.calls) == 2 and method == "POST" and url == "http://solver.test:8191/v1"
    assert kw["json"]["cmd"] == "request.get" and kw["json"]["maxTimeout"] == 20000
    assert kw["json"]["url"] == c.source_url
    assert "cookies" not in kw["json"]
    assert "headers" not in kw and "session" not in kw["json"]
    authenticated = net.calls[1]
    assert authenticated[0] == "GET" and authenticated[1] == c.source_url
    assert authenticated[2]["headers"] == {
        "Cookie": "cf_clearance=private-clearance; bb_session=private-session; bb_data=private-data",
        "User-Agent": "FixtureBrowser/1.0",
    }
    assert "private-session" not in json.dumps(kw) and "private-data" not in json.dumps(kw)
    public = json.dumps(c.public())
    assert (
        "private-session" not in public
        and "private-data" not in public
        and "fixture-secret" not in public
        and "private-clearance" not in public
    )
    assert c.detail_diagnostics["transport"] == "flaresolverr"


@pytest.mark.parametrize(
    "response,expected",
    [
        (
            (500, {"status": "error", "message": "Error solving challenge. Timeout after 20 seconds. private-session"}),
            "BLOCKED",
        ),
        ((200, {"status": "error", "message": "CAPTCHA required private-session"}), "BLOCKED"),
        ((200, {"status": "error", "message": "Browser unavailable private-session"}), "TRANSIENT_ERROR"),
        ((200, []), "INVALID_RESPONSE"),
        ((200, {"status": "ok", "solution": None}), "INVALID_RESPONSE"),
        ((200, solution(status="200")), "INVALID_RESPONSE"),
        ((200, solution(url="https://rutracker.org/forum/viewtopic.php?t=3")), "REDIRECT_REFUSED"),
        ((200, solution(url="https://foreign.test/forum/login.php")), "REDIRECT_REFUSED"),
        ((200, solution(url="https://rutracker.org/forum/login.php?token=PRIVATE")), "AUTHENTICATION_ERROR"),
        ((200, solution("<title>Just a moment...</title>", status=403)), "BLOCKED"),
        ((200, solution('<input name="login_password">')), "AUTHENTICATION_ERROR"),
        ((200, b"not JSON private-session"), "PARSER_ERROR"),
    ],
)
def test_solver_failures_are_bounded_distinct_and_private(
    policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch, response: tuple[Any, ...], expected: str
) -> None:
    policy = solver_policy(policy, monkeypatch)
    net = Network(
        monkeypatch,
        lambda method, url, kw: (
            response if method == "POST" else (200, b'<input name="login_password">', {"Content-Type": "text/html"})
        ),
    )
    c = candidate()
    Jackett(policy).details(c)
    assert c.detail_status == expected
    details = response[1].get("solution") if isinstance(response[1], dict) else None
    needs_auth_request = isinstance(details, dict) and (
        details.get("url") == "https://rutracker.org/forum/login.php?token=PRIVATE"
        or details.get("response") == '<input name="login_password">'
    )
    assert len(net.calls) == (2 if needs_auth_request else 1)
    public = json.dumps(c.public())
    assert "private-session" not in public and "PRIVATE" not in public


def test_real_workflow_solver_failure_keeps_independent_results_and_csv(
    policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    policy = replace(solver_policy(policy, monkeypatch), result_limit=2, detail_mode="full")

    def respond(method: str, url: str, kw: dict[str, Any]) -> Any:
        if method == "GET":
            if url.startswith("https://rutracker.org/"):
                return (
                    200,
                    topic(artist="The Queen's Cartoonists", title="Mozart's Jazz Requiem"),
                    {"Content-Type": "text/html"},
                )
            if kw["params"]["offset"]:
                return 200, feed([])
            return 200, feed(
                [(1, "[CD] Queen - Jazz - 1978, FLAC"), (2, "The Queen's Cartoonists - Mozart's Jazz Requiem - 2023")]
            )
        if kw["json"]["url"].endswith("t=1"):
            return 500, {"status": "error", "message": "Timeout solving challenge"}
        return 200, solution(topic(artist="The Queen's Cartoonists", title="Mozart's Jazz Requiem").decode())

    net = Network(monkeypatch, respond)
    output = io.StringIO()
    path = tmp_path / "results.csv"
    assert (
        workflow(
            Query("", "Queen", "Jazz", "album"),
            policy,
            Jackett(policy),
            path,
            csv_only=True,
            input_stream=io.StringIO(),
            output=output,
        )
        == 0
    )
    import csv

    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["detail_status"] == "BLOCKED" and rows[0]["eligible"] == "True"
    assert rows[0]["codec"] == "FLAC" and rows[1]["eligible"] == "False"
    assert len(net.calls) == 4 and all("/torrents/" not in url for _, url, _ in net.calls)


@pytest.mark.parametrize("title", ["APE - Jazz", "Queen - FLAC", "(Queen) - Jazz"])
def test_artist_or_album_words_are_not_codec_claims(
    policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch, title: str
) -> None:
    c = title_candidate(policy, monkeypatch, title)
    assert c.artist is not None and c.release_title is not None
    assert c.codec is None and c.lossless is None


def test_independent_public_topic_survives_authenticated_challenge(
    policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = solver_policy(policy, monkeypatch)
    body = '<meta charset="windows-1251"><div class="post_body">Artist: Queen<br>Album: Jazz<br>Country: Япония<br>Catalog number: private-clearance<br>Label: private-session</div>'
    net = Network(
        monkeypatch,
        lambda method, url, kw: (
            (200, solution(body))
            if method == "POST"
            else (403, b"<title>Just a moment...</title>", {"Content-Type": "text/html", "cf-mitigated": "challenge"})
        ),
    )
    c = candidate()
    Jackett(policy).details(c)
    assert c.detail_status == "FOUND_PUBLIC" and c.country == "Япония"
    assert (c.artist, c.release_title) == ("Queen", "Jazz")
    assert c.detail_diagnostics["http_status"] == 403
    assert c.detail_diagnostics["selected_response"] == "public_browser"
    assert c.detail_diagnostics["selected_http_status"].startswith("UNKNOWN")
    assert "cf-mitigated: challenge" in c.detail_diagnostics["evidence"]
    assert len(net.calls) == 2
    assert "private-clearance" not in json.dumps(c.public()) and "private-session" not in json.dumps(c.public())


@pytest.mark.parametrize("body", ['<input name="login_password">', "<html>not a topic</html>"])
def test_public_fallback_cannot_use_login_or_unrecognized_page(
    policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    policy = solver_policy(policy, monkeypatch)
    Network(
        monkeypatch,
        lambda method, url, kw: (
            (200, solution(body)) if method == "POST" else (403, b"Forbidden", {"Content-Type": "text/html"})
        ),
    )
    c = candidate()
    Jackett(policy).details(c)
    assert c.detail_status == "BLOCKED" and c.artist is None


def test_public_success_cannot_hide_authenticated_login_failure(
    policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = solver_policy(policy, monkeypatch)
    Network(
        monkeypatch,
        lambda method, url, kw: (
            (200, solution()) if method == "POST" else (401, b"Unauthorized", {"Content-Type": "text/html"})
        ),
    )
    c = candidate()
    Jackett(policy).details(c)
    assert c.detail_status == "AUTHENTICATION_ERROR" and "selected_response" not in c.detail_diagnostics


def test_clearance_reused_without_cross_topic_body_leak(policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = solver_policy(policy, monkeypatch)
    net = Network(
        monkeypatch,
        lambda method, url, kw: (
            (200, solution())
            if method == "POST"
            else (200, topic(title="Jazz" if url.endswith("t=2") else "Other"), {"Content-Type": "text/html"})
        ),
    )
    adapter = Jackett(policy)
    a, b = candidate(), candidate("rutracker:3")
    adapter.details(a)
    adapter.details(b)
    assert a.release_title == "Jazz" and b.release_title == "Other"
    assert [method for method, _, _ in net.calls] == ["POST", "GET", "GET"]


@pytest.mark.parametrize(
    "change",
    [
        {"cookies": None},
        {"userAgent": "bad\nheader"},
        {"cookies": [{"name": "bad", "value": "secret\nvalue", "domain": "rutracker.org"}]},
    ],
)
def test_invalid_clearance_never_reaches_authenticated_request(
    policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    policy = solver_policy(policy, monkeypatch)
    net = Network(monkeypatch, lambda *args: (200, solution(**change)))
    c = candidate()
    Jackett(policy).details(c)
    assert c.detail_status == "INVALID_RESPONSE" and len(net.calls) == 1


def test_inline_login_widget_is_not_an_authentication_failure() -> None:
    c = candidate()
    parse_details(c, topic() + b'<form><input name="login_username"></form>')
    assert c.detail_status == "FOUND" and c.artist == "Queen"


def test_rejected_cached_clearance_recovers_once(policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = solver_policy(policy, monkeypatch)
    gets = 0

    def respond(method: str, url: str, kw: dict[str, Any]) -> Any:
        nonlocal gets
        if method == "POST":
            return 200, solution(url=candidate().source_url if gets == 0 else candidate("rutracker:3").source_url)
        gets += 1
        if gets == 2:
            return 403, b"Forbidden", {"Content-Type": "text/html"}
        return 200, topic(), {"Content-Type": "text/html"}

    net = Network(monkeypatch, respond)
    adapter = Jackett(policy)
    first, second = candidate(), candidate("rutracker:3")
    adapter.details(first)
    adapter.details(second)
    assert first.detail_status == second.detail_status == "FOUND"
    assert [m for m, _, _ in net.calls] == ["POST", "GET", "GET", "POST", "GET"]
    assert second.detail_diagnostics["cached_clearance_http_status"] == 403


def test_unreadable_forbidden_body_is_not_retried(policy: SearchPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    def chunks(self: requests.Response, chunk_size: int) -> Any:
        raise requests.ReadTimeout("private response error")
        yield b""

    net = Network(monkeypatch, lambda *args: (403, b"Forbidden", {"Content-Type": "text/html"}))
    monkeypatch.setattr(requests.Response, "iter_content", chunks)
    c = candidate()
    Jackett(policy).details(c)
    assert c.detail_status == "BLOCKED" and c.detail_diagnostics["body_read"] == "incomplete"
    assert len(net.calls) == 1 and "private response error" not in json.dumps(c.public())
