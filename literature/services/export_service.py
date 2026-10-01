#!/usr/bin/env python3

'''
@File:   export_service.py

@Brief:  Generate human-readable Markdown database from SQLite truth.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

from pathlib import Path
from collections.abc import Iterable

# Custom library Imports
from literature.models import Paper, Project

# ------------------------------------------------------------------------ #
# Markdown export
# ------------------------------------------------------------------------ #

# --- Build markdown for one project ---
def build_markdown(project: Project) -> str:
    """Render all papers with metadata, abstract, discovery and decisions."""
    project.ensure_default_stages()
    stages = list(project.stages.order_by("order"))
    lines: list[str] = [f"# Literature Database — {project.name}", ""]
    if project.description:
        lines += [project.description, ""]

    papers = (
        project.searches.values_list("discoveries__paper_id", flat=True)
        .distinct()
        .order_by()
    )
    # Fall back to decisions-linked papers so exports work even pre-search.
    from literature.models import Paper, ScreeningDecision

    paper_ids = set(pid for pid in papers if pid is not None)
    if not paper_ids:
        paper_ids = set(
            ScreeningDecision.objects.filter(stage__project=project).values_list(
                "paper_id", flat=True
            )
        )
    ordered = Paper.objects.filter(pk__in=paper_ids).order_by("id")
    for paper in ordered:
        lines += [f"## {paper.pk:04d} — {paper.title}", ""]
        lines += [f"**Authors:** {paper.author_display()}  "]
        lines += [f"**Year:** {paper.publication_year or 'n/a'}  "]
        lines += [f"**Venue:** {paper.venue or 'n/a'}  "]
        lines += [f"**DOI:** {paper.doi or 'n/a'}  ", ""]
        if paper.keywords:
            lines += [f"**Keywords:** {', '.join(paper.keywords)}  ", ""]
        lines += ["### Abstract", "", paper.abstract or "_No abstract._", ""]
        from literature.services.screening_service import has_reference_list

        lines += ["### References", "", "Available" if has_reference_list(paper) else "Awaiting reference completion", ""]
        if paper.reference_text:
            lines += [paper.reference_text, ""]
        lines += [f"- {reference}" for reference in paper.references]
        lines += ["### Discovery", ""]
        for disc in paper.discoveries.select_related("search").all():
            lines += [
                f"- Source: {disc.source} | Query: `{disc.query}` "
                f"| Search: {disc.search.name} | Retrieved: {disc.retrieved_at:%Y-%m-%d}"
            ]
        if not list(paper.discoveries.all()):
            lines += ["- _No provenance recorded._"]
        lines += ["", "### Screening", ""]
        for stage in stages:
            dec = paper.decisions.filter(stage=stage).first()
            status = dec.decision if dec else "PENDING"
            lines += [f"- {stage.name}: {status}"]
            if stage.name == "Abstract" and status == "ACCEPT" and not has_reference_list(paper):
                lines += ["  - Content passed; awaiting references, not finally selected."]
        notes = [d.notes for d in paper.decisions.all() if d.notes]
        if notes:
            lines += ["", "### Notes", ""]
            lines += [f"- {n}" for n in notes]
        lines += ["", "---", ""]
    return "\n".join(lines)


# --- Downloadable reference list ---
def build_references_markdown(
    project: Project, papers: Iterable[Paper], *, heading: str,
) -> str:
    """Format one paper group as a compact, human-readable bibliography."""
    ordered = sorted(
        papers,
        key=lambda paper: (-(paper.publication_year or 0), paper.title.casefold(), paper.pk),
    )
    lines = [f"# {heading} — {project.name}", "", f"{len(ordered)} papers", ""]
    if not ordered:
        return "\n".join(lines + ["_No papers in this group._", ""])
    for paper in ordered:
        year = paper.publication_year or "n.d."
        venue = f" {paper.venue.strip()}." if paper.venue.strip() else ""
        doi = f" DOI: {paper.doi}." if paper.doi else ""
        lines.append(
            f"- {paper.author_display()} ({year}). {paper.title.strip().rstrip('.')}."
            f"{venue}{doi} [Paper #{paper.pk}]"
        )
    return "\n".join(lines + [""])


# --- Write markdown to disk ---
def write_markdown(project: Project, output: Path) -> Path:
    """Write markdown export and return the file path."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_markdown(project), encoding="utf-8")
    return output
