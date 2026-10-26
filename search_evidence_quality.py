"""Work scope and provenance checks for retained original forum evidence."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from search_models import Query, norm, public_evidence_url


def literal_term(text: str, term: str) -> bool:
    return bool(norm(term)) and (" " + norm(term) + " ") in (" " + norm(text) + " ")


def artist_terms(artist: str) -> tuple[str, ...]:
    # A documented spelling in this corpus; do not strip The from arbitrary bands.
    return (artist, "Beatles") if norm(artist) == "the beatles" else (artist,)


def album_terms(query: Query) -> tuple[str, ...]:
    artist = norm(query.artist)
    key = ("the beatles" if artist == "beatles" else artist, norm(query.title))
    aliases = {
        ("the beatles", "the beatles"): ("White Album", "The White Album", "The Beatles (1968)"),
        ("led zeppelin", "led zeppelin"): ("Led Zeppelin I", "Led Zeppelin 1", "LZ I", "LZI"),
        ("the beatles", "sgt pepper s lonely hearts club band"): ("Sgt. Pepper", "Sgt. Pepper's"),
        ("pink floyd", "the dark side of the moon"): ("Dark Side of the Moon", "DSOTM"),
        ("pink floyd", "wish you were here"): ("WYWH",),
    }
    if key in {("the beatles", "the beatles"), ("led zeppelin", "led zeppelin")}:
        # A band-name match alone cannot identify its self-titled album.
        return aliases[key]
    return (query.title, *aliases.get(key, ()))


def work_title_present(query: Query, text: str) -> bool:
    return any(literal_term(text, t) for t in album_terms(query) if t)


def declared_ai_summary(source: dict[str, Any]) -> bool:
    text = str(source.get("full_text") or source.get("album_context") or source.get("excerpt", ""))
    return bool(
        re.search(
            r"(?is)^\s*(?:Got this from.{0,90}\b(?:AI|ChatGPT|Gemini)\b|(?:This|The following|Here).{0,90}(?:generated|written|produced) by.{0,30}\b(?:AI|ChatGPT|Gemini)\b)",
            text,
        )
    )


def declared_republication(source: dict[str, Any]) -> bool:
    text = str(source.get("full_text") or source.get("album_context") or source.get("excerpt", ""))
    return bool(re.search(r"(?is)^\s*(?:Here['’]s|Here is)\s+an?\s+excerpt\b.{0,160}\b(?:interview|article)\b", text))


def scoped_text(source: dict[str, Any]) -> str:
    if source.get("context_parser_version") == 7:
        return str(source.get("album_context", ""))
    return str(source.get("album_context") or source.get("excerpt", ""))


def thread_key(source: dict[str, Any]) -> str:
    url = public_evidence_url(source.get("url"), ("forums.stevehoffman.tv",))
    if not url:
        return ""
    path = urlsplit(url).path
    match = re.fullmatch(r"(/threads/[^/]+\.\d+)(?:/page-\d+)?/?", path)
    return match.group(1) if match else ""


def identity_span(source: dict[str, Any], query: Query) -> str:
    """A literal slice of this author's scoped prose, never a search snippet/quote."""
    text = scoped_text(source)

    def occurrences(terms: tuple[str, ...]) -> list[re.Match[str]]:
        result: list[re.Match[str]] = []
        for term in terms:
            words = norm(term).split()
            if words:
                pattern = r"(?<!\w)" + r"\W+".join(re.escape(w) for w in words) + r"(?!\w)"
                result.extend(re.finditer(pattern, text, re.IGNORECASE))
        return result

    pairs = [
        (min(a.start(), b.start()), max(a.end(), b.end()))
        for a in occurrences(artist_terms(query.artist))
        for b in occurrences(album_terms(query))
    ]
    if not pairs:
        return ""
    start, end = min(pairs, key=lambda pair: pair[1] - pair[0])
    return text[start:end] if end - start <= 1600 else ""


def album_thread(source: dict[str, Any], query: Query) -> bool:
    title = str(source.get("page_title", ""))
    return bool(
        thread_key(source)
        and query.artist
        and query.title
        and norm(query.title) not in {"jazz", "live", "home", "love", "greatest hits", "best of", "anthology"}
        and work_title_present(query, title)
        and not re.search(r"(?i)\b(?:catalogue|catalog|discography|discographies|albums|summary thread)\b", title)
    )


