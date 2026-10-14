"""
Preview project-scoped chart entries and stored-content revision stamps.

Yearly points mean IN; running totals mean THROUGH. Final selections require
reference completion, and previews create no temporary exports.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   chart_views.py

@Brief:  Preview project-scoped chart entries and stored-content revision stamps.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Custom library Imports
from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_safe

from literature.models import Paper, Project, ScreeningDecision
from literature.services.screening_service import final_decisions

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Functions


@require_safe
def project_revision(request, pk):
    """
    Return a stored-content revision stamp for same-project revalidation.

    Args:
        request (HttpRequest): Safe request; method validation is delegated
            to require_safe.
        pk (int): Primary key of the project to fingerprint.

    Kwargs:
        None

    Returns:
        JsonResponse: revision key containing the project's content hash;
            the decorator rejects methods other than GET/HEAD.

    Raises:
        Http404: If the project does not exist.

    Side Effects:
        Read stored project-related rows; no providers or database writes.
    """
    from literature.services.metrics_service import project_revision as stamp

    project = get_object_or_404(Project, pk=pk)
    return JsonResponse({"revision": stamp(project)})


@require_safe
def chart_entries(request, pk):
    """
    List discovered papers matching a yearly or cumulative chart point.

    Args:
        request (HttpRequest): Safe request with GET filters: step defaults to
            found (also title/abstract), mode to year (also through), and year
            is required in 1..9999. format=json selects a ten-item preview;
            otherwise page selects a 25-item HTML page.
        pk (int): Primary key of the project bounding discovery and decisions.

    Kwargs:
        None

    Returns:
        HttpResponse: HTML page or JSON label/count/papers (id/title/year/url),
            ordered by descending year, title and PK. Invalid filters/year
            return 400; require_safe rejects non-GET/HEAD methods. Undated
            papers never match; abstract uses Abstract ACCEPT with references
            or nonblank bibliography, excluding reference-pending accepts.

    Raises:
        Http404: If the project does not exist.

    Side Effects:
        Read project papers/decisions only; no temporary files or saves.
    """
    project = get_object_or_404(Project, pk=pk)
    step = request.GET.get("step", "found")
    mode = request.GET.get("mode", "year")
    if step not in {"found", "title", "abstract"} or mode not in {"year", "through"}:
        return HttpResponseBadRequest("Invalid chart filter.")
    try:
        year = int(request.GET.get("year", ""))
        if not 1 <= year <= 9999:
            raise ValueError
    except (TypeError, ValueError):
        return HttpResponseBadRequest("A valid publication year is required.")
    papers = Paper.objects.filter(
        discoveries__search__project=project).distinct()
    papers = (
        papers.filter(publication_year=year)
        if mode == "year"
        else papers.filter(publication_year__lte=year)
    )
    if step == "title":
        ids = ScreeningDecision.objects.filter(
            stage__project=project, stage__name="Title", decision="ACCEPT"
        ).values_list("paper_id", flat=True)
        papers = papers.filter(pk__in=ids)
    elif step == "abstract":
        papers = papers.filter(pk__in=final_decisions(
            project).values_list("paper_id", flat=True))
    papers = papers.order_by("-publication_year", "title", "pk")
    label = {
        "found": "Found papers",
        "title": "Title-passed papers",
        "abstract": "Finally selected papers",
    }[step]
    label += f" published {'in' if mode == 'year' else 'through'} {year}"
    if request.GET.get("format") == "json":
        return JsonResponse(
            {
                "label": label,
                "count": papers.count(),
                "papers": [
                    {
                        "id": paper.pk,
                        "title": paper.title,
                        "year": paper.publication_year,
                        "url": reverse("paper_manage", args=[pk, paper.pk]),
                    }
                    for paper in papers[:10]
                ],
            }
        )
    page = Paginator(papers, 25).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    query.pop("format", None)
    return render(
        request,
        "literature/chart_entries.html",
        {"project": project, "label": label,
            "page": page, "query": query.urlencode()},
    )
