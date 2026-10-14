#!/usr/bin/env python3

'''
@File:   search_service.py

@Brief:  Execute multi-provider search, store papers, log provenance.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import hashlib
import json
import time

# Custom library Imports
from django.core import signing
from django.db import transaction
from django.utils import timezone

from literature.models import (
    Discovery,
    Paper,
    Project,
    ScreeningDecision,
    Search,
    SearchRun,
)
from literature.providers import DEFAULT_SEARCH_PROVIDERS, SEARCH_PROVIDERS
from literature.providers.arxiv import ArxivConfig, ArxivSource
from literature.providers.crossref import CrossrefConfig, CrossrefSource
from literature.providers.openalex import OpenAlexSource, SearchConfig
from literature.providers.scopus import ScopusConfig, ScopusSource
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
# Definitions
# ------------------------------------------------------------------------ #

# --- Explicit current settings; preview never writes or calls providers ---
PROVIDERS = DEFAULT_SEARCH_PROVIDERS
CONFIGURATION_FIELDS = (
    "pk", "name", "query", "keywords", "venues", "author_names",
    "sources_selected", "year_min", "year_max", "is_current",
)
EDITABLE_FIELDS = (
    "name", "keywords", "venues", "author_names", "sources_selected",
    "year_min", "year_max",
)


# --- Functions


def effective_providers(selection: list[str] | None) -> list[str]:
    """
    Resolve stored sources using the executable legacy fallback rule.

    Args:
        selection: Stored choices, including explicit Scopus selection;
            empty or all-invalid means the original four providers.

    Kwargs:
        None

    Returns:
        list[str]: Allowed sources in stored order, or all four defaults.
        Scopus is never implicitly enabled by an empty selection.
    """
    selected = [source for source in (selection or PROVIDERS)
                if source in SEARCH_PROVIDERS]
    return selected or list(PROVIDERS)


def require_current_search(project: Project, search_id: int) -> Search:
    """
    Resolve this project's sole writable configuration from fresh rows.

    Args:
        project: Owning project in the fixed database.
        search_id: Posted or service-supplied search identity.

    Kwargs:
        None

    Returns:
        Search: Fresh current configuration.

    Raises:
        ValueError: If the identity is historical, stale or foreign.
    """
    search = Search.objects.filter(
        project=project, pk=search_id, is_current=True).first()
    if search is None:
        raise ValueError(
            "Choose this project's current search settings; "
            "historical settings are read-only.")
    return search


def _configuration_sources(project: Project) -> list[dict]:
    """
    Capture project-scoped business settings in stable identity order.

    Args:
        project: Owning project whose settings will be confirmed.

    Kwargs:
        None

    Returns:
        list[dict]: Settings snapshots, without mutable run counters.
    """
    return list(project.searches.order_by("pk").values(*CONFIGURATION_FIELDS))


def _configuration_fingerprint(sources: list[dict]) -> str:
    """
    Fingerprint constituent settings, including executable legacy queries.

    Args:
        sources: Stable configuration snapshots captured from one project.

    Kwargs:
        None

    Returns:
        str: SHA256 digest for detecting stale confirmation.
    """
    return hashlib.sha256(
        json.dumps(sources, sort_keys=True).encode("utf-8")).hexdigest()


def configuration_preview(project: Project, binding: list[str]) -> dict:
    """
    Propose editable future settings without changing research or history.

    Args:
        project: Owning project.
        binding: Worker UUID and epoch; two empty strings in developer mode.

    Kwargs:
        None

    Returns:
        dict: Proposal, source snapshots, query-only warnings and signed token.
    """
    sources = _configuration_sources(project)
    effective = [set(effective_providers(source["sources_selected"]))
                 for source in sources]
    proposal = {
        "name": sources[0]["name"] if len(sources) == 1 else "Project search",
        "sources_selected": [
            provider for provider in SEARCH_PROVIDERS
            if (not sources and provider in PROVIDERS)
            or any(provider in choices for choices in effective)
        ],
    }
    for field in ("keywords", "venues", "author_names"):
        rows = Search.clean_rows([
            word for source in sources for word in source[field] or []])
        restricted = field == "keywords" or all(
            source[field] for source in sources)
        proposal[field + "_json"] = json.dumps(rows if restricted else [])
    for field, widest in (("year_min", min), ("year_max", max)):
        bounds = [source[field] for source in sources]
        proposal[field] = (
            widest(bounds) if bounds and None not in bounds else None)
    token = signing.dumps({
        "project": project.pk, "binding": binding,
        "fingerprint": _configuration_fingerprint(sources),
    }, salt="current-search", compress=True)
    return {
        "proposal": proposal, "source_configurations": sources,
        "query_only": [source for source in sources
                       if source["query"].strip() and not source["keywords"]],
        "configuration_token": token,
    }


def _verify_configuration_preview(
    project: Project, sources: list[dict], token: str, binding: list[str],
) -> None:
    """
    Reject stale, forged or expired confirmation before any mutation.

    Args:
        project: Owning project.
        sources: Fresh transaction-local snapshots of all constituent fields.
        token: Server-signed preview submitted by the form.
        binding: Worker UUID and epoch that displayed the preview.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ValueError: If identity, settings or the 30-minute token is invalid.
    """
    try:
        preview = signing.loads(token, salt="current-search", max_age=1800)
    except signing.BadSignature as exc:
        raise ValueError(
            "This search preview expired or is invalid. "
            "Reload settings before saving.") from exc
    expected = {
        "project": project.pk, "binding": binding,
        "fingerprint": _configuration_fingerprint(sources),
    }
    if preview != expected:
        raise ValueError(
            "Search settings changed after this preview. "
            "Reload before confirming.")


@transaction.atomic
def update_current_configuration(
    project: Project, search_id: int, values: dict, token: str,
    binding: list[str],
) -> Search:
    """
    Apply confirmed business settings without overwriting run counters.

    Args:
        project: Owning project.
        search_id: Sole writable configuration identity.
        values: Validated SearchForm future settings.
        token: Signed preview; keyword creation invalidates stale editors.
        binding: Worker UUID and epoch that displayed the preview.

    Kwargs:
        None

    Returns:
        Search: Updated current configuration.

    Raises:
        ValueError: If confirmation or the configuration is stale or foreign.
        OperationalError: If contention requires an explicit user retry.
    """
    _verify_configuration_preview(
        project, _configuration_sources(project), token, binding)
    search = require_current_search(project, search_id)
    for field in EDITABLE_FIELDS:
        setattr(search, field, values.get(field))
    search.save(update_fields=[*EDITABLE_FIELDS, "query"])
    return search


@transaction.atomic
def save_current_configuration(
    project: Project, values: dict, token: str, binding: list[str], *,
    adopt: bool = False, translated: bool = False,
) -> Search:
    """
    Adopt an unchanged legacy row or create a new current snapshot.

    Args:
        project: Owning project.
        values: Validated future settings; ignored for explicit adoption.
        token: Signed preview of all constituent settings.
        binding: Worker UUID and epoch that displayed the preview.

    Kwargs:
        adopt: Promote only the single legacy flag. Defaults to False,
            creating a new row rather than altering historical settings.
        translated: Acknowledge translation or replacement of legacy
            query-only syntax. Defaults to False, refusing silent loss.

    Returns:
        Search: Sole current row; all historical fields and FKs are retained.

    Raises:
        ValueError: If confirmation is stale, ambiguous or already configured.
        IntegrityError: If a competitor wins the unique-current constraint.
        OperationalError: If SQLite contention requires a user retry.
    """
    # ponytail: uniqueness arbitrates races; SQLite has no row-lock guarantee.
    sources = _configuration_sources(project)
    _verify_configuration_preview(project, sources, token, binding)
    if any(source["is_current"] for source in sources):
        raise ValueError(
            "Another tab already configured this project. "
            "Edit its current settings instead.")
    if adopt:
        if len(sources) != 1:
            raise ValueError(
                "Only one unchanged configuration can be adopted directly.")
        # No Search.save normalization: preserve executable query-only syntax.
        Search.objects.filter(project=project, pk=sources[0]["pk"]).update(
            is_current=True)
        return Search.objects.get(pk=sources[0]["pk"], project=project)
    if any(source["query"].strip() and not source["keywords"]
           for source in sources) and not translated:
        raise ValueError(
            "Translate or deliberately replace the displayed legacy queries, "
            "then confirm that choice.")
    return Search.objects.create(
        project=project, is_current=True,
        **{field: values.get(field) for field in EDITABLE_FIELDS})


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
    """
    Deduplicate and persist one provider batch with discovery provenance.

    Every returned paper is stored with provenance (complete database).
    When create_decisions is False, queue rows are left to gate_papers
    so enrichment can complete papers first.

    Args:
        None

    Kwargs:
        search: Required configuration owning this provider discovery.
        source_key: Required provider identifier recorded in provenance.
        run: Required SearchRun audit receiving the batch counters.
        items: Required mapped results with paper_data and raw_response.
        title_stage: Required Title stage, or None to omit queue creation.
        query_for_provenance: Required executed query stored on discoveries.
        create_decisions: Create pending Title rows when True; defaults True.

    Returns:
        tuple: New-paper count, duplicate-hit count, missing-abstract count
        and distinct touched paper IDs in encounter order.
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
    """
    Check full metadata completeness, not eligibility for Title admission.

    Args:
        paper: Canonical paper whose title, abstract and references are checked.

    Kwargs:
        None

    Returns:
        bool: True for a nonblank title, real abstract and available references.
    """
    return bool(
        (paper.title or "").strip()
        and abstract_available(paper.abstract)
        and has_reference_list(paper)
    )


# --- Gate touched papers into the screening queue ---
def gate_papers(papers: list[Paper], title_stage) -> int:
    """
    Admit titled papers without replacing existing screening decisions.

    Args:
        papers: Touched canonical papers to consider for Title admission.
        title_stage: Owning Title stage, or None to omit queue creation.

    Kwargs:
        None

    Returns:
        int: Number lacking titles; missing abstracts/references do not count.
    """
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
    """
    Execute one provider and retain a distinct audit of success or failure.

    Decisions are deferred: run_search enriches incomplete papers first,
    then gates them into the queue. OpenAlex/Crossref/arXiv get one
    OR-joined fetch (1 request) to stay inside rate limits. Semantic
    Scholar gets one plain fetch per row because its query syntax
    ignores OR (OR-joined returns 0 hits).
    Scopus uses literal fielded OR groups and retains its exact translated
    query in the audit. Scopus credentials never enter queries or errors.

    Args:
        None

    Kwargs:
        search: Required configuration owning the new audit and discoveries.
        source_key: Required supported provider identifier.
        derived_query: Required fallback query when keyword rows are absent.
        keywords: Required keyword rows used to build provider-specific syntax.
        venues: Required restrictions; empty means unrestricted.
        author_names: Required author restrictions; empty means unrestricted.
        max_pages: Required fetch-page ceiling; S2 uses one page per row.
        title_stage: Required Title stage passed to the deferred batch gate.

    Returns:
        tuple[SearchRun, list[int]]: Provider audit and touched paper IDs.
        Provider failures are retained as failed audits with no fetched batch.
    """
    rows = [str(k).strip() for k in keywords if str(k).strip()]

    # --- Escape one row for OR embedding ---
    def _escape_row(term: str) -> str:
        """
        Escape a keyword row before placing it in quoted provider syntax.

        Args:
            term: Keyword text to escape.

        Kwargs:
            None

        Returns:
            str: Text with backslashes and double quotes escaped.
        """
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
        elif source_key == "scopus":
            source = ScopusSource()
            config = ScopusConfig(
                query=query,
                year_min=search.year_min,
                year_max=search.year_max,
                keywords=tuple(keywords),
                venues=tuple(venues),
                author_names=tuple(author_names),
                max_pages=max_pages,
            )
            run.query_used = source.build_request_params(config, 0)["query"]
            run.save(update_fields=["query_used"])
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
    """
    Fetch from ticked providers, complete the database, gate screening.

    Pipeline per run: store and deduplicate -> enrich missing metadata ->
    admit every titled paper to Title screening. Missing abstracts and
    references are completed in their later review steps, not rejected.
    Returns per-source runs in ticked order.
    Failures are isolated per provider so one outage never blocks others.

    Args:
        search_id: Current configuration identity; historical IDs are refused
            before stage creation, run audits or provider construction.

    Kwargs:
        max_pages: Provider page ceiling. Defaults to 4; existing per-provider
            lower ceilings and pacing remain in effect.

    Returns:
        list[SearchRun]: New per-provider audits, including failed sources.

    Raises:
        ValueError: If settings are historical or contain no executable query.
        Search.DoesNotExist: If the configuration identity does not exist.
    """
    search = Search.objects.select_related("project").get(pk=search_id)
    search = require_current_search(search.project, search.pk)
    search.project.ensure_default_stages()

    keywords = list(search.keywords or [])
    venues = list(search.venues or [])
    author_names = list(search.author_names or [])
    derived_query = " ".join(str(k).strip()
                             for k in keywords if str(k).strip())
    if not (derived_query or search.query):
        raise ValueError("Search has no keywords to query.")
    title_stage = search.project.stages.order_by("order").first()

    selected = effective_providers(search.sources_selected)

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
                            defaults={
                                "decision": (
                                    ScreeningDecision.Decision.PENDING),
                            },
                        )
                else:
                    incomplete += 1
            run.incomplete_count = incomplete
            run.save(update_fields=["incomplete_count"])

    completed = [r for r in runs if r.status == SearchRun.Status.COMPLETED]
    search.last_run_at = timezone.now()
    search.results_count = sum(r.results_count for r in completed)
    search.new_papers_count = sum(r.new_papers_count for r in completed)
    search.save(update_fields=["last_run_at",
                "results_count", "new_papers_count"])
    return runs
