#!/usr/bin/env python3
"""Search and export release evidence; downloading always requires terminal confirmation."""

from __future__ import annotations

import argparse
import math
import re
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from pipeline_config import SearchPolicy, load_config
from search_evaluator import LLMEvaluator, Recommendation, build_evaluator, explain
from search_models import (
    Candidate,
    Query,
    SearchError,
    assess_relevance,
    export_csv,
    preference_order,
    public_text,
    resolve_research_query,
)
from search_providers import Jackett, SearchAdapter
from search_research import ForumResearch, Research
from search_torrent import Qbittorrent, client_settings


def validate_policy(policy: SearchPolicy) -> None:
    for name, maximum in (("result_limit", 100), ("page_size", 100), ("max_pages", 10)):
        value = getattr(policy, name)
        if type(value) is not int or not 1 <= value <= maximum:
            raise SearchError(f"search.{name} must be between 1 and {maximum}")
    if not math.isfinite(policy.timeout) or not 1 <= policy.timeout <= 60:
        raise SearchError("search.timeout must be finite and between 1 and 60 seconds")
    if not math.isfinite(policy.flaresolverr_timeout) or not 1 <= policy.flaresolverr_timeout <= 60:
        raise SearchError("search.flaresolverr_timeout must be finite and between 1 and 60 seconds")
    if policy.detail_mode not in {"targeted", "full"}:
        raise SearchError("search.detail_mode must be targeted or full")
    if type(policy.detail_budget) is not int or not 1 <= policy.detail_budget <= 100:
        raise SearchError("search.detail_budget must be between 1 and 100")
    if not math.isfinite(policy.detail_time_budget) or not 1 <= policy.detail_time_budget <= 3600:
        raise SearchError("search.detail_time_budget must be finite, 1..3600 seconds")
    if not math.isfinite(policy.detail_cache_ttl) or not 0 <= policy.detail_cache_ttl <= 86400:
        raise SearchError("search.detail_cache_ttl must be finite, 0..86400 seconds")
    if policy.research_backend not in {"none", "brave", "serper"}:
        raise SearchError("search.research_backend must be none, brave or serper")
    if policy.research_page_transport not in {"direct", "flaresolverr"}:
        raise SearchError("search.research_page_transport must be direct or flaresolverr")
    for name, bound in (("research_queries", 8), ("research_pages", 10)):
        if type(getattr(policy, name)) is not int or not 1 <= getattr(policy, name) <= bound:
            raise SearchError(f"search.{name} must be between 1 and {bound}")
    if not math.isfinite(policy.research_cache_ttl) or not 0 <= policy.research_cache_ttl <= 604800:
        raise SearchError("search.research_cache_ttl must be finite, 0..604800 seconds")
    if not all(
        re.fullmatch(r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+", d.strip()) for d in policy.research_domains.split(",")
    ):
        raise SearchError("search.research_domains must contain comma-separated public forum hostnames")
    if policy.backend not in {"none", "openai", "gemini", "local"}:
        raise SearchError("search.backend must be none, openai, gemini or local")
    for name, maximum in (
        ("research_posts_per_page", 200),
        ("mastering_max_sources", 100),
        ("mastering_batch_size", 100),
        ("mastering_output_tokens", 32768),
        ("research_openai_tool_calls", 10),
    ):
        if type(getattr(policy, name)) is not int or not 1 <= getattr(policy, name) <= maximum:
            raise SearchError(f"search.{name} must be between 1 and {maximum}")
    if policy.openai_api not in {"chat", "responses"}:
        raise SearchError("search.openai_api must be chat or responses")
    if policy.openai_reasoning_effort not in {"low", "medium", "high", "xhigh"}:
        raise SearchError("Invalid search.openai_reasoning_effort")
    for name in (
        "mastering_pipeline",
        "mastering_literal_recovery",
        "research_adaptive",
        "research_follow_pages",
        "research_openai_discovery",
    ):
        if type(getattr(policy, name)) is not bool:
            raise SearchError(f"search.{name} must be boolean")


def describe(candidate: Candidate, output: TextIO, *, compact: bool = False) -> None:
    print(f"{candidate.candidate_id}: {candidate.torrent_title}\n  Source: {candidate.source_url}", file=output)
    if compact:
        print(
            f"  Details={candidate.detail_status}; codec={candidate.codec or 'UNKNOWN'}; "
            f"medium={candidate.medium or 'UNKNOWN'}; catalog={candidate.catalog_number or 'UNKNOWN'}; "
            f"seeders={candidate.seeders if candidate.seeders is not None else 'UNKNOWN'}",
            file=output,
        )
        if candidate.recommendation_explanation:
            print("  Conditional recommendation: " + candidate.recommendation_explanation, file=output)
        return
    names = (
        "artist",
        "release_title",
        "release_type",
        "release_year",
        "original_album_year",
        "edition",
        "label",
        "catalog_number",
        "country",
        "mastering",
        "medium",
        "codec",
        "lossless",
        "bit_depth",
        "sample_rate",
        "channels",
        "complete",
        "disc_count",
        "size_bytes",
        "seeders",
        "leechers",
    )
    print(
        "  "
        + "; ".join(
            f"{name}={getattr(candidate, name) if getattr(candidate, name) is not None else 'UNKNOWN'}"
            for name in names
        ),
        file=output,
    )
    print(f"  Identity={candidate.identity_confidence}; detail status={candidate.detail_status}", file=output)
    if candidate.recommendation_explanation:
        print("  Recommendation: " + candidate.recommendation_explanation, file=output)
    print(
        f"  Evidence={candidate.evidence_completeness}; missing={', '.join(candidate.missing_information) or 'none listed'}",
        file=output,
    )
    for warning in candidate.warnings:
        if not warning.startswith("Topic acquisition: "):
            print("  Warning: " + public_text(warning), file=output)


def request_selection(
    candidates: list[Candidate],
    selected_id: str | None,
    policy: SearchPolicy,
    adapter: SearchAdapter,
    input_stream: TextIO,
    output: TextIO,
) -> int:
    if not input_stream.isatty() or not output.isatty():
        print(
            "Submission refused: an interactive terminal and explicit candidate confirmation are required.", file=output
        )
        return 2
    by_id = {c.candidate_id: c for c in candidates if c.eligible}
    if selected_id is None:
        print("Enter an eligible candidate ID (blank cancels): ", end="", file=output, flush=True)
        selected_id = input_stream.readline().strip()
        if not selected_id:
            print("Cancelled; nothing submitted.", file=output)
            return 0
    if selected_id not in by_id:
        raise SearchError("Selection is not an eligible candidate ID")
    selected = by_id[selected_id]
    base, destination = client_settings(policy)
    print("\nExact release proposed for submission:", file=output)
    describe(selected, output)
    print(f"  Client: {base}\n  Destination on client host: {destination}", file=output)
    print("  Superior mastering is not established; page properties are stated claims.", file=output)
    expected = "DOWNLOAD " + selected.candidate_id
    print(f"Type '{expected}' to confirm this release (anything else cancels): ", end="", file=output, flush=True)
    confirmation = input_stream.readline()
    if confirmation.rstrip("\r\n") != expected:
        print("Cancelled; nothing submitted.", file=output)
        return 0
    # Resolution/client authentication occur only AFTER confirmation. No model URLs.
    download = adapter.resolve_download(selected)
    outcome = Qbittorrent(policy).submit(selected, download)
    print(outcome, file=output)
    return 0


def workflow(
    query: Query,
    policy: SearchPolicy,
    adapter: SearchAdapter,
    csv_path: Path,
    *,
    csv_only: bool = False,
    submit: bool = False,
    selected_id: str | None = None,
    input_stream: TextIO,
    output: TextIO,
    researcher: ForumResearch | None = None,
) -> int:
    workflow_started = time.monotonic()
    started = time.monotonic()
    candidates = adapter.search(query, policy.result_limit, policy.page_size, policy.max_pages)
    jackett_seconds = time.monotonic() - started
    started = time.monotonic()
    for candidate in candidates:
        candidate.mastering_rank = None
        candidate.practical_rank = None
        candidate.mastering_group_id = None
        candidate.research_status = "disabled_csv_only" if csv_only else "not_requested"
        candidate.mastering_decision = {
            "status": "disabled_csv_only" if csv_only else "not_evaluated",
            "candidate_id": "",
        }
        candidate.search_diagnostics = getattr(adapter, "search_diagnostics", [])
        candidate.final_evaluation_status = (
            "disabled_csv_only"
            if csv_only
            else "disabled_backend_none"
            if policy.backend == "none"
            else "not_requested"
        )
        assess_relevance(candidate, query, policy, preliminary=True)
    filter_seconds = time.monotonic() - started
    eligible = preference_order(candidates, policy)
    print(
        f"Found {len(candidates)} topics; explicit mismatches {len(candidates) - len(eligible)}; detail candidates {len(eligible)}.",
        file=output,
        flush=True,
    )
    # Publication before any expensive optional operation preserves every discovery.
    export_csv(csv_path, candidates)
    preliminary_seconds = 0.0
    if policy.preliminary_llm and not csv_only and policy.backend != "none" and eligible:
        started = time.monotonic()
        try:
            preliminary = build_evaluator(policy).evaluate(query, eligible)
            ranked = {cid: rank for rank, cid in enumerate(preliminary.ranked_candidate_ids, 1)}
            for c in eligible:
                c.preliminary_evaluation_status = (
                    "abstained" if not preliminary.selected_candidate_id else "priority_only"
                )
                c.preliminary_rank = ranked.get(c.candidate_id)
                c.preliminary_explanation = (
                    " | ".join(explain(r, c) for r in preliminary.reasons if r["candidate_id"] == c.candidate_id)
                    or None
                )
            # A model can reorder priority, but cannot remove unranked candidates.
            eligible.sort(key=lambda c: ranked.get(c.candidate_id, 1000))
        except SearchError as exc:
            for c in eligible:
                c.preliminary_evaluation_status = "failed"
                c.preliminary_explanation = "Evaluation unavailable: " + public_text(exc)
            print("Preliminary evaluation unavailable; deterministic priority retained.", file=output)
        preliminary_seconds = time.monotonic() - started
    for rank, c in enumerate(eligible, 1):
        if c.preliminary_rank is None:
            c.preliminary_rank = rank
    detail_started = time.monotonic()
    checked = 0
    rate_limited = False
    ordered = eligible if policy.detail_mode == "targeted" else eligible + [c for c in candidates if not c.eligible]
    for c in candidates:
        if c not in ordered:
            c.detail_status = "SKIPPED_MISMATCH"
    for c in ordered:
        if rate_limited and policy.details:
            c.detail_status = "SKIPPED_RATE_LIMIT"
            c.warnings.append("Further detail requests stopped after rate limiting; retry later.")
            continue
        budget_exceeded = policy.detail_mode == "targeted" and checked >= policy.detail_budget
        time_exceeded = time.monotonic() - detail_started >= policy.detail_time_budget
        if policy.details and (budget_exceeded or time_exceeded):
            c.detail_status = "SKIPPED_TIME_BUDGET" if time_exceeded else "SKIPPED_BUDGET"
            c.warnings.append("Details not checked due to budget; potentially better edition remains unexamined.")
            continue
        print(
            f"Details {checked + 1}/{len(ordered)}: {c.candidate_id}; remaining {len(ordered) - checked - 1}",
            file=output,
            flush=True,
        )
        started = time.monotonic()
        adapter.details(c)
        rate_limited = c.detail_status == "RATE_LIMITED"
        c.timings["detail_total_seconds"] = round(time.monotonic() - started, 6)
        checked += 1
        # Ambiguous/missing summary evidence must survive to manual selection.
        c.selection_stage = "final"
        assess_relevance(
            c,
            query,
            policy,
            preliminary=c.title_parse_status != "parsed"
            and c.identity_confidence != "stated"
            and not all(c.evidence.get(n, {}).get("source") == "topic_detail" for n in ("artist", "release_title")),
        )
        c.selection_stage = "final"
        export_csv(csv_path, candidates)
    for c in candidates:
        c.timings.update(
            jackett_seconds=round(jackett_seconds, 6),
            preliminary_filter_seconds=round(filter_seconds, 6),
            preliminary_llm_seconds=round(preliminary_seconds, 6),
        )
    unexamined = sum(
        c.detail_status.startswith("SKIPPED_") and c.preliminary_status != "explicit_mismatch" for c in candidates
    )
    if unexamined:
        print(
            f"Warning: {unexamined} potential candidates remain unchecked; best edition may be among them. Inspect CSV skip reasons; use --full-details/increase budgets, or retry later after rate limiting.",
            file=output,
        )
    available = sum(c.detail_status in {"FOUND", "FOUND_PUBLIC"} for c in candidates)
    print(
        f"Details processed {checked}; descriptions available {available}; remaining potential {unexamined}.",
        file=output,
    )
    for c in candidates:
        c.selection_stage = "final"
    # Durable user output precedes optional evaluation; failure cannot erase discovery.
    export_csv(csv_path, candidates)
    for warning in adapter.warnings:
        print("Search warning: " + warning, file=output)
    print(
        f"Exported {len(candidates)} topics to {csv_path}; rejected {sum(not c.eligible for c in candidates)}.",
        file=output,
    )
    eligible = preference_order(candidates, policy)
    if not eligible and (csv_only or policy.research_backend == "none"):
        print("No eligible releases; nothing submitted. Inspect CSV rejection reasons/missing evidence.", file=output)
        return 0
    research: Research | None = None
    research_query = resolve_research_query(query, eligible)
    if not csv_only:
        print("Researching external mastering reviews (bounded queries/pages).", file=output, flush=True)
        started = time.monotonic()
        try:
            research = (researcher or ForumResearch(policy)).collect(research_query, candidates)
        except SearchError as exc:
            research = Research(status="UNAVAILABLE", warnings=["Внешние отзывы не проверены: " + public_text(exc)])
        for c in candidates:
            c.research_diagnostics = research.diagnostics
            c.research_status = research.status
            c.external_sources = research.sources
            c.mastering_decision = {"status": "not_evaluated", "candidate_id": "", "research_status": research.status}
            c.timings["research_seconds"] = round(time.monotonic() - started, 6)
        for warning in research.warnings:
            print(warning, file=output)
        export_csv(csv_path, candidates)
    recommendation: Recommendation | None = None
    if not csv_only and policy.backend != "none":
        started = time.monotonic()
        for c in candidates:
            c.final_evaluation_status = "pending" if c.eligible else "ineligible"
        try:
            evaluator = build_evaluator(policy)
            if policy.mastering_pipeline and isinstance(evaluator, LLMEvaluator) and research and research.sources:
                recommendation = evaluator.evaluate_mastering(research_query, candidates, research)
            else:
                recommendation = (
                    evaluator.evaluate(query, candidates, research=research)
                    if research and research.sources
                    else evaluator.evaluate(query, candidates)
                )
        except SearchError as exc:
            print("Evaluation unavailable: " + str(exc) + ". CSV and manual selection remain available.", file=output)
            for c in candidates:
                if c.eligible:
                    c.final_evaluation_status = "failed"
        finally:
            for c in candidates:
                c.timings["final_llm_seconds"] = round(time.monotonic() - started, 6)
            export_csv(csv_path, candidates)
        if recommendation:
            if recommendation.mastering_review:
                review = recommendation.mastering_review
                review["practical_candidate_id"] = recommendation.selected_candidate_id
                for c in candidates:
                    c.mastering_decision = review
                decision_label = {
                    "preference": "условное предпочтение",
                    "no_common_preference": "единого предпочтения нет",
                    "insufficient_evidence": "воздержание: недостаточно свидетельств",
                }[review["status"]]
                if review.get("decision_status") == "failed":
                    decision_label = "воздержание: сервис не завершил проверяемый выбор"
                print("Предпочтение по найденным отзывам: " + decision_label, file=output)
                if review["status"] == "preference":
                    print("  Издание: " + public_text(review["preferred_edition"]), file=output)
                    if review.get("listening_scope") == "multichannel_or_mixed":
                        print(
                            "  Область отзыва: многоканальный звук или смешанное обсуждение; стерео-предпочтение не установлено.",
                            file=output,
                        )
                    print(
                        "  Раздача: "
                        + (
                            review["candidate_id"]
                            or (
                                "среди кандидатов не найдено"
                                if review["mapping_status"] == "not_found"
                                else "не удалось сопоставить: неоднозначность или недостающие идентификаторы"
                            )
                        ),
                        file=output,
                    )
                    print("  Сопоставление: " + review["match_confidence"], file=output)
                print("  Статус сопоставления: " + review["mapping_status"], file=output)
                coverage = review.get("extraction_coverage", {})
                if coverage:
                    counts = coverage["source_status_counts"]
                    print(
                        f"  Учёт сообщений: с принятыми отзывами {counts.get('accepted', 0)}; "
                        f"без извлечённого отзыва {counts.get('no_opinion_proposed', 0)}; "
                        f"с отклонёнными отзывами {counts.get('opinion_rejected', 0)}; "
                        f"с ошибкой обработки {counts.get('request_failed', 0)}; "
                        f"не обработано {counts.get('not_attempted', 0) + counts.get('not_selected', 0)}.",
                        file=output,
                    )
                    if coverage.get("literal_opinion_count"):
                        print(
                            f"  Сохранено явных предпочтений по исходному тексту: {coverage['literal_opinion_count']}.",
                            file=output,
                        )
                for ref in review.get("identity_reference_candidates", [])[:5]:
                    print(
                        f"  Кандидат для проверки связи с отзывом: {ref['candidate_id']} — каталог {public_text(ref['catalog_number'])}; связь изданий не подтверждена.",
                        file=output,
                    )
                for comparison in review["comparisons"]:
                    print(
                        f"  {comparison['stance']}: {public_text(comparison['quote'])} — {comparison['url']}",
                        file=output,
                    )
                    if comparison.get("preference_basis_quote"):
                        print(
                            "    Основание личного списка: " + public_text(comparison["preference_basis_quote"]),
                            file=output,
                        )
                for endorsement in review.get("endorsements", [])[:5]:
                    print(
                        "  Положительный отзыв без сравнительного предпочтения: "
                        + public_text(endorsement["quote"])
                        + " — "
                        + endorsement["url"],
                        file=output,
                    )
                print(
                    "  Поддерживающих аккаунтов: "
                    + str(review["distinct_accounts"])
                    + "; независимость и консенсус не установлены.",
                    file=output,
                )
                print("  Ограничения: " + ", ".join(review["limitations"]), file=output)
                if review.get("comparative_explanation"):
                    print("  Обоснование: " + public_text(review["comparative_explanation"]), file=output)
                print(
                    "  Практический вариант для скачивания (отдельно): "
                    + (recommendation.selected_candidate_id or "воздержание"),
                    file=output,
                )
            elif research:
                for c in candidates:
                    c.mastering_decision = {
                        "status": "reviews_unavailable",
                        "candidate_id": "",
                        "practical_candidate_id": recommendation.selected_candidate_id,
                    }
                print(
                    "Предпочтение мастеринга не установлено: внешние отзывы не проверены/недостаточны. Ниже только практические варианты.",
                    file=output,
                )
            for c in candidates:
                if c.eligible:
                    c.final_evaluation_status = (
                        "abstained" if not recommendation.selected_candidate_id else "evaluated_unranked"
                    )
            if not recommendation.selected_candidate_id:
                print("Evaluator abstained: insufficient evidence; manual selection remains available.", file=output)
            by_id = {c.candidate_id: c for c in eligible}
            for rank, cid in enumerate(recommendation.ranked_candidate_ids, 1):
                candidate = by_id[cid]
                candidate.final_evaluation_status = "conditional_recommendation"
                candidate.recommendation_rank = rank
                candidate.recommendation_explanation = " | ".join(
                    explain(reason, candidate) for reason in recommendation.reasons if reason["candidate_id"] == cid
                )
                if candidate.practical_rank is None:
                    candidate.practical_rank = rank
            eligible = [by_id[cid] for cid in recommendation.ranked_candidate_ids] + [
                c for c in eligible if c.candidate_id not in recommendation.ranked_candidate_ids
            ]
            print(
                "Evaluator uncertainty: " + ", ".join(recommendation.uncertainty + recommendation.warnings), file=output
            )
            export_csv(csv_path, candidates)
    if recommendation is None:
        if not csv_only:
            print("Предпочтение мастеринга не установлено; CSV и ручной выбор доступны.", file=output)
        print(
            "Manual selection available. Listed order follows stated identity/completeness, lossless/rip preferences, then availability; it is not a sound-quality measurement.",
            file=output,
        )
    if recommendation and recommendation.mastering_review and "evidence_graph" in recommendation.mastering_review:
        review = recommendation.mastering_review
        primary_ids = review.get("matching_candidate_ids", [])
        print("Раздачи рекомендуемого мастеринга:", file=output)
        for candidate in eligible:
            if candidate.candidate_id in primary_ids:
                describe(candidate, output, compact=True)
        if not primary_ids:
            print("  Подтверждённое сопоставление не найдено; автоматический выбор раздачи отсутствует.", file=output)
        print("Прочие практические варианты (предпочтение мастеринга для них не установлено):", file=output)
        eligible = [c for c in eligible if c.candidate_id not in primary_ids]
    print("Eligible alternatives (up to five; full evidence and unknowns are in CSV):", file=output)
    for candidate in eligible[:5]:
        describe(candidate, output, compact=True)
    for candidate in candidates:
        candidate.timings["search_total_seconds"] = round(time.monotonic() - workflow_started, 6)
        candidate.timings.setdefault("final_llm_seconds", 0.0)
    export_csv(csv_path, candidates)
    print(
        f"Timing: Jackett {jackett_seconds:.2f}s; filter {filter_seconds:.3f}s; details {sum(c.timings.get('detail_total_seconds', 0.0) for c in candidates):.2f}s; preliminary LLM {preliminary_seconds:.2f}s; final LLM {candidates[0].timings['final_llm_seconds'] if candidates else 0.0:.2f}s.",
        file=output,
    )
    if submit:
        # A recommendation never supplies default consent or even default selection.
        return request_selection(candidates, selected_id, policy, adapter, input_stream, output)
    return 0


def parser(prog: str | None = None) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog=prog,
        description="Search RuTracker through its configured Jackett indexer. Metadata is stated, unmeasured evidence; no automatic downloads.",
    )
    result.add_argument("query", nargs="*", help="Free text, e.g. Queen Jazz")
    result.add_argument("--artist", default="")
    result.add_argument("--title", "--album", default="")
    result.add_argument("--type", choices=("album", "single"), default="")
    result.add_argument("--limit", type=int, help="Topic bound (1..100)")
    result.add_argument("--page-size", type=int, help="Results per search page (1..100)")
    result.add_argument("--max-pages", type=int, help="Request-page bound (1..10); pagination support varies")
    result.add_argument("--csv", type=Path, default=Path("search-results.csv"))
    result.add_argument(
        "--csv-only",
        action="store_true",
        help="Discover/details/export only: no external review search, LLM or client calls",
    )
    result.add_argument(
        "--detail-budget", type=int, help="Targeted detail count budget (default 5); all discoveries remain in CSV"
    )
    result.add_argument(
        "--detail-time-budget",
        type=float,
        help="Deadline to start detail requests (default 180s); an in-flight bounded request may finish later",
    )
    result.add_argument(
        "--full-details",
        action="store_true",
        help="Attempt every discovered topic without early reduction/count cap; time budget still applies",
    )
    result.add_argument(
        "--preliminary-llm",
        action="store_true",
        help="Optional additional LLM call to prioritize details; never excludes candidates; CSV-only makes no LLM calls",
    )
    result.add_argument(
        "--no-research",
        action="store_true",
        help="Do not search external reviews; mastering preference remains unverified",
    )
    result.add_argument("--research-queries", type=int, help="Total external search query budget (1..8)")
    result.add_argument(
        "--research-pages", type=int, help="Total forum HTTP request budget, including redirects (1..10)"
    )
    result.add_argument("--refresh-details", action="store_true", help="Ignore successful detail cache for this run")
    result.add_argument("--no-details", action="store_true", help="Export summaries only, explicitly marked incomplete")
    result.add_argument(
        "--backend",
        choices=("none", "gemini", "openai", "local"),
        help="Remote evaluation discloses normalized query/evidence; requires configuration opt-in and explicit model",
    )
    result.add_argument(
        "--submit",
        action="store_true",
        help="Prompt for candidate and explicit terminal confirmation before client submission",
    )
    result.add_argument(
        "--select", help="Eligible candidate ID to display for confirmation; does not approve downloading"
    )
    return result


