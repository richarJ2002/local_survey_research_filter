"""!
@File: paper_lookup_service.py
@Brief: Read-only paper previews and atomic keyword/status saves for DOI lookup.
@Date: 30/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

Django application service, invoked through views rather than a standalone CLI.
"""

from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import quote

from django.db import transaction

from literature.models import Discovery, Paper, PaperKeywordAssignment, Project, Search
from literature.services.manual_paper_service import (
    finish_manual_addition,
    selected_keyword,
    selected_origin,
)
from literature.services.reference_service import normalize_doi
from literature.services.screening_service import abstract_available, correct_decision, title_ready


# --- Preview data ---
def build_metadata(data: dict, doi: str) -> dict:
    """!
    @brief Keep only typed, displayable provider metadata and supported citation identifiers.
    @param data Provider lookup result or stored paper values.
    @param doi Validated identity of the paper being previewed.
    @return JSON-serializable metadata, with no credentials or raw API response.
    """
    metadata = {"doi": doi}
    for field in ("title", "abstract", "venue"):
        value = data.get(field)
        metadata[field] = value.strip() if isinstance(value, str) else ""
    reference_text = data.get("reference_text")
    metadata["reference_text"] = reference_text.strip() if isinstance(reference_text, str) else ""
    year = data.get("publication_year")
    metadata["publication_year"] = year if type(year) is int and 1500 <= year <= 9999 else None
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
    references = data.get("references") if isinstance(data.get("references"), list) else []
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
    """!
    @brief Resolve a supported citation identifier to an HTTPS provider link.
    @param reference DOI, OpenAlex URL or S2 identifier.
    @return Safe HTTPS link, or an empty string for an unsupported identifier.
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
    """!
    @brief Prefill an existing assignment and statuses without assuming a new paper's choices.
    @param project Project being updated.
    @param paper Stored canonical paper, or None for a new DOI.
    @return Initial form values.
    """
    if paper is None:
        return {}
    assignment = PaperKeywordAssignment.objects.filter(project=project, paper=paper).first()
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
        paper.decisions.filter(stage__project=project).values_list("stage__name", "decision")
    )
    return {
        "search_keyword": keyword_value,
        "title_decision": decisions.get("Title", "PENDING"),
        "abstract_decision": decisions.get(
            "Abstract", "PENDING" if decisions.get("Title") == "ACCEPT" else "NOT_QUEUED"
        ),
    }


def entry_version(project: Project, paper: Paper) -> str:
    """!
    @brief Fingerprint metadata, assignment and decisions to detect changes in another tab.
    @param project Project whose entry state is being reviewed.
    @param paper Current stored paper.
    @return Stable state fingerprint without exposing raw database rows in the token.
    """
    assignment = list(
        paper.keyword_assignments.filter(project=project).values_list("search_id", "keyword")
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
    """!
    @brief Save keyword assignment, provenance and both statuses as one transaction.
    @param project Owning project for screening and keyword assignment.
    @param doi Validated DOI from the preview.
    @param metadata Authenticated read-only provider preview for new records.
    @param values Validated keyword and status form values.
    @param expected_paper_id Identity previewed by the user, or None for a new DOI.
    @param expected_version State fingerprint for an existing paper preview.
    @return Saved paper and whether it was newly created.
    @throws ValueError If the preview is stale or a requested assignment/status is invalid.
    """
    paper = Paper.objects.select_for_update().filter(doi__iexact=doi).first()
    if (paper.pk if paper else None) != expected_paper_id:
        raise ValueError("The database entry changed since this preview. Look up the DOI again.")
    if paper is not None and entry_version(project, paper) != expected_version:
        raise ValueError(
            "This entry was updated after you opened it. Reload the entry before saving."
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
            raise ValueError("No paper title was found. Retry the DOI lookup or use manual entry.")
        paper = Paper(**candidate)
        if paper.openalex_id and Paper.objects.filter(openalex_id=paper.openalex_id).exists():
            raise ValueError(
                "The provider identifies a paper already stored under another DOI. Review the existing record before adding it."
            )
    if abstract == "ACCEPT" and not abstract_available(paper.abstract):
        raise ValueError(
            "No abstract found. Enter it during Abstract screening before accepting the paper."
        )

    # Resolve the selected existing keyword or create the explicit new choice.
    if values["search_keyword"] == "NEW":
        search = (
            Search.objects.select_for_update()
            .filter(
                project=project,
                pk=values.get("new_keyword_search"),
            )
            .first()
        )
        fresh = (values.get("new_keyword") or "").strip()
        if search is None or not fresh or len(fresh) > 255:
            raise ValueError("Enter a keyword and choose a search in this project.")
        search.keywords = Search.clean_rows([*search.keywords, fresh])
        search.save()
        keyword = next(word for word in search.keywords if word.lower() == fresh.lower())
    else:
        search, keyword = selected_keyword(project, values["search_keyword"])

    if created:
        paper.keywords = [keyword]
        paper.sources = [origin]
        paper.save()
    old_assignment = PaperKeywordAssignment.objects.filter(paper=paper, project=project).first()
    changed_link = old_assignment is None or (old_assignment.search_id, old_assignment.keyword) != (
        search.pk,
        keyword,
    )
    if changed_link or existing_origin is None:
        PaperKeywordAssignment.objects.update_or_create(
            paper=paper,
            project=project,
            defaults={"search": search, "keyword": keyword},
        )
        discovery, _ = Discovery.objects.get_or_create(
            paper=paper,
            search=search,
            source=origin,
            defaults={"query": keyword, "raw_payload": {"entry_method": "user_added"}},
        )
        # Discovery history is retained; each reassignment has a separate audited run.
        project.ensure_default_stages()
        finish_manual_addition(project, paper, discovery, created=created, keyword=keyword)

    # Update only the keyword index; metadata of an existing record is never saved here.
    keywords = set(paper.keywords or [])
    if old_assignment and old_assignment.keyword != keyword:
        previous = old_assignment.keyword
        used_elsewhere = paper.keyword_assignments.filter(keyword=previous).exists()
        provider_attributed = any(
            (discovery.raw_payload or {}).get("entry_method") != "user_added"
            for discovery in paper.discoveries.exclude(source="manual").filter(query=previous)
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
    # Re-query after each correction: Title can create or invalidate an Abstract row.
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
        correct_decision(paper=paper, stage=stage, decision=status, reason=reason)
    return paper, created
