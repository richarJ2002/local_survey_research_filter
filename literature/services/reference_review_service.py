"""Complete missing screening metadata and retain bibliography text/provenance.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   reference_review_service.py

@Brief:  Complete missing screening metadata and retain bibliography text/provenance.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import re

# Custom library Imports
from django.db import transaction
from django.utils import timezone

from literature.models import Paper, Project, Search
from literature.services.manual_paper_service import author_rows
from literature.services.reference_service import normalize_doi
from literature.services.screening_service import (
    InvalidTransition,
    abstract_available,
    correct_decision,
    has_reference_list,
    title_ready,
)

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Functions


def _record_manual_source(paper: Paper, field: str) -> None:
    """Record manual-field provenance without duplicating the pasted content.

    Args:
        paper (Paper): Instance whose raw metadata is updated in memory.
        field (str): Field name to mark as manually entered.

    Kwargs:
        None

    Returns:
        None

    Side Effects:
        Replace this field's manual_metadata provenance with source="manual"
        and the current ISO timestamp, preserving other raw keys. Do not save.
    """
    raw = dict(paper.raw or {})
    existing = raw.get("manual_metadata")
    manual = dict(existing) if isinstance(existing, dict) else {}
    manual[field] = {"source": "manual",
                     "entered_at": timezone.now().isoformat()}
    raw["manual_metadata"] = manual
    paper.raw = raw


def missing_metadata_fields(paper: Paper) -> set[str]:
    """Identify unknown metadata without treating a reported zero as missing.

    Args:
        paper (Paper): Paper whose values and raw provenance are inspected.

    Kwargs:
        None

    Returns:
        set[str]: Missing field names among title, authors, publication_year,
            publication_date, venue, publisher, doi, keywords, abstract,
            reference_text and citation_count. reference_text is missing only
            when neither reference IDs nor bibliography text exists; the exact
            audience-only tag is not an abstract. Zero citations are missing
            only when neither a provider integer count nor manual count
            provenance confirms them. No database changes are made.
    """
    raw = paper.raw if isinstance(paper.raw, dict) else {}
    manual = raw.get("manual_metadata") if isinstance(
        raw.get("manual_metadata"), dict) else {}
    reported_count = any(
        isinstance(raw.get(key), int)
        for key in ("cited_by_count", "citationCount", "is-referenced-by-count")
    )
    missing = {
        "title": not title_ready(paper),
        "authors": not any(
            (
                str(author.get("name") or "") if isinstance(
                    author, dict) else str(author or "")
            ).strip()
            for author in (paper.authors or [])
        ),
        "publication_year": paper.publication_year is None,
        "publication_date": paper.publication_date is None,
        "venue": not (paper.venue or "").strip(),
        "publisher": not (paper.publisher or "").strip(),
        "doi": not (paper.doi or "").strip(),
        "keywords": not paper.keywords,
        "abstract": not abstract_available(paper.abstract),
        "reference_text": not has_reference_list(paper),
        # ponytail: use existing provenance to distinguish an unreported zero; no new state table.
        "citation_count": paper.citation_count == 0
        and not reported_count
        and "citation_count" not in manual,
    }
    return {field for field, unknown in missing.items() if unknown}


@transaction.atomic
def save_missing_metadata(project: Project, paper: Paper, values: dict) -> None:
    """Atomically fill only unknown fields on a pending Abstract record.

    Args:
        project (Project): Project requiring discovery membership and Title
            acceptance before updating its pending Abstract paper.
        paper (Paper): Stored paper identity; reload the row before validating.
        values (dict): Cleaned field values from the completion form. Ignore
            paper_id, None, empty strings and empty lists. Authors and keywords
            are newline-separated text; dates/years/counts are parsed values.
            Every supplied field must still be unknown when the row is read.

    Kwargs:
        None

    Returns:
        None

    Raises:
        InvalidTransition: If project/stage prerequisites fail, no usable
            update is supplied, a field is already known, abstract text is the
            audience placeholder, year/date disagree, or DOI conflicts.
        Paper.DoesNotExist: If paper has been deleted.
        IntegrityError: If a concurrent write claims a unique DOI identifier.

    Side Effects:
        Save a reloaded instance, not the caller's instance, with manual
        provenance. Derive an unknown year from a date, retain replaced
        abstract placeholders in provenance, and extract IDs from bibliography
        text without dropping the full text. All writes share one transaction.
    """
    locked = Paper.objects.select_for_update().get(pk=paper.pk)
    if (
        not locked.discoveries.filter(search__project=project).exists()
        or not locked.decisions.filter(
            stage__project=project, stage__name="Abstract", decision="PENDING"
        ).exists()
    ):
        raise InvalidTransition(
            "This paper is not pending Abstract screening in this project.")
    if not locked.decisions.filter(
        stage__project=project, stage__name="Title", decision="ACCEPT"
    ).exists():
        raise InvalidTransition("Accept the title first.")
    unknown = missing_metadata_fields(locked)
    updates = {
        field: value
        for field, value in values.items()
        if field != "paper_id" and value is not None and value != "" and value != []
    }
    if not updates:
        raise InvalidTransition(
            "Enter at least one missing detail before saving.")
    if set(updates) - unknown:
        raise InvalidTransition(
            "Some fields were filled in another request. Reload the paper; existing values were not overwritten."
        )
    if "abstract" in updates and not abstract_available(updates["abstract"]):
        raise InvalidTransition(
            "Paste the actual abstract, not a provider metadata tag.")
    date = updates.get("publication_date") or locked.publication_date
    year = updates.get("publication_year") or locked.publication_year
    if date and year and date.year != year:
        raise InvalidTransition(
            "The publication date must match the publication year.")
    if date and locked.publication_year is None and "publication_year" not in updates:
        updates["publication_year"] = date.year
    doi = updates.get("doi")
    if doi and Paper.objects.filter(doi__iexact=doi).exclude(pk=locked.pk).exists():
        raise InvalidTransition(
            "This DOI belongs to another stored paper. Review duplicates instead."
        )
    previous_abstract = locked.abstract
    for field, value in updates.items():
        if field == "authors":
            value = author_rows(value)
        elif field == "keywords":
            value = Search.clean_rows(value.splitlines())
        setattr(locked, field, value)
        _record_manual_source(locked, field)
    changed = list(updates)
    if "abstract" in updates and (previous_abstract or "").strip():
        locked.raw["manual_metadata"]["abstract"]["replaced_placeholder"] = previous_abstract
    if "reference_text" in updates:
        locked.references = extract_reference_ids(updates["reference_text"])
        _record_manual_source(locked, "references")
        changed.append("references")
    locked.save(update_fields=[*changed, "raw", "updated_at"])


@transaction.atomic
def save_missing_abstract(project: Project, paper: Paper, abstract: str) -> None:
    """Atomically replace a missing abstract on a pending Abstract paper.

    Args:
        project (Project): Project requiring discovery membership and Title
            acceptance for the pending Abstract record.
        paper (Paper): Stored identity to reload; the caller's instance is not
            refreshed by this function.
        abstract (str): Actual abstract to strip/store, not an audience tag.

    Kwargs:
        None

    Returns:
        None

    Raises:
        InvalidTransition: If text is blank/placeholder, the paper is not
            pending Abstract in the project, Title is not accepted, or a usable
            abstract was already stored.
        Paper.DoesNotExist: If paper has been deleted.

    Side Effects:
        Save abstract, manual provenance and updated_at in one transaction;
        retain a replaced nonblank placeholder in provenance.
    """
    locked = Paper.objects.select_for_update().get(pk=paper.pk)
    value = abstract.strip()
    if not abstract_available(value):
        raise InvalidTransition("Paste the abstract before saving.")
    if (
        not locked.discoveries.filter(search__project=project).exists()
        or not locked.decisions.filter(
            stage__project=project, stage__name="Abstract", decision="PENDING"
        ).exists()
    ):
        raise InvalidTransition(
            "This paper is not pending Abstract screening in this project.")
    if not locked.decisions.filter(
        stage__project=project, stage__name="Title", decision="ACCEPT"
    ).exists():
        raise InvalidTransition("Accept the title first.")
    if abstract_available(locked.abstract):
        raise InvalidTransition(
            "An abstract is already present. Reload the paper; it was not overwritten."
        )
    previous = locked.abstract
    locked.abstract = value
    _record_manual_source(locked, "abstract")
    if (previous or "").strip():
        locked.raw["manual_metadata"]["abstract"]["replaced_placeholder"] = previous
    locked.save(update_fields=["abstract", "raw", "updated_at"])


def extract_reference_ids(text: str) -> list[str]:
    """Extract resolvable identifier candidates from bibliography text.

    Args:
        text (str): Original multiline bibliography or standalone identifiers.

    Kwargs:
        None

    Returns:
        list[str]: Unique encounter-order identifiers: whole-line OpenAlex
            URLs or S2: IDs, DOI resolver URLs extracted from prose, and modern
            arXiv IDs converted to versionless 10.48550/arxiv DOI URLs. Remove
            trailing prose punctuation while preserving balanced DOI brackets.
            Unrecognized prose contributes no IDs; callers retain the original
            bibliography separately, and no stored-paper match is checked here.
    """
    result = []
    for line in text.splitlines():
        value = line.strip()
        if re.fullmatch(r"https://openalex\.org/W[0-9A-Za-z]+|S2:[0-9A-Za-z-]+", value):
            result.append(value)
            continue
        for arxiv in re.finditer(
            r"(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:)\s*(\d{4}\.\d{4,5})(?!\d)(?:v\d+)?",
            value,
            re.IGNORECASE,
        ):
            result.append("https://doi.org/10.48550/arxiv." + arxiv.group(1))
        # DOI text is extracted from prose, but the full original bibliography is retained.
        for match in re.finditer(r"10\.\d{4,9}/[^\s<>]+", value):
            identifier = match.group(0).rstrip(".,;")
            # Remove prose punctuation without damaging balanced DOI suffixes.
            for opening, closing in (("(", ")"), ("[", "]")):
                while identifier.endswith(closing) and identifier.count(closing) > identifier.count(
                    opening
                ):
                    identifier = identifier[:-1]
            doi = normalize_doi(identifier)
            result.append("https://doi.org/" + doi)
    return list(dict.fromkeys(result))


@transaction.atomic
def complete_references(
    project: Project, paper: Paper, text: str = "", *, reject: bool = False
) -> None:
    """Complete references or audit an Abstract rejection, never both.

    Args:
        project (Project): Project in which paper must be discovered and
            Abstract-accepted but still lack references.
        paper (Paper): Stored identity to reload; leave the caller's instance
            unchanged in memory.

    Kwargs:
        text (str): Bibliography text to strip, retain and extract IDs from.
            Defaults to ""; must be nonblank unless rejecting.
        reject (bool): Correct Abstract to REJECT with an audit reason.
            Ignore text instead of saving references. Defaults to False.

    Returns:
        None

    Raises:
        InvalidTransition: If membership/acceptance fails, references already
            exist, text is blank when saving, or correction validation fails.
        Paper.DoesNotExist: If paper has been deleted.

    Side Effects:
        Save full reference_text, extracted IDs and manual provenance, or
        delegate an audited rejection to correct_decision, in one transaction.
        A bibliography with no recognized IDs still completes references.
    """
    locked = Paper.objects.select_for_update().get(pk=paper.pk)
    if (
        not locked.discoveries.filter(search__project=project).exists()
        or not locked.decisions.filter(
            stage__project=project, stage__name="Abstract", decision="ACCEPT"
        ).exists()
    ):
        raise InvalidTransition(
            "This paper has not passed Abstract screening in this project.")
    if has_reference_list(locked):
        raise InvalidTransition(
            "References are already available. Reload the queue.")
    if reject:
        correct_decision(
            paper=locked,
            stage=project.stages.get(name="Abstract"),
            decision="REJECT",
            reason="Rejected during reference completion: references unavailable.",
        )
        return
    value = text.strip()
    if not value:
        raise InvalidTransition(
            "Paste a reference list before saving, or reject this paper.")
    locked.reference_text = value
    locked.references = extract_reference_ids(value)
    _record_manual_source(locked, "references")
    locked.save(update_fields=["reference_text",
                "references", "raw", "updated_at"])
