"""Conservative checks of reported preferences, independent of response formatting."""

from __future__ import annotations

import re

PREFERENCE = r"\b(?:prefer(?:s|red|ring|ed)?|better|best|favo[u]?rite|superior|edges? out|beats?|going with|voted for|my (?:first choice|pick|go[ -]to))\b"
POSITIVE = r"\b(?:love|like|recommend\w*)\b"
NEGATIVE = r"\b(?:avoid\w*|worse|worst|inferior|dislike\w*|disappoint\w*|(?:no|not) good|(?:don['’]?t|didn['’]?t|do not|did not) (?:like|care for)|(?:wasn['’]?t|was not) impressed)\b"
NEGATION = r"\b(?:not|never|no|neither|don['’]?t|doesn['’]?t|didn['’]?t|isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|haven['’]?t|hasn['’]?t|can['’]?t|cannot|wouldn['’]?t)\b"
DIRECT = r"\b(?:compared directly|direct comparison|a/b|ab comparison|side by side|level[ -]matched|matched levels|I(?:['’]ve| have)?(?: (?:also|recently|just))? compared|I (?:have |had )?shot out|I (?:did|performed) a shoot[ -]?out|I (?:used|own|have|bought|ripped|applied|corrected|de-emphasized|deemphasized)\b[^.!?\n]{0,240}?\band compared|listening sessions of comparing)\b"


def edition_mentions(text: str, edition: str) -> list[re.Match[str]]:
    """SACD is not the SACD suffix in SHM-SACD; catalogue punctuation is literal."""
    if not edition:
        return []
    matches = list(re.finditer(r"(?<![\w-])" + re.escape(edition) + r"(?![\w-])", text, re.IGNORECASE))
    if edition.upper() in {"CD", "SACD"}:
        matches = [m for m in matches if not re.search(r"(?i)\b(?:SHM|UHQ|Blu[ -]spec)\s+$", text[: m.start()])]
    return matches


def opinion_parts(text: str, edition: str = "") -> list[tuple[int, int, str]]:
    """Offsets into original prose; do not split a literal name such as U.S."""
    protected = edition_mentions(text, edition)
    start = 0
    result = []
    for match in re.finditer(r"[;.!?\n]|\bbut\b", text, re.IGNORECASE):
        if any(m.start() <= match.start() < m.end() for m in protected):
            continue
        result.append((start, match.start(), match.group()))
        start = match.end()
    result.append((start, len(text), ""))
    return result


def _negated(prefix: str) -> bool:
    """Bind negation to its local predicate, not unrelated sentences/relative clauses."""
    for m in re.finditer(NEGATION, prefix, re.IGNORECASE):
        tail = prefix[m.end() :]
        if re.search(r"[.!?;]|\b(?:which|but|because)\b", tail, re.IGNORECASE):
            continue
        if len(tail.split()) <= 4:
            return True
    return False


def reported_stance(text: str, edition: str, stance: str) -> bool:
    """Necessary lexical checks; endorsement never establishes a comparative winner."""
    if stance == "context":
        return True
    if stance not in {"prefer", "against", "endorse"} or not edition:
        return False
    marker = PREFERENCE if stance == "prefer" else NEGATIVE if stance == "against" else POSITIVE
    if stance == "endorse" and reported_stance(text, edition, "prefer"):
        return False
    parts = opinion_parts(text, edition)
    for index, (begin, end, terminal) in enumerate(parts):
        clause = text[begin:end]
        if terminal == "?":
            continue
        mentions = [(m.start(), m.end()) for m in edition_mentions(clause, edition)]
        if not mentions and index:
            previous = text[parts[index - 1][0] : parts[index - 1][1]]
            subject = re.search(
                r"(?<![\w-])" + re.escape(edition) + r"\s+(?:is|was|sounds|remains)\b", previous, re.IGNORECASE
            )
            # Only the immediately preceding explicit subject can carry to "it".
            # "I own A and B. It is better" and an opponent after "than" cannot.
            if subject and not re.search(
                r"(?i)\b(?:than|over|versus|vs|someone|he|she|they|people|others|if|might|may|could|would)\b",
                previous[: subject.start()],
            ):
                mentions = [
                    (m.start(), m.end()) for m in re.finditer(r"(?i)\bit(?=\s+(?:is|was|sounds|remains)\b)", clause)
                ]
        for start, mention_end in mentions:
            for indicator in re.finditer(marker, clause, re.IGNORECASE):
                if stance == "prefer" and re.match(
                    r"(?i)best\s+(?:bang\s+for\s+(?:the|your)\s+buck|value|price|deal|buy)\b",
                    clause[indicator.start() :],
                ):
                    continue
                if re.fullmatch(r"edges? out|beats?", indicator.group(0), re.IGNORECASE) and start > indicator.start():
                    continue
                prefix = clause[max(0, indicator.start() - 85) : indicator.start()]
                if stance == "prefer" and re.search(r"(?i)\b(?:least|less)\s*$", prefix):
                    continue
                if _negated(re.sub(re.escape(edition), "", prefix, flags=re.IGNORECASE)):
                    continue
                if re.search(
                    r"\b(?:someone|they|he|she|people|others|reportedly|apparently|says?|said|told|claims?|claimed|according to)\b",
                    prefix,
                    re.IGNORECASE,
                ):
                    continue
                if re.search(
                    r"\b(?:if|might|may|could|would|wish|wonder|whether|used to|up until recently|previously|formerly|do you|does anyone|are you|did you|have you|is anyone)\b",
                    prefix,
                    re.IGNORECASE,
                ):
                    continue
                if start > indicator.start() and "," in clause[indicator.end() : start]:
                    continue
                if start < indicator.start() and "," in clause[mention_end : indicator.start()]:
                    gap = clause[mention_end : indicator.start()]
                    continuation = re.fullmatch(
                        r"(?is)[^,]+,\s*and\s+(?:(?:even|still|much|clearly|slightly)\s+)*", gap
                    ) and re.match(
                        r"(?i)\s*(?:the\s+)?" + re.escape(edition) + r"\s+(?:is|was|sounds|remains)\b", clause
                    )
                    if not continuation and not re.search(r",\s*which\b", gap, re.IGNORECASE):
                        continue
                if (
                    re.match(r"(?:prefer|recommend|love|like)", indicator.group(0), re.IGNORECASE)
                    and start < indicator.start()
                ):
                    between = clause[mention_end : indicator.start()]
                    if not re.fullmatch(r"\s+(?:is|are)(?:\s+(?:still|clearly))?\s+", between, re.IGNORECASE):
                        continue
                separators = re.finditer(r"\b(?:over|than|to|versus|vs\.?|rather than)\b", clause, re.IGNORECASE)
                if any(indicator.start() < sep.start() < start for sep in separators):
                    continue
                tail = clause[indicator.end() :]
                if stance == "prefer" and re.search(
                    r"(?i)\b(?:according to|by (?:other\s+)?(?:people|listeners|members|posters|users|others|many|most|them|him|her)|in (?:this|the) poll)\b",
                    tail,
                ):
                    continue
                if re.search(
                    r"\b(?:less|least|not really|only on paper|best avoided)\b",
                    indicator.group(0) + " " + tail,
                    re.IGNORECASE,
                ):
                    continue
                return True
    return False


def reports_direct_comparison(text: str) -> bool:
    for match in re.finditer(DIRECT, text, re.IGNORECASE):
        prefix = re.split(r"[.!?;\n]", text[max(0, match.start() - 80) : match.start()])[-1]
        if not _negated(prefix):
            return True
    return False
