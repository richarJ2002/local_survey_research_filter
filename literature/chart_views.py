"""Stable, paginated paper lists matching chart-point semantics; no temporary files.

@File: chart_views.py
@Brief: Project-scoped yearly and running-total previews.
@Date: 02/10/2026
"""

from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_safe

from literature.models import Paper, Project, ScreeningDecision
from literature.services.screening_service import final_decisions


@require_safe
def chart_entries(request, pk):
    """Yearly points mean IN the year; cumulative points mean THROUGH the year."""
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
    papers = Paper.objects.filter(discoveries__search__project=project).distinct()
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
        papers = papers.filter(pk__in=final_decisions(project).values_list("paper_id", flat=True))
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
        {"project": project, "label": label, "page": page, "query": query.urlencode()},
    )
