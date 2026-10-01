#!/usr/bin/env python3

'''
@File:   search_service.py

@Brief:  Execute multi-provider search, store papers, log provenance.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

import time

from django.utils import timezone

# Custom library Imports
from literature.models import Discovery, Paper, ScreeningDecision, Search, SearchRun
from literature.providers.arxiv import ArxivConfig, ArxivSource
from literature.providers.crossref import CrossrefConfig, CrossrefSource
from literature.providers.openalex import OpenAlexSource, SearchConfig
from literature.providers.semantic_scholar import (
    SemanticScholarConfig,
    SemanticScholarSource,
)
from literature.services.dedup_service import find_duplicate, merge_sources
from literature.services.reference_service import enrich_papers
from literature.services.screening_service import (
    abstract_available,
    has_reference_list,
    title_ready,
)

# ------------------------------------------------------------------------ #
# Search execution
# ------------------------------------------------------------------------ #

# --- Store one provider batch ---
def _store_batch(
    *,
    search: Search,
    source_key: str,
    run: SearchRun,
    items: list,
    title_stage,
    query_for_provenance: str,
    create_decisions: bool = True,
) -> tuple[int, int, int, list[int]]:
    """Deduplicate + persist batch. Returns (new, dups, no_abstract, touched).

    Every returned paper is stored with provenance (complete database).
    When create_decisions is False, queue rows are left to gate_papers
    so enrichment can complete papers first.
    """
    new_count = 0
    dup_count = 0
    no_abs_count = 0
    touched: list[int] = []
    for item in items:
        paper_data = dict(item.paper_data)
        existing, _match = find_duplicate(paper_data)
        if existing is None:
            paper = Paper.objects.create(
                openalex_id=paper_data.get("openalex_id"),
                title=paper_data.get("title") or "",
                authors=paper_data.get("authors") or [],
                abstract=paper_data.get("abstract") or "",
                publication_year=paper_data.get("publication_year"),
                publication_date=paper_data.get("publication_date"),
                venue=paper_data.get("venue") or "",
                publisher=paper_data.get("publisher") or "",
                doi=paper_data.get("doi"),
                urls=paper_data.get("urls") or [],
                keywords=paper_data.get("keywords") or [],
                citation_count=paper_data.get("citation_count") or 0,
                references=paper_data.get("references") or [],
                source_ids=paper_data.get("source_ids") or {},
                sources=paper_data.get("sources") or [source_key],
                raw=paper_data.get("raw") or {},
            )
            new_count += 1
        else:
            paper = existing
            merge_sources(paper, paper_data)
            dup_count += 1
        Discovery.objects.get_or_create(
            paper=paper,
            source=source_key,
            search=search,
            defaults={
                "search_run": run,
                "query": query_for_provenance,
                "raw_payload": item.raw_response,
            },
        )
        if not abstract_available(paper.abstract):
            no_abs_count += 1
        if create_decisions and title_stage is not None and title_ready(paper):
            ScreeningDecision.objects.get_or_create(
                paper=paper,
                stage=title_stage,
                defaults={"decision": ScreeningDecision.Decision.PENDING},
            )
        if paper.pk not in touched:
            touched.append(paper.pk)
    return new_count, dup_count, no_abs_count, touched


# --- Completeness gate ---
def is_complete(paper: Paper) -> bool:
    """Full metadata completeness, not a prerequisite for Title admission."""
    return bool(
        (paper.title or "").strip()
        and abstract_available(paper.abstract)
        and has_reference_list(paper)
    )


# --- Gate touched papers into the screening queue ---
def gate_papers(papers: list[Paper], title_stage) -> int:
    """Admit all titled papers; report only missing titles as unqueueable."""
    incomplete = 0
    for paper in papers:
        if not title_ready(paper):
            incomplete += 1
            continue
        if title_stage is not None:
            ScreeningDecision.objects.get_or_create(
                paper=paper,
                stage=title_stage,
                defaults={"decision": ScreeningDecision.Decision.PENDING},
            )
    return incomplete


# --- Run one provider ---
def _run_provider(
    *,
    search: Search,
    source_key: str,
    derived_query: str,
    keywords: list,
    venues: list,
    author_names: list,
    max_pages: int,
    title_stage,
) -> tuple[SearchRun, list[int]]:
    """Execute a single provider. Returns (run, touched paper pks).

    Decisions are deferred: run_search enriches incomplete papers first,
    then gates them into the queue. OpenAlex/Crossref/arXiv get one
    OR-joined fetch (1 request) to stay inside rate limits. Semantic
    Scholar gets one plain fetch per row because its query syntax
    ignores OR (OR-joined returns 0 hits).
    """
    rows = [str(k).strip() for k in keywords if str(k).strip()]

    # --- Escape one row for OR embedding ---
    def _escape_row(term: str) -> str:
        """Escape backslashes/quotes so rows never break OR syntax."""
        return term.replace("\\", "\\\\").replace('"', '\\"')

    or_query = " OR ".join(
        f'"{_escape_row(r)}"' if " " in r else _escape_row(r) for r in rows
    )
    query = or_query or derived_query or search.query
    run = SearchRun.objects.create(
        search=search,
        query_used=query,
        source=source_key,
        status=SearchRun.Status.RUNNING,
    )
    try:
        results: list = []
        if source_key == "openalex":
            source = OpenAlexSource()
            config = SearchConfig(
                query=query,
                year_min=search.year_min,
                year_max=search.year_max,
                keywords=tuple(keywords),
                venues=tuple(venues),
                author_names=tuple(author_names),
                max_pages=max_pages,
            )
            results, _total = source.fetch(config)
        elif source_key == "semantic_scholar":
            source = SemanticScholarSource()
            for i, row in enumerate(rows or [query]):
                if i:
                    time.sleep(1.2)
                config = SemanticScholarConfig(
                    query=row,
                    year_min=search.year_min,
                    year_max=search.year_max,
                    keywords=(row,),
                    venues=tuple(venues),
                    author_names=tuple(author_names),
                    max_pages=1,
                )
                batch, _total = source.fetch(config)
                results.extend(batch)
        elif source_key == "crossref":
            source = CrossrefSource()
            config = CrossrefConfig(
                query=query,
                year_min=search.year_min,
                year_max=search.year_max,
                keywords=tuple(keywords),
                venues=tuple(venues),
                author_names=tuple(author_names),
                max_pages=max_pages,
            )
            results, _total = source.fetch(config)
        else:
            source = ArxivSource()
            config = ArxivConfig(
                query=query,
                year_min=search.year_min,
                year_max=search.year_max,
                keywords=tuple(keywords),
                venues=tuple(venues),
                author_names=tuple(author_names),
                max_pages=min(max_pages, 2),
            )
            results, _total = source.fetch(config)
    except Exception as exc:  # narrow boundary: provider failure -> audit row
        run.status = SearchRun.Status.FAILED
        run.error_message = str(exc)[:2000]
        run.save(update_fields=["status", "error_message"])
        return run, []

    new_count, dup_count, no_abs_count, touched = _store_batch(
        search=search,
        source_key=source_key,
        run=run,
        items=results,
        title_stage=title_stage,
        query_for_provenance=run.query_used,
        create_decisions=False,
    )
    run.results_count = len(results)
    run.new_papers_count = new_count
    run.duplicates_count = dup_count
    run.no_abstract_count = no_abs_count
    run.status = SearchRun.Status.COMPLETED
    run.save(
        update_fields=[
            "results_count",
            "new_papers_count",
            "duplicates_count",
            "no_abstract_count",
            "status",
        ]
    )
    return run, touched


# --- Run one search across providers ---
def run_search(search_id: int, *, max_pages: int = 4) -> list[SearchRun]:
    """Fetch from ticked providers, complete the database, gate screening.

    Pipeline per run: store and deduplicate -> enrich missing metadata ->
    admit every titled paper to Title screening. Missing abstracts and
    references are completed in their later review steps, not rejected.
    Returns per-source runs in ticked order.
    Failures are isolated per provider so one outage never blocks others.
    """
    search = Search.objects.select_related("project").get(pk=search_id)
    search.project.ensure_default_stages()

    keywords = list(search.keywords or [])
    venues = list(search.venues or [])
    author_names = list(search.author_names or [])
    derived_query = " ".join(str(k).strip() for k in keywords if str(k).strip())
    if not (derived_query or search.query):
        raise ValueError("Search has no keywords to query.")
    title_stage = search.project.stages.order_by("order").first()

    allowed = ("openalex", "semantic_scholar", "crossref", "arxiv")
    selected = [s for s in (search.sources_selected or list(allowed)) if s in allowed]
    if not selected:
        selected = list(allowed)

    runs: list[SearchRun] = []
    touched_by_run: list[list[int]] = []
    for i, source_key in enumerate(selected):
        if i:
            time.sleep(1.0)
        run, touched = _run_provider(
            search=search,
            source_key=source_key,
            derived_query=derived_query or search.query,
            keywords=keywords,
            venues=venues,
            author_names=author_names,
            max_pages=max_pages,
            title_stage=title_stage,
        )
        runs.append(run)
        touched_by_run.append(touched)

    # Enrich incomplete papers from other providers (batched DOI lookup).
    # Enrichment failing must never fail the runs: gate on best effort.
    seen: set[int] = set()
    ordered_pks: list[int] = []
    for touched in touched_by_run:
        for pk in touched:
            if pk not in seen:
                seen.add(pk)
                ordered_pks.append(pk)
    if ordered_pks:
        papers = list(Paper.objects.filter(pk__in=ordered_pks))
        try:
            enrich_papers(papers)
        except Exception:
            pass
        fresh = {p.pk: p for p in Paper.objects.filter(pk__in=ordered_pks)}
        gated: set[int] = set()
        for run, touched in zip(runs, touched_by_run):
            if run.status != SearchRun.Status.COMPLETED:
                continue
            incomplete = 0
            for pk in touched:
                if pk in gated:
                    continue
                gated.add(pk)
                paper = fresh.get(pk)
                if paper is None:
                    continue
                if title_ready(paper):
                    if title_stage is not None:
                        ScreeningDecision.objects.get_or_create(
                            paper=paper,
                            stage=title_stage,
                            defaults={"decision": ScreeningDecision.Decision.PENDING},
                        )
                else:
                    incomplete += 1
            run.incomplete_count = incomplete
            run.save(update_fields=["incomplete_count"])

    completed = [r for r in runs if r.status == SearchRun.Status.COMPLETED]
    search.last_run_at = timezone.now()
    search.results_count = sum(r.results_count for r in completed)
    search.new_papers_count = sum(r.new_papers_count for r in completed)
    search.save(update_fields=["last_run_at", "results_count", "new_papers_count"])
    return runs