def source_relevance(
    source: dict[str, Any], query: Query, *, thread_sources: list[dict[str, Any]] | None = None
) -> str:
    if source.get("page_status") != "FOUND":
        return "page_unread"
    if source.get("scope") != "post_excerpt":
        return "neighbor_context_only"
    if declared_ai_summary(source):
        return "declared_ai_summary"
    if declared_republication(source):
        return "republished_commentary"
    text = (
        str(source.get("album_context", ""))
        if source.get("context_parser_version") == 7
        else str(source.get("album_context") or source.get("excerpt", ""))
    )
    combined = str(source.get("page_title", "")) + " " + text
    if query.artist and not any(literal_term(combined, t) for t in artist_terms(query.artist)):
        verified = album_thread(source, query) and any(
            thread_key(anchor) == thread_key(source)
            and album_thread(anchor, query)
            and source_relevance(anchor, query) == "usable"
            and identity_span(anchor, query)
            for anchor in (thread_sources or [])
        )
        if not verified:
            return "artist_context_missing"
    if query.title:
        if not work_title_present(query, combined):
            return "album_context_missing"
        # An explicit reply about another work overrides the thread's album title.
        if not work_title_present(query, text):
            subjects = re.findall(r"(?i)\b(?:for|regarding|referring to|as for)\s+[\"“]([^\"”]+)[\"”]", text)
            if any(not work_title_present(query, s) for s in subjects):
                return "different_work_in_post"
    elif query.text and not literal_term(combined, query.text):
        return "album_context_missing"
    complete = source.get("album_context_complete", source.get("context_complete", False))
    if not complete and not (query.title and work_title_present(query, text)):
        return "incomplete_context_without_album"
    return "usable"


class SourceScope:
    """Thread identity comes only from retained original posts in this research set."""

    def __init__(self, sources: list[dict[str, Any]], query: Query) -> None:
        self.query = query
        self.anchors: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            if (
                album_thread(source, query)
                and source_relevance(source, query) == "usable"
                and identity_span(source, query)
            ):
                self.anchors.setdefault(thread_key(source), []).append(source)

    def reason(self, source: dict[str, Any]) -> str:
        return source_relevance(source, self.query, thread_sources=self.anchors.get(thread_key(source), []))

    def identity_evidence(self, source: dict[str, Any]) -> list[dict[str, str]]:
        if source_relevance(source, self.query) != "artist_context_missing" or self.reason(source) != "usable":
            return []
        return [
            {"source_id": s["source_id"], "quote": identity_span(s, self.query), "url": s["url"]}
            for s in self.anchors.get(thread_key(source), [])[:2]
        ]


def choice_list_intro(source: dict[str, Any], query: Query) -> str:
    """Carry only an explicit list-choice heading; never neighboring sonic adjectives."""
    full = source.get("full_text")
    context = source.get("album_context")
    if not isinstance(full, str) or not isinstance(context, str) or "[TRUNCATED" in full:
        return ""
    intro = re.match(r"\s*(My choice:|My preferred (?:CD )?versions:|My favorites:)\s*\n\s*\n", full, re.IGNORECASE)
    if not intro or source.get("scope") != "post_excerpt" or source.get("page_status") != "FOUND":
        return ""
    # The requested row must actually open its retained block. A later mention in
    # another album's description does not inherit the list heading.
    words = norm(context).split()
    if not any(words[: len(norm(t).split())] == norm(t).split() for t in album_terms(query) if norm(t)):
        return ""
    return intro.group(1)


def source_caveats(source: dict[str, Any], edition: str = "") -> list[str]:
    text = str(source.get("full_text") or source.get("album_context") or source.get("excerpt", ""))
    flags = []
    scoped = str(source.get("album_context") or source.get("excerpt", ""))
    if re.search(r"(?i)\b(?:MC mix|multichannel|multi-channel|surround|quadraphonic)\b|\b5\.1\b", scoped):
        flags.append("multichannel_context")
    if re.search(r"(?i)\bon my system\b|\bjust my opinion\b", text):
        flags.append("system_specific_opinion")
    if re.search(r"(?i)\bI guess\b", text):
        flags.append("tentative_opinion")
    if re.search(r"(?i)\bif I cared\b|\bwould be my\b", text):
        flags.append("conditional_opinion")
    if declared_ai_summary(source):
        flags.append("declared_ai_summary")
    if not source.get("album_context_complete", source.get("context_complete", False)):
        flags.append("incomplete_post_context")
    if edition:
        for sentence in re.split(r"[.!?\n]", text):
            if (
                edition.casefold() in sentence.casefold()
                and re.search(r"(?i)\b(?:up until recently|used to|previously|formerly|no longer)\b", sentence)
                and re.search(r"(?i)prefer|favo[u]?rite", sentence)
            ):
                flags.append("historical_preference")
                break
    return flags
