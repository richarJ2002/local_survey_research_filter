"""Project-scoped editor for the separate survey-writing graph.

@File: survey_views.py
@Brief: Editable curated snapshots and manual citations.
@Date: 02/10/2026
"""

from urllib.parse import urlencode

import httpx
from django.contrib import messages
from django.core import signing
from django.db import DatabaseError, IntegrityError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from literature.forms import DoiSearchForm, SurveyCitationForm, SurveyEntryForm
from literature.models import Paper, Project, SurveyCitation, SurveyEntry
from literature.services.plots_service import build_citation_figure
from literature.services.reference_service import RefbackfillError, lookup_doi
from literature.services.survey_graph_service import build_survey_graph, prefill, save_survey_entry


@require_http_methods(["GET", "POST"])
def survey_graph(request, pk):
    """Manage chosen entries and directed manual links without changing the draft graph."""
    project = get_object_or_404(Project, pk=pk)
    form = SurveyCitationForm(project=project)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "link":
            form = SurveyCitationForm(request.POST, project=project)
            if form.is_valid():
                try:
                    SurveyCitation.objects.get_or_create(
                        citing=form.cleaned_data["citing"], cited=form.cleaned_data["cited"]
                    )
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
                link.delete()
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
            entry = get_object_or_404(SurveyEntry, pk=entry_pk, project=project)
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
    return render(
        request,
        "literature/survey_graph.html",
        {
            "project": project,
            "entries": project.survey_entries.all(),
            "link_form": form,
            "links": SurveyCitation.objects.filter(
                citing__project=project, cited__project=project
            ).select_related("citing", "cited"),
            "graph_plot": build_citation_figure(graph),
            "automatic_count": len(graph["automatic_edges"]),
        },
    )


@require_http_methods(["GET", "POST"])
def survey_entry(request, pk, entry_id=None):
    """DOI-prefill followed by an entirely editable curated snapshot."""
    project = get_object_or_404(Project, pk=pk)
    entry = get_object_or_404(SurveyEntry, project=project, pk=entry_id) if entry_id else None
    lookup_form = DoiSearchForm()
    initial = {}
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
            )
        }
        initial["version"] = signing.dumps(
            [entry.pk, entry.updated_at.isoformat()], salt="survey-version"
        )
    if request.method == "POST" and request.POST.get("action") == "lookup" and entry is None:
        lookup_form = DoiSearchForm(request.POST)
        if lookup_form.is_valid():
            return redirect(
                reverse("survey_entry_add", args=[pk])
                + "?"
                + urlencode({"doi": lookup_form.cleaned_data["doi"]})
            )
    elif request.method == "POST":
        if request.POST.get("action") != "save":
            return HttpResponse(status=400)
        form = SurveyEntryForm(request.POST)
        if form.is_valid():
            try:
                save_survey_entry(project, form.cleaned_data, entry_id)
            except ValueError as exc:
                form.add_error(None, str(exc))
            except IntegrityError:
                form.add_error(
                    None,
                    "A reference number or DOI conflicts with another save. Reload and try again.",
                )
            except DatabaseError:
                form.add_error(None, "Could not save (database busy). Reload and try again.")
            else:
                messages.success(
                    request, "Curated survey entry saved; draft metadata was not overwritten."
                )
                return redirect("survey_graph", pk=pk)
        return render(
            request,
            "literature/survey_entry.html",
            {"project": project, "entry": entry, "form": form, "lookup_form": lookup_form},
        )
    elif request.GET.get("doi") and not entry:
        lookup_form = DoiSearchForm(request.GET)
        if lookup_form.is_valid():
            doi = lookup_form.cleaned_data["doi"]
            paper = Paper.objects.filter(doi__iexact=doi).first()
            try:
                metadata = {} if paper else lookup_doi(doi)
            except (RefbackfillError, httpx.RequestError, ValueError, TypeError):
                metadata = {}
                messages.warning(
                    request, "DOI lookup was unavailable. You can enter the fields manually."
                )
            initial.update(prefill(project, doi, metadata, paper))
    form = SurveyEntryForm(initial=initial)
    return render(
        request,
        "literature/survey_entry.html",
        {"project": project, "entry": entry, "form": form, "lookup_form": lookup_form},
    )
