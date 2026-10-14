#!/usr/bin/env python3

'''
@File:   manual_paper_service.py

@Brief:  Curate missed project papers without rerunning provider searches.

@Date:   30/09/2026

@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.db import transaction

from literature.models import Discovery, Paper, Project, ScreeningDecision, Search, SearchRun
from literature.services.dedup_service import find_duplicate
from literature.services.screening_service import (
    abstract_available,
    has_reference_list,
    title_ready,
)
from literature.services.search_service import require_current_search

# ------------------------------------------------------------------------ #
# Module setup
# ------------------------------------------------------------------------ #

ORIGIN_SOURCES = (
    ("openalex", "OpenAlex"), ("semantic_scholar", "Semantic Scholar"),
    ("crossref", "Crossref"), ("arxiv", "arXiv"),
    ("scopus", "Scopus"),
    ("google_scholar", "Google Scholar"), ("publisher", "Publisher / journal website"),
    ("reference_list", "Another paper's reference list"), ("other", "Other source"),
)


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Functions
def selected_origin(value: str) -> str:
    """Require a discovery origin; manual entry is a method, not a source.

    Args:
        value: Submitted origin identifier from the supported choices.

    Kwargs:
        None

    Returns:
        str: Unchanged valid discovery-origin identifier.

    Raises:
        ValueError: If the origin was omitted or is unsupported.
    """
    if value not in dict(ORIGIN_SOURCES):
        raise ValueError("Choose where the paper was found.")
    return value


def author_rows(raw: str) -> list[dict]:
    """Parse one locally entered author per nonblank line.

    Args:
        raw: Author text with newline-separated names.

    Kwargs:
        None

    Returns:
        list[dict]: Canonical name rows with blank ORCID and affiliations.
    """
    return [
        {"name": name.strip(), "orcid": "", "affiliations": []}
        for name in raw.splitlines() if name.strip()
    ]


def selected_keyword(project: Project, value: str) -> tuple[Search, str]:
    """Resolve a posted keyword only against this project's current settings.

    Args:
        project: Owning project.
        value: Posted search ID and keyword separated by a vertical bar.

    Kwargs:
        None

    Returns:
        tuple[Search, str]: Fresh current search and its exact keyword.

    Raises:
        ValueError: If the identity is malformed, foreign, historical or stale.
    """
    try:
        search_id, keyword = value.split("|", 1)
        search = require_current_search(project, int(search_id))
    except (ValueError, Search.DoesNotExist) as exc:
        raise ValueError(
            "Choose a keyword from this project's searches.") from exc
    if keyword not in search.keywords:
        raise ValueError("That keyword is no longer in the search.")
    return search, keyword


@transaction.atomic
def add_manual_paper(
    project: Project, search: Search, keyword: str, data: dict,
) -> tuple[Paper, bool, Discovery]:
    """Persist a manual paper under a fresh current keyword and real origin.

    Args:
        project: Owning project.
        search: Submitted search; re-read before any paper mutation.
        keyword: Exact current keyword to attribute.
        data: Validated manual metadata and required origin_source.

    Kwargs:
        None

    Returns:
        tuple[Paper, bool, Discovery]: Paper, creation flag and provenance.

    Raises:
        ValueError: If the search, keyword or discovery origin is invalid.
    """
    search = require_current_search(project, search.pk)
    if search.project_id != project.pk or keyword not in search.keywords:
        raise ValueError("Keyword does not belong to this project search.")
    origin = selected_origin(data.get("origin_source", ""))
    candidate = {"doi": data.get("doi") or None,
                 "title": data.get("title") or ""}
    paper, match = find_duplicate(candidate)
    # A title match alone cannot override two conflicting DOI identities.
    # Keep both records so the existing duplicate-review page can decide.
    if (match == "title" and paper and paper.doi and candidate["doi"]
            and paper.doi != candidate["doi"]):
        paper = None
    created = paper is None
    if paper is None:
        paper = Paper.objects.create(
            title=data.get("title") or "", doi=data.get("doi") or None,
            authors=author_rows(data.get("authors") or ""),
            abstract=data.get("abstract") or "",
            publication_year=data.get("publication_year"),
            venue=data.get("venue") or "",
            references=data.get("references") or [],
            keywords=[keyword], sources=[origin],
        )
    else:
        changed: list[str] = []
        for field in ("title", "doi", "abstract", "publication_year", "venue",
                      "references"):
            incoming = data.get(field)
            if incoming and not getattr(paper, field):
                setattr(paper, field, incoming)
                changed.append(field)
        authors = author_rows(data.get("authors") or "")
        if authors and not paper.authors:
            paper.authors = authors
            changed.append("authors")
        if keyword not in paper.keywords:
            paper.keywords = sorted(
                [*paper.keywords, keyword], key=str.casefold)
            changed.append("keywords")
        if origin not in paper.sources:
            paper.sources = [*paper.sources, origin]
            changed.append("sources")
        if changed:
            paper.save(update_fields=[*changed, "updated_at"])
    discovery, _ = Discovery.objects.get_or_create(
        paper=paper, search=search, source=origin,
        defaults={"query": keyword, "raw_payload": {
            "entry_method": "user_added"}},
    )
    return paper, created, discovery


@transaction.atomic
def finish_manual_addition(
    project: Project, paper: Paper, discovery: Discovery, *, created: bool,
    keyword: str,
) -> None:
    """Audit a current-keyword addition and admit titled papers to Title.

    Args:
        project: Owning project.
        paper: Saved canonical paper being linked.
        discovery: Current search provenance for this paper.

    Kwargs:
        created: Required flag recording new versus deduplicated paper.
        keyword: Required exact current keyword attributed by the addition.

    Returns:
        None

    Raises:
        ValueError: If provenance is foreign, historical or no longer current.
    """
    search = require_current_search(project, discovery.search_id)
    if discovery.paper_id != paper.pk or keyword not in search.keywords:
        raise ValueError("Keyword does not belong to this current discovery.")
    run = SearchRun.objects.create(
        search=discovery.search, source=discovery.source, query_used=keyword,
        status=SearchRun.Status.COMPLETED, results_count=1,
        new_papers_count=int(created), duplicates_count=int(not created),
        incomplete_count=int(not title_ready(paper)),
    )
    if discovery.search_run_id is None:
        discovery.search_run = run
        discovery.save(update_fields=["search_run"])
    if title_ready(paper):
        title_stage = project.stages.get(name="Title")
        ScreeningDecision.objects.get_or_create(
            paper=paper, stage=title_stage,
            defaults={"decision": ScreeningDecision.Decision.PENDING},
        )


@transaction.atomic
def update_paper_details(project: Project, paper: Paper, data: dict) -> None:
    """Save local details atomically without resetting research decisions.

    Args:
        project: Project owning a discovery of this paper.
        paper: Existing canonical record being explicitly edited.
        data: Validated details, including newline-separated author text.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ValueError: For foreign papers, conflicting DOI, or removal of a
            title/abstract/reference list required by existing decisions.

    Side Effects:
        Persist explicit metadata and create a pending Title row if needed;
        leave keyword assignments and all existing decisions unchanged.
    """
    had_references = has_reference_list(paper)
    updated = {
        "title": data.get("title") or "",
        "doi": data.get("doi") or None,
        "authors": author_rows(data.get("authors") or ""),
        "abstract": data.get("abstract") or "",
        "publication_year": data.get("publication_year"),
        "venue": data.get("venue") or "",
        "references": data.get("references") or [],
    }
    for field, value in updated.items():
        setattr(paper, field, value)
    if not paper.discoveries.filter(search__project=project).exists():
        raise ValueError("Paper does not belong to this project.")
    if not title_ready(paper) and paper.decisions.exists():
        raise ValueError("A screened paper cannot lose its title.")
    if not abstract_available(paper.abstract) and paper.decisions.filter(stage__name="Abstract", decision="ACCEPT").exists():
        raise ValueError(
            "An abstract-accepted paper cannot lose its abstract.")
    if had_references and not has_reference_list(paper) and paper.decisions.filter(stage__name="Abstract", decision="ACCEPT").exists():
        raise ValueError(
            "A finally selected paper cannot lose its reference list through a details edit.")
    if paper.doi and Paper.objects.filter(doi__iexact=paper.doi).exclude(pk=paper.pk).exists():
        raise ValueError(
            "That DOI already belongs to another paper. Review duplicates instead.")
    paper.save(update_fields=[*updated, "updated_at"])
    if title_ready(paper):
        project.ensure_default_stages()
        ScreeningDecision.objects.get_or_create(
            paper=paper, stage=project.stages.get(name="Title"),
            defaults={"decision": ScreeningDecision.Decision.PENDING},
        )
