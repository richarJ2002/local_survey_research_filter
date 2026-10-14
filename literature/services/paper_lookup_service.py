"""
!
@File: paper_lookup_service.py
@Brief: Read-only paper previews and atomic keyword/status saves for DOI lookup.
@Date: 30/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

Django application service, invoked through views rather than a standalone CLI.
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import quote

# Custom library Imports
from django.db import transaction

from literature.models import Discovery, Paper, PaperKeywordAssignment, Project, Search
from literature.services.manual_paper_service import (
    finish_manual_addition,
    selected_keyword,
    selected_origin,
)
from literature.services.reference_service import normalize_doi
from literature.services.screening_service import abstract_available, correct_decision, title_ready

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Preview data ---


def build_metadata(data: dict, doi: str) -> dict:
    """
    Project typed preview fields without retaining raw provider responses.

    Args:
        data: Provider result or stored paper values to validate.
        doi: Already validated identity of the previewed paper.

    Kwargs:
        None

    Returns:
        dict: JSON-compatible typed bibliographic fields and supported
        identifiers, excluding credentials and raw API payloads.
    """
    metadata = {"doi": doi}
    for field in ("title", "abstract", "venue"):
        value = data.get(field)
        metadata[field] = value.strip() if isinstance(value, str) else ""
    reference_text = data.get("reference_text")
    metadata["reference_text"] = reference_text.strip(
    ) if isinstance(reference_text, str) else ""
    year = data.get("publication_year")
    metadata["publication_year"] = year if type(
        year) is int and 1500 <= year <= 9999 else None
    metadata["authors"] = (
        [
            {
                "name": author["name"],
                "orcid": author.get("orcid") if isinstance(author.get("orcid"), str) else "",
                "affiliations": [
                    affiliation
                    for affiliation in author.get("affiliations", [])
                    if isinstance(affiliation, str)
                ]
                if isinstance(author.get("affiliations"), list)
                else [],
            }
            for author in (data.get("authors") or [])
            if isinstance(author, dict)
            and isinstance(author.get("name"), str)
            and author["name"].strip()
        ]
        if isinstance(data.get("authors"), list)
        else []
    )
    # Never make clickable links from arbitrary provider URLs.
    references = data.get("references") if isinstance(
        data.get("references"), list) else []
    metadata["references"] = list(
        dict.fromkeys(
            reference
            for reference in references
            if isinstance(reference, str) and reference_url(reference)
        )
    )
    openalex_id = data.get("openalex_id")
    metadata["openalex_id"] = (
        openalex_id
        if isinstance(openalex_id, str) and re.fullmatch(r"W[0-9A-Za-z]+", openalex_id)
        else None
    )
    ids = data.get("source_ids")
    metadata["source_ids"] = (
        {
            key: value
            for key, value in ids.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if isinstance(ids, dict)
        else {}
    )
    return metadata


def reference_url(reference: str) -> str:
    """
    Resolve a supported identifier without accepting arbitrary URLs.

    Args:
        reference: DOI, canonical OpenAlex URL or S2 identifier.

    Kwargs:
        None

    Returns:
        str: Safe HTTPS provider link, or blank for an unsupported identifier.
    """
    if re.fullmatch(r"https://openalex\.org/W[0-9A-Za-z]+", reference):
        return reference
    if re.fullmatch(r"S2:[0-9A-Za-z-]+", reference):
        return "https://www.semanticscholar.org/paper/" + reference[3:]
    doi = normalize_doi(reference)
    if doi and re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        return "https://doi.org/" + quote(doi, safe="/")
    return ""


def entry_initial(project: Project, paper: Paper | None) -> dict:
    """
    Prefill saved linkage/status without guessing new-entry decisions.

    Args:
        project: Project whose assignment and decisions are displayed.
        paper: Stored canonical paper, or None for a new DOI.

    Kwargs:
        None

    Returns:
        dict: Saved keyword/Title/Abstract form values; empty for a new paper.
    """
    if paper is None:
        return {}
    assignment = PaperKeywordAssignment.objects.filter(
        project=project, paper=paper).first()
    keyword_value = ""
    if assignment is not None:
        keyword_value = f"{assignment.search_id}|{assignment.keyword}"
    else:
        # Older entries have no assignment yet; only infer an unambiguous valid linkage.
        choices = []
        for discovery in paper.discoveries.filter(search__project=project).select_related("search"):
            if discovery.query in discovery.search.keywords:
                choices.append(f"{discovery.search_id}|{discovery.query}")
        if len(set(choices)) == 1:
            keyword_value = choices[0]
    decisions = dict(
        paper.decisions.filter(stage__project=project).values_list(
            "stage__name", "decision")
    )
    return {
        "search_keyword": keyword_value,
        "title_decision": decisions.get("Title", "PENDING"),
        "abstract_decision": decisions.get(
            "Abstract", "PENDING" if decisions.get(
                "Title") == "ACCEPT" else "NOT_QUEUED"
        ),
    }


def entry_version(project: Project, paper: Paper) -> str:
    """
    Fingerprint the stored entry to reject stale confirmation from a tab.

    Args:
        project: Project owning the relevant assignment and decisions.
        paper: Current stored paper, including its last metadata timestamp.

    Kwargs:
        None

    Returns:
        str: SHA256 of metadata timestamp, assignment and ordered decisions;
        the signed preview need not expose those raw rows.
    """
    assignment = list(
        paper.keyword_assignments.filter(
            project=project).values_list("search_id", "keyword")
    )
    decisions = list(
        paper.decisions.filter(stage__project=project)
        .order_by("stage_id")
        .values_list("stage_id", "decision", "updated_at")
    )
    state = (paper.updated_at.isoformat(), assignment, decisions)
    return sha256(repr(state).encode("utf-8")).hexdigest()


# --- Atomic save ---
@transaction.atomic
def save_entry(
    project: Project,
    doi: str,
    metadata: dict,
    values: dict,
    expected_paper_id: int | None,
    expected_version: str | None = None,
) -> tuple[Paper, bool]:
    """
    Save explicit assignment and statuses atomically, retaining old links.

    Args:
        project: Owning project for screening and keyword assignment.
        doi: Validated DOI from the signed preview.
        metadata: Authenticated provider preview for new records only.
        values: Validated keyword, origin and screening form values.
        expected_paper_id: Previewed identity, or None for a new DOI.

    Kwargs:
        expected_version: Existing entry fingerprint. Defaults to None for
            new entries; existing entries require their matching version.

    Returns:
        tuple[Paper, bool]: Saved paper and whether it was newly created.

    Raises:
        ValueError: If preview, keyword target or screening choice is invalid.
        OperationalError: If SQLite contention requires an explicit retry.
    """
    paper = Paper.objects.select_for_update().filter(doi__iexact=doi).first()
    if (paper.pk if paper else None) != expected_paper_id:
        raise ValueError(
            "The database entry changed since this preview. "
            "Look up the DOI again.")
    if paper is not None and entry_version(project, paper) != expected_version:
        raise ValueError(
            "This entry was updated after you opened it. "
            "Reload the entry before saving."
        )
    existing_origin = (
        paper.discoveries.filter(search__project=project)
        .exclude(source="manual")
        .order_by("pk")
        .first()
        if paper
        else None
    )
    origin = (
        existing_origin.source
        if existing_origin
        else selected_origin(values.get("origin_source", ""))
    )
    title = values["title_decision"]
    abstract = values["abstract_decision"]
    if title not in {"PENDING", "ACCEPT", "REJECT"} or abstract not in {
        "NOT_QUEUED",
        "PENDING",
        "ACCEPT",
        "REJECT",
    }:
        raise ValueError("Choose valid Title and Abstract statuses.")
    if (title == "ACCEPT") == (abstract == "NOT_QUEUED"):
        raise ValueError("Abstract screening requires Title to pass first.")

    # Validate metadata before any keyword creation or database writes.
    created = paper is None
    if created:
        candidate = build_metadata(metadata, doi)
        if not candidate["title"]:
            raise ValueError(
                "No paper title was found. "
                "Retry the DOI lookup or use manual entry.")
        paper = Paper(**candidate)
        if paper.openalex_id and Paper.objects.filter(
                openalex_id=paper.openalex_id).exists():
            raise ValueError(
                "The provider identifies a paper already stored under another "
                "DOI. Review the existing record before adding it."
            )
    if abstract == "ACCEPT" and not abstract_available(paper.abstract):
        raise ValueError(
            "No abstract found. Enter it during Abstract screening "
            "before accepting the paper."
        )

    historical_value = (entry_initial(project, paper).get("search_keyword", "")
                        if not created else "")
    linked_search = (Search.objects.filter(
        project=project, pk=historical_value.split("|", 1)[0]).first()
        if historical_value else None)
    selectable = bool(
        linked_search and linked_search.is_current
        and historical_value.split("|", 1)[1] in linked_search.keywords)
    preserve_historical = bool(
        historical_value and values["search_keyword"] == historical_value
        and not selectable)
    if preserve_historical and existing_origin is None:
        raise ValueError(
            "Choose a current keyword before recording "
            "a new discovery origin.")
    # Only server-derived saved links bypass the current reassignment rule.
    if values["search_keyword"] == "NEW":
        search = (
            Search.objects.select_for_update()
            .filter(
                project=project,
                pk=values.get("new_keyword_search"),
                is_current=True,
            )
            .first()
        )
        fresh = (values.get("new_keyword") or "").strip()
        if search is None or not fresh or len(fresh) > 255:
            raise ValueError(
                "Enter a keyword and choose a search in this project.")
        if search.query.strip() and not search.keywords:
            raise ValueError(
                "These settings use a historical query without keyword rows. "
                "Translate or replace it explicitly in Edit search settings "
                "before adding a keyword; nothing was saved."
            )
        search.keywords = Search.clean_rows([*search.keywords, fresh])
        search.save(update_fields=["keywords", "query"])
        keyword = next(
            word for word in search.keywords if word.lower() == fresh.lower())
    elif preserve_historical:
        search = linked_search
        keyword = historical_value.split("|", 1)[1]
    else:
        search, keyword = selected_keyword(project, values["search_keyword"])

    if created:
        paper.keywords = [keyword]
        paper.sources = [origin]
        paper.save()
    old_assignment = PaperKeywordAssignment.objects.filter(
        paper=paper, project=project).first()
    changed_link = (old_assignment is None
                    or (old_assignment.search_id, old_assignment.keyword)
                    != (search.pk, keyword))
    if not preserve_historical and (changed_link or existing_origin is None):
        PaperKeywordAssignment.objects.update_or_create(
            paper=paper,
            project=project,
            defaults={"search": search, "keyword": keyword},
        )
        discovery, _ = Discovery.objects.get_or_create(
            paper=paper,
            search=search,
            source=origin,
            defaults={"query": keyword, "raw_payload": {
                "entry_method": "user_added"}},
        )
        # Retain discoveries; audit each explicit reassignment separately.
        project.ensure_default_stages()
        finish_manual_addition(project, paper, discovery,
                               created=created, keyword=keyword)

    if not preserve_historical:
        # Existing metadata and historical links are not normalization targets.
        keywords = set(paper.keywords or [])
        if old_assignment and old_assignment.keyword != keyword:
            previous = old_assignment.keyword
            used_elsewhere = paper.keyword_assignments.filter(
                keyword=previous).exists()
            provider_attributed = any(
                (discovery.raw_payload or {}).get("entry_method")
                != "user_added"
                for discovery in paper.discoveries.exclude(source="manual")
                .filter(query=previous)
            )
            if not used_elsewhere and not provider_attributed:
                keywords.discard(previous)
        keywords.add(keyword)
        paper.keywords = Search.clean_rows(list(keywords))
        if origin not in paper.sources:
            paper.sources = [*paper.sources, origin]
        paper.save(update_fields=["keywords", "sources", "updated_at"])

    project.ensure_default_stages()
    title_stage = project.stages.get(name="Title")
    abstract_stage = project.stages.get(name="Abstract")
    reason = "Updated through the DOI paper entry panel."
    # Re-query: Title correction can create or invalidate an Abstract row.
    for stage, status in ((title_stage, title), (abstract_stage, abstract)):
        current = paper.decisions.filter(stage=stage).first()
        if status == "NOT_QUEUED":
            continue
        if current is not None and current.decision == status:
            continue
        if status == "PENDING" and current is None:
            if title_ready(paper):
                paper.decisions.create(stage=stage, decision=status)
            continue
        correct_decision(paper=paper, stage=stage,
                         decision=status, reason=reason)
    return paper, created
