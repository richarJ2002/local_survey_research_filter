"""Project-scoped editor for the separate survey-writing graph.

@File: survey_views.py
@Brief: Editable curated snapshots and manual citations.
@Date: 02/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from urllib.parse import urlencode

# Custom library Imports
import httpx
from django.contrib import messages
from django.core import signing
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_http_methods

from literature.forms import DoiSearchForm, SurveyCitationForm, SurveyEntryForm
from literature.models import Paper, Project, SurveyCitation, SurveyEntry
from literature.services.plots_service import build_citation_figure
from literature.services.reference_service import RefbackfillError, lookup_doi
from literature.services.survey_graph_service import build_survey_graph, prefill, save_survey_entry

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Project library search ---


def search_project_papers(project: Project, query: str, limit: int = 25) -> list:
    """Match project-discovered papers by title/authors/DOI/venue, deterministic order."""
    q = (query or "").strip()
    if not q:
        return []
    return list(
        Paper.objects.filter(discoveries__search__project=project)
        .filter(
            Q(title__icontains=q)
            | Q(authors__icontains=q)
            | Q(doi__icontains=q)
            | Q(venue__icontains=q)
        )
        .distinct()
        .only("title", "authors", "doi", "publication_year", "venue")
        .order_by("title", "pk")[:limit]
    )


@require_http_methods(["GET", "POST"])
def survey_graph(request: HttpRequest, pk: int) -> HttpResponse:
    """Manage curated entries with the shared citation drawing and controls.

    Args:
        request: Local GET or explicit curated-entry/manual-link POST.
        pk: Project identifier in the fixed owning database.

    Kwargs:
        None

    Returns:
        HttpResponse: Curated graph page, redirect, or invalid-action response.

    POST changes only curated entries and manual links, never draft papers
    or screening decisions. GET renders stored data without provider calls.
    """
    project = get_object_or_404(Project, pk=pk)
    form = SurveyCitationForm(project=project)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "link":
            form = SurveyCitationForm(request.POST, project=project)
            if form.is_valid():
                try:
                    with transaction.atomic():
                        citing = form.cleaned_data["citing"]
                        cited = form.cleaned_data["cited"]
                        SurveyCitation.objects.get_or_create(
                            citing=citing, cited=cited)
                        SurveyEntry.objects.filter(pk=citing.pk).update(
                            updated_at=timezone.now())
                except (IntegrityError, DatabaseError):
                    messages.error(
                        request,
                        "Could not save the manual link (database busy). Reload and try again.",
                    )
                    return redirect("survey_graph", pk=pk)
                messages.success(request, "Manual citation link saved.")
                return redirect("survey_graph", pk=pk)
        elif action == "remove_link":
            try:
                link_pk = int(request.POST.get("link_id"))
            except (TypeError, ValueError):
                return HttpResponse(status=400)
            link = get_object_or_404(
                SurveyCitation, pk=link_pk, citing__project=project, cited__project=project
            )
            try:
                with transaction.atomic():
                    citing_id = link.citing_id
                    link.delete()
                    SurveyEntry.objects.filter(pk=citing_id).update(
                        updated_at=timezone.now())
            except DatabaseError:
                messages.error(
                    request,
                    "Could not remove the manual link (database busy). Reload and try again.",
                )
                return redirect("survey_graph", pk=pk)
            messages.success(
                request,
                "Manual link removed. An independently matched automatic citation may remain.",
            )
            return redirect("survey_graph", pk=pk)
        elif action == "remove_entry":
            try:
                entry_pk = int(request.POST.get("entry_id"))
            except (TypeError, ValueError):
                return HttpResponse(status=400)
            entry = get_object_or_404(
                SurveyEntry, pk=entry_pk, project=project)
            try:
                entry.delete()
            except DatabaseError:
                messages.error(
                    request, "Could not remove the entry (database busy). Reload and try again."
                )
                return redirect("survey_graph", pk=pk)
            messages.success(
                request, "Curated entry removed; the draft library and decisions were not changed."
            )
            return redirect("survey_graph", pk=pk)
        else:
            return HttpResponse(status=400)
    graph = build_survey_graph(project)
    entries = project.survey_entries.all()
    figure = build_citation_figure(
        graph, plot_config={"displayModeBar": False})
    automatic_count = len(graph["automatic_edges"])
    from literature.plotly_assets import plotly_version

    return render(
        request,
        "literature/survey_graph.html",
        {
            "project": project,
            "entries": entries,
            "survey_entries": entries,
            "link_form": form,
            "links": SurveyCitation.objects.filter(
                citing__project=project, cited__project=project
            ).select_related("citing", "cited"),
            "graph_plot": figure,
            "survey_plot": figure,
            "native_chart_controls": True,
            "automatic_count": automatic_count,
            "survey_automatic_count": automatic_count,
            "plotly_version": plotly_version(),
        },
    )


@xframe_options_sameorigin
@require_http_methods(["GET", "POST"])
def survey_entry(
    request: HttpRequest, pk: int, entry_id: int | None = None,
) -> HttpResponse:
    """Preview cached or explicitly fetched details before a separate save.

    Args:
        request: Local GET or CSRF-protected fetch/save POST.
        pk: Project identifier in the fixed owning database.

    Kwargs:
        entry_id: Existing curated entry to edit. Defaults to None for add.

    Returns:
        HttpResponse: Editable independent snapshot or saved-graph response.

    Ordinary GET and saves call no provider. Preview identities bind the
    owning project and worker; fetching never creates research rows.
    """
    project = get_object_or_404(Project, pk=pk)
    entry = get_object_or_404(
        SurveyEntry, project=project, pk=entry_id) if entry_id else None
    popup = request.GET.get("popup") == "1" or request.POST.get("popup") == "1"
    binding = [getattr(request, "worker_uuid", ""),
               getattr(request, "worker_epoch", "")]
    lookup_form = DoiSearchForm()
    initial: dict = {}
    project_papers: list = []
    paper_query = ""
    if entry:
        initial = {
            name: getattr(entry, name)
            for name in (
                "reference_number",
                "title",
                "doi",
                "authors",
                "publication_year",
                "venue",
                "publisher",
                "abstract",
                "reference_text",
                "origin_source",
                "inclusion_origin",
            )
        }
        initial["cited_entries"] = list(
            entry.manual_outgoing.values_list("cited_id", flat=True))
        initial["version"] = signing.dumps(
            [entry.pk, entry.updated_at.isoformat()], salt="survey-version"
        )
    if "q" in request.GET:
        paper_query = request.GET.get("q", "")
        project_papers = search_project_papers(project, paper_query)
    if (request.method == "POST" and entry is None
            and request.POST.get("action") in {"lookup", "fetch"}):
        lookup_form = DoiSearchForm(request.POST)
        if lookup_form.is_valid():
            doi = lookup_form.cleaned_data["doi"]
            if request.POST.get("action") == "lookup":
                params = {"doi": doi}
                if popup:
                    params["popup"] = "1"
                return redirect(reverse("survey_entry_add", args=[pk])
                                + "?" + urlencode(params))
            paper = Paper.objects.filter(doi__iexact=doi).first()
            try:
                metadata = {} if paper else lookup_doi(doi)
            except (RefbackfillError, httpx.RequestError, ValueError, TypeError):
                metadata = {}
                messages.warning(
                    request, "Online DOI fetch failed; nothing was saved. "
                    "Retry or enter details manually.")
            initial.update(prefill(
                project, doi, metadata, paper, binding=binding))
        return render(
            request,
            "literature/survey_entry.html",
            {
                "project": project,
                "entry": entry,
                "form": SurveyEntryForm(
                    initial=initial, project=project, entry=entry),
                "lookup_form": lookup_form,
                "popup": popup,
                "project_papers": project_papers,
                "paper_query": paper_query,
            },
        )
    elif request.method == "POST":
        if request.POST.get("action") != "save":
            return HttpResponse(status=400)
        form = SurveyEntryForm(request.POST, project=project, entry=entry)
        if form.is_valid():
            # Older open forms have no link controls; absence must not erase their manual links.
            if "cited_entries_present" not in request.POST and "cited_entries" not in request.POST:
                form.cleaned_data.pop("cited_entries", None)
            if entry and "inclusion_origin" not in request.POST:
                form.cleaned_data.pop("inclusion_origin", None)
            try:
                saved = save_survey_entry(
                    project, form.cleaned_data, entry_id, binding=binding)
            except ValueError as exc:
                form.add_error(None, str(exc))
            except IntegrityError:
                form.add_error(
                    None,
                    "A reference number or DOI conflicts with another save. Reload and try again.",
                )
            except DatabaseError:
                form.add_error(
                    None, "Could not save (database busy). Reload and try again.")
            else:
                messages.success(
                    request, "Curated survey entry saved; draft metadata was not overwritten."
                )
                if popup:
                    return render(
                        request,
                        "literature/survey_entry_saved.html",
                        {
                            "project": project,
                            "entry": saved,
                            "popup": popup,
                            "graph_url": reverse("survey_graph", args=[pk]),
                        },
                    )
                return redirect("survey_graph", pk=pk)
        return render(
            request,
            "literature/survey_entry.html",
            {
                "project": project,
                "entry": entry,
                "form": form,
                "lookup_form": lookup_form,
                "popup": popup,
                "project_papers": project_papers,
                "paper_query": paper_query,
            },
        )
    elif entry is None and "paper_id" in request.GET:
        raw = request.GET.get("paper_id", "")
        try:
            paper_pk = int(raw)
        except (TypeError, ValueError):
            return HttpResponse(status=400)
        paper = Paper.objects.filter(
            pk=paper_pk, discoveries__search__project=project).first()
        if paper is None:
            return HttpResponse(status=404)
        initial.update(prefill(
            project, paper.doi or "", {}, paper, binding=binding))
    elif request.GET.get("doi") and not entry:
        lookup_form = DoiSearchForm(request.GET)
        if lookup_form.is_valid():
            doi = lookup_form.cleaned_data["doi"]
            paper = Paper.objects.filter(doi__iexact=doi).first()
            # Shared cached metadata may prefill a reading entry, without importing discovery history.
            initial.update(prefill(
                project, doi, {}, paper, binding=binding))
    form = SurveyEntryForm(initial=initial, project=project, entry=entry)
    return render(
        request,
        "literature/survey_entry.html",
        {
            "project": project,
            "entry": entry,
            "form": form,
            "lookup_form": lookup_form,
            "popup": popup,
            "project_papers": project_papers,
            "paper_query": paper_query,
        },
    )