def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    # Same environment precedence as enrichment; never change global configuration.
    from audiophile_tagger import load_env_file

    load_env_file()
    arguments = parser(prog)
    args = arguments.parse_args(argv)
    if args.csv_only and args.submit:
        arguments.error("--csv-only is incompatible with --submit")
    if args.select and not args.submit:
        arguments.error("--select requires --submit; selection always requires confirmation")
    query = Query(" ".join(args.query).strip(), args.artist.strip(), args.title.strip(), args.type)
    if not query.search_text or len(query.search_text) > 500:
        arguments.error("provide a query/artist/title of at most 500 characters")
    try:
        policy = load_config().search
        overrides = {
            name: value
            for name, value in (
                ("result_limit", args.limit),
                ("page_size", args.page_size),
                ("max_pages", args.max_pages),
                ("backend", args.backend),
                ("detail_budget", args.detail_budget),
                ("detail_time_budget", args.detail_time_budget),
                ("research_queries", args.research_queries),
                ("research_pages", args.research_pages),
            )
            if value is not None
        }
        if args.no_research:
            overrides["research_backend"] = "none"
        if args.full_details:
            overrides["detail_mode"] = "full"
        if args.preliminary_llm:
            overrides["preliminary_llm"] = True
        if args.refresh_details:
            overrides["detail_cache_ttl"] = 0.0
        if args.no_details:
            overrides["details"] = False
        policy = replace(policy, **overrides)
        validate_policy(policy)
        return workflow(
            query,
            policy,
            Jackett(policy),
            args.csv,
            csv_only=args.csv_only,
            submit=args.submit,
            selected_id=args.select,
            input_stream=sys.stdin,
            output=sys.stdout,
        )
    except KeyboardInterrupt:
        print(
            "Cancelled. If interrupted during submission, its pending receipt prevents an automatic retry.",
            file=sys.stderr,
        )
        return 130
    except (SearchError, OSError, ValueError, TypeError) as exc:
        # Config/OS errors may include paths; sanitize instead of logging raw exceptions.
        print("Search failed: " + public_text(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
