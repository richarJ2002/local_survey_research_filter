#!/usr/bin/env python3

'''
@File:   dedup_service.py

@Brief:  Deduplication by DOI -> openalex_id -> normalized title.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.db import transaction

from literature.models import Paper
from literature.providers.mapping import normalize_title

# ------------------------------------------------------------------------ #
# Duplicate lookup / merge
# ------------------------------------------------------------------------ #


def find_duplicate(paper_data: dict) -> tuple[Paper | None, str]:
    """Return (existing Paper or None, match_type).

    Stages in order: same DOI, then same provider ID, then same
    normalized title. Title alone merges (first author/year not
    required); review page exists for manual correction.
    match_type in {"none", "doi", "openalex_id", "title"}.
    """
    raw_doi = paper_data.get("doi") or None
    doi: str | None = None
    if isinstance(raw_doi, str):
        cleaned = raw_doi.strip().lower().replace("https://doi.org/", "").replace(
            "http://doi.org/", ""
        )
        doi = cleaned or None
    if doi:
        existing = Paper.objects.filter(doi__iexact=doi).first()
        if existing is not None:
            return existing, "doi"

    openalex_id = paper_data.get("openalex_id")
    if openalex_id:
        existing = Paper.objects.filter(openalex_id=openalex_id).first()
        if existing is not None:
            return existing, "openalex_id"

    norm = normalize_title(paper_data.get("title") or "")
    if norm:
        for cand in Paper.objects.exclude(title="").only("id", "title"):
            if normalize_title(cand.title) == norm:
                return cand, "title"
    return None, "none"


# ------------------------------------------------------------------------ #
# Review groups (same title) + merge / remove
# ------------------------------------------------------------------------ #

# --- Duplicate groups ---
def duplicate_groups(project) -> list[list[Paper]]:
    """Group project papers by normalized title (len>1), A-Z.

    Start rule per user: same titles (+ authors shown for confirmation).
    """
    from literature.models import Discovery

    paper_ids = set(
        Discovery.objects.filter(search__project=project).values_list(
            "paper_id", flat=True
        )
    )
    if not paper_ids:
        return []
    buckets: dict[str, list[Paper]] = {}
    for paper in Paper.objects.filter(pk__in=paper_ids).only(
        "id", "title", "authors", "publication_year", "venue", "doi"
    ):
        key = normalize_title(paper.title)
        if not key:
            continue
        buckets.setdefault(key, []).append(paper)
    groups = [sorted(v, key=lambda p: p.id) for v in buckets.values() if len(v) > 1]
    groups.sort(key=lambda g: normalize_title(g[0].title))
    return groups


# --- Merge papers ---
@transaction.atomic
def merge_papers(*, keep: Paper, drop: Paper) -> Paper:
    """Merge drop into keep: sources, provenance, decisions; delete drop."""
    from literature.models import Discovery, PaperKeywordAssignment, ScreeningDecision

    merge_sources(keep, {
        "sources": list(drop.sources or []),
        "source_ids": dict(drop.source_ids or {}),
        "abstract": drop.abstract,
        "venue": drop.venue,
        "citation_count": drop.citation_count or 0,
        "references": drop.references,
        "reference_text": drop.reference_text,
    })
    for disc in drop.discoveries.all():
        Discovery.objects.get_or_create(
            paper=keep,
            source=disc.source,
            search=disc.search,
            defaults={
                "search_run": disc.search_run,
                "query": disc.query,
                "raw_payload": disc.raw_payload,
            },
        )
    for dec in drop.decisions.select_related("stage").all():
        existing = ScreeningDecision.objects.filter(
            paper=keep, stage=dec.stage
        ).first()
        if existing is None:
            dec.paper = keep
            dec.save(update_fields=["paper", "updated_at"])
        elif (
            existing.decision == ScreeningDecision.Decision.PENDING
            and dec.decision != ScreeningDecision.Decision.PENDING
        ):
            existing.decision = dec.decision
            existing.notes = dec.notes or existing.notes
            existing.reason = dec.reason or existing.reason
            existing.decided_at = dec.decided_at or existing.decided_at
            existing.save()
    # Preserve project assignments when the retained record does not have one.
    # For an explicit keep/drop conflict, the retained paper's assignment wins;
    # discoveries and their original keyword queries remain the audit trail.
    for assignment in drop.keyword_assignments.all():
        PaperKeywordAssignment.objects.get_or_create(
            paper=keep, project=assignment.project,
            defaults={"search": assignment.search, "keyword": assignment.keyword},
        )
    drop.delete()
    keep.refresh_from_db()
    return keep


def merge_sources(existing: Paper, paper_data: dict) -> bool:
    """Merge source metadata into existing paper. Returns True if saved."""
    changed = False
    existing_sources = list(existing.sources or [])
    for src in paper_data.get("sources") or []:
        if src not in existing_sources:
            existing_sources.append(src)
            changed = True
    if changed:
        existing.sources = existing_sources

    existing_ids = dict(existing.source_ids or {})
    for key, val in (paper_data.get("source_ids") or {}).items():
        if val and key not in existing_ids:
            existing_ids[key] = val
            changed = True
    if changed:
        existing.source_ids = existing_ids

    # Fill blanks only, never overwrite curated data.
    if not existing.abstract and paper_data.get("abstract"):
        existing.abstract = paper_data["abstract"]
        changed = True
    if not existing.venue and paper_data.get("venue"):
        existing.venue = paper_data["venue"]
        changed = True
    for field in ("references", "reference_text"):
        if not getattr(existing, field) and paper_data.get(field):
            setattr(existing, field, paper_data[field])
            changed = True
    if (existing.citation_count or 0) < (paper_data.get("citation_count") or 0):
        existing.citation_count = paper_data["citation_count"]
        changed = True
    if changed:
        existing.save(
            update_fields=["sources", "source_ids", "abstract", "venue", "references", "reference_text", "citation_count", "updated_at"]
        )
    return changed
