#!/usr/bin/env python3

'''
@File:   metrics_service.py

@Brief:  PRISMA-style metrics from immutable audit rows (no extra tables).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.db.models import Count, Q

from literature.models import Discovery, ScreeningDecision, SearchRun
from literature.services.screening_service import final_decisions, pending_references

# ------------------------------------------------------------------------ #
# Metrics
# ------------------------------------------------------------------------ #

# --- Build metrics ---
def build_metrics(project) -> dict:
    """Compute identification -> dedup -> screening funnel for a project."""
    runs = SearchRun.objects.filter(search__project=project, status="completed")
    from django.db.models import Sum

    sums = runs.values("source").annotate(
        results_sum=Sum("results_count"),
        new_sum=Sum("new_papers_count"),
        dups_sum=Sum("duplicates_count"),
        noabs_sum=Sum("no_abstract_count"),
        incomplete_sum=Sum("incomplete_count"),
        run_count=Count("id"),
    )
    per_source = {}
    for row in sums:
        per_source[row["source"]] = {
            "runs": row["run_count"],
            "results": int(row["results_sum"] or 0),
            "new": int(row["new_sum"] or 0),
            "dups": int(row["dups_sum"] or 0),
            "no_abstract": int(row["noabs_sum"] or 0),
            "incomplete": int(row["incomplete_sum"] or 0),
        }

    total_identified = sum(v["results"] for v in per_source.values())
    total_dups = sum(v["dups"] for v in per_source.values())
    total_no_abstract = sum(v["no_abstract"] for v in per_source.values())
    total_incomplete = sum(v["incomplete"] for v in per_source.values())

    paper_ids = set(
        Discovery.objects.filter(search__project=project).values_list(
            "paper_id", flat=True
        )
    )
    unique_total = len(paper_ids)
    multi_source = (
        Discovery.objects.filter(paper_id__in=paper_ids)
        .values("paper_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .count()
        if paper_ids
        else 0
    )

    stages: dict[str, dict[str, int]] = {}
    for row in (
        ScreeningDecision.objects.filter(stage__project=project)
        .values("stage__name")
        .annotate(
            pending=Count("id", filter=Q(decision="PENDING")),
            accept=Count("id", filter=Q(decision="ACCEPT")),
            reject=Count("id", filter=Q(decision="REJECT")),
        )
    ):
        stages[row["stage__name"]] = {
            "pending": row["pending"],
            "accept": row["accept"],
            "reject": row["reject"],
        }

    final_included = final_decisions(project).count()
    reference_pending = pending_references(project).count()
    abstract_counts = stages.setdefault("Abstract", {"pending": 0, "accept": 0, "reject": 0})
    abstract_counts["accept"] = final_included
    abstract_counts["pending"] += reference_pending
    title_counts = stages.get("Title", {})
    title_not_queued = max(unique_total - sum(title_counts.get(status, 0) for status in ("pending", "accept", "reject")), 0)

    return {
        "per_source": per_source,
        "total_identified": total_identified,
        "total_dups": total_dups,
        "total_no_abstract": total_no_abstract,
        "total_incomplete": total_incomplete,
        "unique_total": unique_total,
        "multi_source": multi_source,
        "stages": stages,
        "final_included": final_included,
        "reference_pending": reference_pending,
        "title_not_queued": title_not_queued,
    }


# ------------------------------------------------------------------------ #
# Keyword hits per stage
# ------------------------------------------------------------------------ #

# --- Paper haystack ---
def _haystack(paper) -> str:
    """Lowercased searchable text for keyword attribution."""
    parts = [str(paper.title or ""), str(paper.abstract or "")]
    for kw in paper.keywords or []:
        parts.append(str(kw))
    return " ".join(parts).lower()


# --- Keyword hits ---
def keyword_stage_counts(project) -> dict[str, dict[str, int]]:
    """Count unique papers hitting each keyword row, per filter stage.

    Attribution is corpus-based (title/abstract/keywords contain the row),
    so no extra API calls are needed and all providers share the same
    definition. Title/Abstract counts are subsets of Found.
    """
    from literature.models import Paper, PaperKeywordAssignment

    keywords: list[str] = []
    seen: set[str] = set()
    for search in project.searches.all().only("keywords"):
        for raw in search.keywords or []:
            text = str(raw).strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                keywords.append(text)
    keywords.sort(key=str.lower)
    if not keywords:
        return {}

    paper_ids = set(
        Discovery.objects.filter(search__project=project).values_list(
            "paper_id", flat=True
        )
    )
    if not paper_ids:
        return {kw: {"found": 0, "title": 0, "abstract": 0} for kw in keywords}

    papers = list(Paper.objects.filter(pk__in=paper_ids))
    assigned_keywords = dict(PaperKeywordAssignment.objects.filter(
        project=project, paper_id__in=paper_ids,
    ).values_list("paper_id", "keyword"))
    title_ok = set(
        ScreeningDecision.objects.filter(
            stage__project=project, stage__name="Title", decision="ACCEPT"
        ).values_list("paper_id", flat=True)
    )
    abstract_ok = set(final_decisions(project).values_list("paper_id", flat=True))

    result: dict[str, dict[str, int]] = {}
    lowered = {kw: kw.lower() for kw in keywords}
    for kw in keywords:
        needle = lowered[kw]
        found = title_c = abstract_c = 0
        for paper in papers:
            # An explicit project assignment takes precedence over inferred text matches.
            assigned = assigned_keywords.get(paper.pk)
            if (assigned.lower() != needle if assigned else needle not in _haystack(paper)):
                continue
            found += 1
            if paper.pk in title_ok:
                title_c += 1
            if paper.pk in abstract_ok:
                abstract_c += 1
        result[kw] = {"found": found, "title": title_c, "abstract": abstract_c}
    return result


# ------------------------------------------------------------------------ #
# Cumulative by year per stage
# ------------------------------------------------------------------------ #

# --- Stage yearly series ---
def stage_year_cumulative(project) -> dict:
    """Running totals per year for Found / Title-kept / Abstract-kept.

    Undated papers are excluded from lines but counted in the footnote
    (total undated, and how many of those passed each check).
    """
    from literature.models import Paper

    paper_ids = set(
        Discovery.objects.filter(search__project=project).values_list(
            "paper_id", flat=True
        )
    )
    if not paper_ids:
        return {"years": [], "series": {}, "yearly": {}, "undated": {"found": 0, "title": 0, "abstract": 0}}

    papers = list(
        Paper.objects.filter(pk__in=paper_ids).only(
            "id", "publication_year", "title"
        )
    )
    title_ok = set(
        ScreeningDecision.objects.filter(
            stage__project=project, stage__name="Title", decision="ACCEPT"
        ).values_list("paper_id", flat=True)
    )
    abstract_ok = set(final_decisions(project).values_list("paper_id", flat=True))

    undated = {"found": 0, "title": 0, "abstract": 0}
    per_year: dict[int, dict[str, int]] = {}
    for paper in papers:
        year = paper.publication_year
        if not year:
            undated["found"] += 1
            if paper.pk in title_ok:
                undated["title"] += 1
            if paper.pk in abstract_ok:
                undated["abstract"] += 1
            continue
        bucket = per_year.setdefault(year, {"found": 0, "title": 0, "abstract": 0})
        bucket["found"] += 1
        if paper.pk in title_ok:
            bucket["title"] += 1
        if paper.pk in abstract_ok:
            bucket["abstract"] += 1

    years = sorted(per_year)
    series: dict[str, list[int]] = {"found": [], "title": [], "abstract": []}
    yearly: dict[str, list[int]] = {"found": [], "title": [], "abstract": []}
    run_f = run_t = run_a = 0
    for year in years:
        yearly["found"].append(per_year[year]["found"])
        yearly["title"].append(per_year[year]["title"])
        yearly["abstract"].append(per_year[year]["abstract"])
        run_f += per_year[year]["found"]
        run_t += per_year[year]["title"]
        run_a += per_year[year]["abstract"]
        series["found"].append(run_f)
        series["title"].append(run_t)
        series["abstract"].append(run_a)
    return {"years": years, "series": series, "yearly": yearly, "undated": undated}
