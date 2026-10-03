#!/usr/bin/env python3

'''
@File:   views.py

@Brief:  Project, search, screening and export views (no login, local-first).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

import json
import os
from pathlib import Path

# Custom library Imports
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST, require_http_methods
from dotenv import set_key

from literature.forms import (
    DoiSearchForm, LookupPaperForm, ManualPaperForm, MissingMetadataForm, PaperDetailsForm, ReferenceCompletionForm,
    ProjectForm, SearchForm, SettingsForm, StatusCorrectionForm,
)
from literature.models import (
    Discovery,
    OpenCitationsLookup,
    Paper,
    PaperKeywordAssignment,
    Project,
    ScreeningDecision,
    Search,
    Stage,
)
from literature.services import screening_service
from literature.services.dedup_service import duplicate_groups, merge_papers
from literature.services.export_service import build_markdown, build_references_markdown
from literature.services.metrics_service import build_metrics, display_sources, keyword_stage_counts, stage_year_cumulative
from literature.services.manual_paper_service import (
    add_manual_paper, finish_manual_addition, selected_keyword, update_paper_details,
)
from literature.services.plots_service import (
    build_current_source_bars,
    build_funnel_figure,
    build_keyword_figure,
    build_source_pie,
    build_stage_cumulative_figure,
    build_yearly_figure,
)
from literature.services.search_service import run_search

# ------------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------------ #

# --- Env path ---
def _env_path() -> Path:
    """Launcher uses private credentials; developer retains its source .env."""
    return Path(getattr(settings, "LOCAL_SURVEY_CREDENTIALS_FILE", Path(settings.BASE_DIR) / ".env"))


# --- Email configured check ---
def _email_configured() -> bool:
    """Check polite-pool email from settings (restart applies .env edits)."""
    return bool((getattr(settings, "OPENALEX_EMAIL", "") or "").strip())


# --- Stage bootstrap on reads: legacy creates on demand, workers never write ---
def _ensure_stages(request: HttpRequest, project) -> None:
    """Ensure Title/Abstract stages for a read path without GET writes.

    Developer keeps the legacy on-demand bootstrap. A worker store is
    created with its stages by the creation bootstrap, so a worker GET
    never writes; a stageless worker store fails closed downstream instead
    of silently mutating on a safe method.
    """
    from literature.worker import role as worker_role

    if worker_role() == "worker":
        return
    project.ensure_default_stages()


# ------------------------------------------------------------------------ #
# Projects
# ------------------------------------------------------------------------ #

# --- Project list ---
def project_list(request: HttpRequest) -> HttpResponse:
    """List all survey projects (developer) or serve the workspace landing (manager)."""
    from literature.worker import role

    if role() == "manager":
        # ponytail: manager holds no research rows; the landing lists manifests only.
        from literature import workspace_views

        return workspace_views.workspace_home(request)
    projects = Project.objects.order_by("-updated_at")
    email_ok = _email_configured()
    return render(
        request,
        "literature/project_list.html",
        {"projects": projects, "email_ok": email_ok},
    )


# --- Project create ---
def project_create(request: HttpRequest) -> HttpResponse:
    """Create a project and seed Title -> Abstract stages."""
    if request.method == "POST":
        form = ProjectForm(request.POST)
        if form.is_valid():
            project = form.save()
            project.ensure_default_stages()
            messages.success(request, f"Created project '{project.name}'.")
            return redirect("project_detail", pk=project.pk)
    else:
        form = ProjectForm()
    return render(request, "literature/project_form.html", {"form": form})


# --- Project delete ---
def project_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Confirm then delete a project and its orphaned papers."""
    project = get_object_or_404(Project, pk=pk)
    if request.method == "POST":
        name = project.name
        project.delete()
        Paper.objects.filter(discoveries__isnull=True).delete()
        from literature.services.reference_service import normalize_doi

        remaining_dois = {
            doi for raw in Paper.objects.exclude(doi__isnull=True).values_list("doi", flat=True)
            if (doi := normalize_doi(raw))
        }
        OpenCitationsLookup.objects.exclude(doi__in=remaining_dois).delete()
        messages.success(request, f"Deleted project '{name}'.")
        return redirect("project_list")
    return render(request, "literature/project_confirm_delete.html", {"project": project})


# --- Project detail / dashboard ---
def project_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Show counts per stage plus searches and run history."""
    project = get_object_or_404(Project, pk=pk)
    _ensure_stages(request, project)
    stages = list(project.stages.order_by("order"))
    aggregated = (
        ScreeningDecision.objects.filter(stage__project=project)
        .values("stage__name")
        .annotate(
            pending=Count("id", filter=Q(decision="PENDING")),
            accept=Count("id", filter=Q(decision="ACCEPT")),
            reject=Count("id", filter=Q(decision="REJECT")),
        )
    )
    counts: dict[str, dict[str, int]] = {
        row["stage__name"]: {
            "pending": row["pending"],
            "accept": row["accept"],
            "reject": row["reject"],
        }
        for row in aggregated
    }
    for stage in stages:
        counts.setdefault(stage.name, {"pending": 0, "accept": 0, "reject": 0})
    searches = project.searches.order_by("-id").prefetch_related("runs")
    metrics = build_metrics(project)
    dup_groups = duplicate_groups(project)
    plots = _plots_context(project, depth=2)
    paper_ids = set(
        Discovery.objects.filter(search__project=project).values_list(
            "paper_id", flat=True
        )
    )
    missing_refs = (
        sum(
            1
            for refs in Paper.objects.filter(pk__in=paper_ids).values_list(
                "references", flat=True
            )
            if not refs
        )
        if paper_ids
        else 0
    )
    return render(
        request,
        "literature/project_detail.html",
        {
            "project": project,
            "stages": stages,
            "counts": counts,
            "searches": searches,
            "metrics": metrics,
            "dup_count": len(dup_groups),
            "missing_refs": missing_refs,
            **plots,
        },
    )


# ------------------------------------------------------------------------ #
# Project paper management
# ------------------------------------------------------------------------ #

def papers_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Search and page through this project's stored papers and statuses."""
    from django.db.models import Case, CharField, F, OuterRef, Subquery, Value, When
    from django.db.models.fields.json import KT
    from django.db.models.functions import Coalesce, Lower, NullIf, Trim

    from literature.services.metrics_service import project_revision as project_revision_stamp
    from literature.services.search_service import is_complete

    project = get_object_or_404(Project, pk=pk)
    _ensure_stages(request, project)
    query = (request.GET.get("q") or "").strip()[:200]
    papers = Paper.objects.filter(discoveries__search__project=project)
    if query:
        papers = papers.filter(Q(title__icontains=query) | Q(doi__icontains=query))
    decisions = ScreeningDecision.objects.filter(stage__project=project, paper_id=OuterRef("pk"))
    papers = papers.alias(
        _title_decision=Subquery(decisions.filter(stage__name="Title").values("decision")[:1]),
        _abstract_decision=Subquery(
            decisions.filter(stage__name="Abstract").values("decision")[:1]
        ),
        _linked_keyword=Subquery(
            PaperKeywordAssignment.objects.filter(
                project=project,
                paper_id=OuterRef("pk"),
            ).values("keyword")[:1]
        ),
    )
    # ponytail: whitelist native ORM ordering before pagination; no client-side table library.
    ordering = {
        "title": Lower(NullIf(Trim("title"), Value(""))),
        "doi": Lower(NullIf(Trim("doi"), Value(""))),
        "year": F("publication_year"),
        # ponytail: first listed source/keyword is the ordering key for multi-valued cells.
        "source": Lower(
            NullIf(
                Trim(
                    Case(
                        When(sources__0="manual", then=KT("sources__1")),
                        default=KT("sources__0"),
                        output_field=CharField(),
                    )
                ),
                Value(""),
            )
        ),
        "keywords": Lower(
            Coalesce(
                NullIf(Trim(F("_linked_keyword")), Value("")),
                NullIf(Trim(KT("keywords__0")), Value("")),
                output_field=CharField(),
            )
        ),
        "title_status": Lower(
            Case(
                When(~Q(title__regex=r"\S"), then=Value("Needs title")),
                When(_title_decision="ACCEPT", then=Value("Yes")),
                When(_title_decision="REJECT", then=Value("No")),
                When(_title_decision="PENDING", then=Value("Pending")),
                default=Value("Not queued"),
                output_field=CharField(),
            )
        ),
        "abstract_status": Lower(
            Case(
                When(
                    _abstract_decision="ACCEPT",
                    then=Case(
                        When(
                            ~Q(references=[]) | Q(reference_text__regex=r"\S"),
                            then=Value("Final selected"),
                        ),
                        default=Value("Passed content; references pending"),
                    ),
                ),
                When(_abstract_decision="REJECT", then=Value("No")),
                When(
                    _abstract_decision="PENDING",
                    then=Case(
                        When(abstract="", then=Value("Pending — abstract needed")),
                        default=Value("Pending"),
                    ),
                ),
                default=Value(None),
                output_field=CharField(),
            )
        ),
    }
    sort = request.GET.get("sort", "year")
    if sort not in ordering:
        sort = "year"
    direction = request.GET.get("direction", "desc" if sort == "year" else "asc")
    if direction not in {"asc", "desc"}:
        direction = "desc" if sort == "year" else "asc"
    order = (
        ordering[sort].asc(nulls_first=True)
        if direction == "asc"
        else ordering[sort].desc(nulls_last=True)
    )
    papers = papers.distinct().order_by(order, Lower("title"), "pk")
    page_query = request.GET.copy()
    page_query.pop("page", None)
    page_query["q"], page_query["sort"], page_query["direction"] = query, sort, direction
    headers = []
    for fields in (
        (("title", "Paper"), ("doi", "DOI")),
        (("year", "Year"),),
        (("source", "Source"), ("keywords", "Keywords")),
        (("title_status", "Title status"),),
        (("abstract_status", "Abstract status"),),
    ):
        links = []
        for key, label in fields:
            params = page_query.copy()
            next_direction = "desc" if sort == key and direction == "asc" else "asc"
            params["sort"], params["direction"] = key, next_direction
            links.append(
                {
                    "label": label,
                    "url": params.urlencode(),
                    "active": sort == key,
                    "next_direction": "ascending" if next_direction == "asc" else "descending",
                }
            )
        headers.append(
            {
                "links": links,
                "aria_sort": ("ascending" if direction == "asc" else "descending")
                if any(link["active"] for link in links)
                else "none",
            }
        )
    page = Paginator(papers, 25).get_page(request.GET.get("page"))
    page.object_list = list(page.object_list.prefetch_related("decisions__stage"))
    linked_keywords = dict(
        PaperKeywordAssignment.objects.filter(
            project=project,
            paper__in=page.object_list,
        ).values_list("paper_id", "keyword")
    )
    rows = []
    for paper in page.object_list:
        decisions = {
            row.stage.name: row.decision
            for row in paper.decisions.all()
            if row.stage.project_id == project.pk
        }
        rows.append(
            {
                "paper": paper,
                "complete": is_complete(paper),
                "title_ready": screening_service.title_ready(paper),
                "references_pending": decisions.get("Abstract") == "ACCEPT"
                and not screening_service.has_reference_list(paper),
                "title_status": decisions.get("Title", "NOT_QUEUED"),
                "abstract_status": decisions.get("Abstract", "NOT_QUEUED"),
                "linked_keyword": linked_keywords.get(paper.pk, ""),
                # Display only: manual is a legacy entry method, not a known discovery origin.
                "display_sources": display_sources(paper.sources),
            }
        )
    return render(
        request,
        "literature/papers.html",
        {
            "project": project,
            "rows": rows,
            "page": page,
            "query": query,
            "headers": headers,
            "sort": sort,
            "direction": direction,
            "page_query": page_query.urlencode(),
            "revision": project_revision_stamp(project),
        },
    )


def paper_add(request: HttpRequest, pk: int) -> HttpResponse:
    """Find a paper by DOI first; manual details stay below as a fallback."""
    import httpx
    from urllib.parse import quote

    from django.urls import reverse

    from literature.services.reference_service import RefbackfillError, enrich_papers
    from literature.services.search_service import is_complete

    project = get_object_or_404(Project, pk=pk)
    _ensure_stages(request, project)
    if request.method == "POST" and request.POST.get("action") == "search":
        search_form = DoiSearchForm(request.POST)
        manual_form = ManualPaperForm(project=project)
        if search_form.is_valid():
            doi = search_form.cleaned_data["doi"]
            return redirect(reverse("paper_lookup", args=[project.pk]) + f"?doi={quote(doi, safe='')}")
    else:
        search_form = DoiSearchForm()
        manual_form = ManualPaperForm(request.POST or None, project=project)
        if request.method == "POST" and manual_form.is_valid():
            try:
                search, keyword = selected_keyword(project, manual_form.cleaned_data["search_keyword"])
                paper, created, discovery = add_manual_paper(
                    project, search, keyword, manual_form.cleaned_data,
                )
            except ValueError as exc:
                manual_form.add_error("search_keyword", str(exc))
            else:
                if paper.doi and not is_complete(paper):
                    try:
                        enrich_papers([paper])
                    except (RefbackfillError, httpx.RequestError, ValueError):
                        messages.warning(request, "DOI lookup failed; fill missing details manually.")
                    paper.refresh_from_db()
                finish_manual_addition(project, paper, discovery, created=created, keyword=keyword)
                messages.success(request, "Paper added to the project keyword." if created else
                                 "Existing paper linked to this project keyword.")
                if not is_complete(paper):
                    messages.warning(request, "Missing abstracts and references can be completed during screening.")
                return redirect("paper_manage", pk=project.pk, paper_id=paper.pk)
    return render(request, "literature/paper_add.html", {
        "project": project, "search_form": search_form, "manual_form": manual_form,
        "has_keywords": bool(manual_form.fields["search_keyword"].choices),
    })


@require_http_methods(["GET", "POST"])
def paper_lookup(request: HttpRequest, pk: int) -> HttpResponse:
    """!
    @brief Preview read-only metadata and save only a keyword assignment and statuses.
    @param request DOI lookup GET or confirmed entry POST.
    @param pk Owning project ID.
    @return Entry panel with field errors, or redirect after a successful atomic save.
    """
    from urllib.parse import urlencode

    import httpx
    from django.core import signing
    from django.db import IntegrityError, OperationalError
    from django.urls import reverse

    from literature.services.paper_lookup_service import (
        build_metadata,
        entry_initial,
        entry_version,
        reference_url,
        save_entry,
    )
    from literature.services.reference_service import RefbackfillError, lookup_doi
    from literature.services.search_service import is_complete

    project = get_object_or_404(Project, pk=pk)
    doi_form = DoiSearchForm({"doi": request.GET.get("doi") or request.POST.get("doi") or ""})
    if not doi_form.is_valid():
        messages.error(request, "Enter a valid DOI to look up.")
        return redirect("paper_add", pk=project.pk)
    doi = doi_form.cleaned_data["doi"]
    # Canonical papers are global, but assignments and screening remain project-specific.
    paper = Paper.objects.filter(doi__iexact=doi).first()
    needs_origin = paper is None or not paper.discoveries.filter(search__project=project).exclude(source="manual").exists()
    metadata = {}
    lookup_error = ""
    preview_token = request.POST.get("preview_token", "")
    refresh_url = reverse("paper_lookup", args=[pk]) + "?" + urlencode({"doi": doi})
    if request.method == "POST":
        if request.POST.get("action") != "save":
            return HttpResponse(status=400)
        form = LookupPaperForm(request.POST, project=project, needs_origin=needs_origin)
        valid = form.is_valid()
        try:
            # Save the exact preview the user saw, not a second, potentially different API result.
            preview = signing.loads(preview_token, salt="paper-entry", max_age=1800)
            if preview["project"] != pk or preview["doi"] != doi:
                raise signing.BadSignature("Preview identity mismatch")
            if getattr(request, "worker_route", False) and (
                preview.get("worker_uuid") != getattr(request, "worker_uuid", "")
                or preview.get("worker_epoch") != getattr(request, "worker_epoch", "")
            ):
                raise signing.BadSignature("Preview worker mismatch")
            metadata = preview["metadata"]
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            form.add_error(None, "This preview has expired or is invalid. Look up the DOI again.")
            valid = False
        if valid:
            try:
                _, created = save_entry(project, doi, metadata, form.cleaned_data, preview["paper_id"], preview.get("version"))
            except ValueError as exc:
                form.add_error(None, str(exc))
            except IntegrityError:
                form.add_error(None, "Another save changed this entry. Look up the DOI again before saving.")
            except OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                form.add_error(None, "Another database save is in progress. Try saving again in a moment.")
            else:
                messages.success(request, "Paper added to the database." if created else "Entry updated.")
                return redirect(refresh_url)
    else:
        if paper is None:
            try:
                metadata = build_metadata(lookup_doi(doi), doi)
            except (RefbackfillError, httpx.RequestError, ValueError):
                lookup_error = "The providers could not return this paper. Retry the lookup or use manual entry."
        preview_token = signing.dumps({
            "project": pk, "doi": doi, "paper_id": paper.pk if paper else None,
            "version": entry_version(project, paper) if paper else None,
            "metadata": metadata,
            "worker_uuid": getattr(request, "worker_uuid", ""),
            "worker_epoch": getattr(request, "worker_epoch", ""),
        }, salt="paper-entry", compress=True)
        form = LookupPaperForm(initial=entry_initial(project, paper), project=project, needs_origin=needs_origin)

    if paper:
        # Display stored metadata without requesting providers or mutating it on update.
        metadata = build_metadata({
            "title": paper.title, "authors": paper.authors, "abstract": paper.abstract,
            "publication_year": paper.publication_year, "venue": paper.venue,
            "references": paper.references,
            "reference_text": paper.reference_text,
        }, doi)
    return render(request, "literature/paper_lookup.html", {
        "project": project, "doi": doi, "metadata": metadata, "paper": paper, "form": form,
        "preview_token": preview_token, "refresh_url": refresh_url, "lookup_error": lookup_error,
        "references": [{"identifier": ref, "url": reference_url(ref)}
                       for ref in metadata.get("references", [])],
        "complete": is_complete(paper if paper else Paper(**metadata)) if metadata else False,
        "has_searches": project.searches.exists(),
        "other_project": paper is not None and not paper.discoveries.filter(search__project=project).exists(),
    })


def paper_manage(request: HttpRequest, pk: int, paper_id: int) -> HttpResponse:
    """Edit a project paper or correct a screening status with a reason."""
    from literature.services.search_service import is_complete

    project = get_object_or_404(Project, pk=pk)
    _ensure_stages(request, project)
    paper = get_object_or_404(
        Paper.objects.filter(discoveries__search__project=project).distinct(), pk=paper_id,
    )
    initial = {
        "title": paper.title, "doi": paper.doi or "",
        "authors": "\n".join(a.get("name", "") for a in paper.authors if isinstance(a, dict)),
        "publication_year": paper.publication_year, "venue": paper.venue,
        "abstract": paper.abstract, "references": "\n".join(paper.references or []),
    }
    details_form = PaperDetailsForm(initial=initial)
    correction_form = StatusCorrectionForm()
    if request.method == "POST" and request.POST.get("action") == "details":
        details_form = PaperDetailsForm(request.POST)
        if details_form.is_valid():
            try:
                update_paper_details(project, paper, details_form.cleaned_data)
            except ValueError as exc:
                details_form.add_error(None, str(exc))
            else:
                messages.success(request, "Paper details saved. Complete papers enter Title screening.")
                return redirect("paper_manage", pk=project.pk, paper_id=paper.pk)
    elif request.method == "POST" and request.POST.get("action") == "status":
        correction_form = StatusCorrectionForm(request.POST)
        if correction_form.is_valid():
            stage = project.stages.get(name=correction_form.cleaned_data["stage"])
            decision_value = correction_form.cleaned_data["decision"]
            reason = f"Updated from paper record ({stage.name}: {decision_value})"
            try:
                screening_service.correct_decision(
                    paper=paper, stage=stage,
                    decision=decision_value,
                    reason=reason,
                )
            except screening_service.InvalidTransition as exc:
                correction_form.add_error(None, str(exc))
            else:
                messages.success(request, "Screening status corrected and recorded in the audit history.")
                return redirect("paper_manage", pk=project.pk, paper_id=paper.pk)
    elif request.method == "POST":
        return HttpResponse(status=400)
    decisions = {row.stage.name: row.decision for row in
                 paper.decisions.select_related("stage").filter(stage__project=project)}
    status_labels = {"ACCEPT": "Yes", "REJECT": "No", "PENDING": "Pending",
                     "NOT_QUEUED": "Not queued"}
    return render(request, "literature/paper_manage.html", {
        "project": project, "paper": paper,
        "details_form": details_form, "correction_form": correction_form,
        "title_status": status_labels[decisions.get("Title", "NOT_QUEUED")],
        "abstract_status": status_labels[decisions.get("Abstract", "NOT_QUEUED")],
        "complete": is_complete(paper),
        "revisions": paper.revisions.select_related("stage").filter(stage__project=project)[:20],
        "shared": paper.discoveries.exclude(search__project=project).exists(),
    })


# ------------------------------------------------------------------------ #
# Search
# ------------------------------------------------------------------------ #

# --- Search create ---
def search_create(request: HttpRequest, pk: int) -> HttpResponse:
    """Add one query configuration to a project."""
    project = get_object_or_404(Project, pk=pk)
    all_sources = ["openalex", "semantic_scholar", "crossref", "arxiv"]
    if request.method == "POST":
        form = SearchForm(request.POST)
        if form.is_valid():
            search = form.save(commit=False)
            search.project = project
            search.source = "openalex"
            search.keywords = form.cleaned_data.get("keywords", [])
            search.venues = form.cleaned_data.get("venues", [])
            search.author_names = form.cleaned_data.get("author_names", [])
            selected = form.cleaned_data.get("sources_selected") or all_sources
            search.sources_selected = selected
            search.save()
            messages.success(
                request,
                f"Saved '{search.name}'. Now press Run search to fetch papers.",
            )
            return redirect("project_detail", pk=project.pk)
    else:
        form = SearchForm(initial={"sources_selected": all_sources})
    return render(
        request,
        "literature/search_form.html",
        {"form": form, "project": project, "editing": False},
    )


# --- Search update ---
def search_update(request: HttpRequest, pk: int, search_id: int) -> HttpResponse:
    """Reconfigure an existing search without creating a new one."""
    project = get_object_or_404(Project, pk=pk)
    search = get_object_or_404(Search, pk=search_id, project=project)
    all_sources = ["openalex", "semantic_scholar", "crossref", "arxiv"]
    if request.method == "POST":
        form = SearchForm(request.POST, instance=search)
        if form.is_valid():
            updated = form.save(commit=False)
            updated.project = project
            updated.keywords = form.cleaned_data.get("keywords", [])
            updated.venues = form.cleaned_data.get("venues", [])
            updated.author_names = form.cleaned_data.get("author_names", [])
            selected = form.cleaned_data.get("sources_selected") or all_sources
            updated.sources_selected = selected
            updated.save()
            messages.success(
                request,
                f"Updated '{updated.name}'. Press Run search to refresh results.",
            )
            return redirect("project_detail", pk=project.pk)
    else:
        form = SearchForm(
            instance=search,
            initial={
                "keywords_json": json.dumps(search.keywords or []),
                "venues_json": json.dumps(search.venues or []),
                "author_names_json": json.dumps(search.author_names or []),
                "sources_selected": search.sources_selected or all_sources,
            },
        )
    return render(
        request,
        "literature/search_form.html",
        {"form": form, "project": project, "editing": True, "search": search},
    )


# --- Search run ---
@require_POST
def search_run(request: HttpRequest, pk: int, search_id: int) -> HttpResponse:
    """Execute OpenAlex + Semantic Scholar fetch. Gate on email configured."""
    project = get_object_or_404(Project, pk=pk)
    search = get_object_or_404(Search, pk=search_id, project=project)
    if not _email_configured():
        messages.warning(request, "Add your email in Settings first (OpenAlex polite pool).")
        if getattr(request, "manager_base", ""):
            return redirect(f"{request.manager_base}/settings/")
        return redirect("settings_view")
    try:
        runs = run_search(search.pk)
    except Exception as exc:  # boundary: surface provider error as message
        messages.error(request, f"Search failed: {exc}")
        return redirect("project_detail", pk=project.pk)
    parts = []
    for run in runs:
        if run.status == "completed":
            parts.append(
                f"{run.source}: {run.results_count} results "
                f"({run.new_papers_count} new, {run.duplicates_count} dups, "
                f"{run.incomplete_count} incomplete)"
            )
        else:
            parts.append(f"{run.source}: failed ({run.error_message[:120]})")
    messages.success(request, "Search done — " + "; ".join(parts) + ".")
    return redirect("project_detail", pk=project.pk)


# --- Backfill missing fields ---
@require_POST
def refs_backfill(request: HttpRequest, pk: int) -> HttpResponse:
    """Fill blank title/abstract/references for the project's DOI papers."""
    from literature.services.reference_service import backfill_missing_references

    project = get_object_or_404(Project, pk=pk)
    try:
        result = backfill_missing_references(project)
    except Exception as exc:  # boundary: surface provider error as message
        messages.error(request, f"Backfill failed: {exc}")
        return redirect("project_detail", pk=project.pk)
    from literature.services.search_service import gate_papers

    project.ensure_default_stages()
    gate_papers(list(Paper.objects.filter(discoveries__search__project=project).distinct().only("id", "title")), project.stages.get(name="Title"))
    messages.success(
        request,
        f"Checked {result['checked']} papers, filled {result['filled']} "
        f"({result['batches']} requests).",
    )
    return redirect("project_detail", pk=project.pk)


# --- Enrich project citation edges explicitly ---
@require_POST
def citations_enrich(request: HttpRequest, pk: int) -> HttpResponse:
    """Fetch a bounded batch of DOI citation links; GET never calls an API."""
    from literature.services.opencitations_service import enrich_project

    project = get_object_or_404(Project, pk=pk)
    result = enrich_project(project)
    if result["rate_limited"]:
        messages.warning(request, "OpenCitations rate limited this batch; try again later.")
    elif result["failed"]:
        messages.warning(request, f"{result['failed']} citation lookups failed; retry later.")
    messages.success(
        request,
        f"OpenCitations: {result['checked']} lookups completed, "
        f"{result['matched']} within-project DOI links found in this batch; "
        f"{result['remaining']} lookups remain."
    )
    return redirect("project_detail", pk=project.pk)


# ------------------------------------------------------------------------ #
# Settings (account prompt)
# ------------------------------------------------------------------------ #

# --- Settings view ---
def settings_view(request: HttpRequest) -> HttpResponse:
    """Explicit provider settings; save to the selected credential file, never DB."""
    env_file = _env_path()
    initial = {
        "openalex_email": getattr(settings, "OPENALEX_EMAIL", ""),
        "semantic_scholar_key": getattr(settings, "SEMANTIC_SCHOLAR_KEY", ""),
    }
    if request.method == "POST":
        form = SettingsForm(request.POST)
        if form.is_valid():
            try:
                if os.getenv("WP_LAUNCHER") == "1":
                    env_file.touch(mode=0o600, exist_ok=True)
                set_key(str(env_file), "OPENALEX_EMAIL", form.cleaned_data["openalex_email"])
                if form.cleaned_data.get("semantic_scholar_key"):
                    set_key(
                        str(env_file),
                        "SEMANTIC_SCHOLAR_KEY",
                        form.cleaned_data["semantic_scholar_key"],
                    )
                if form.cleaned_data.get("opencitations_token"):
                    set_key(str(env_file), "OPENCITATIONS_TOKEN",
                             form.cleaned_data["opencitations_token"])
            except OSError as exc:
                form.add_error(None, f"Cannot save the credential file: {exc}")
                return render(request, "literature/settings.html", {"form": form})
            message = ("Saved to private credentials (restart launcher to apply)."
                       if os.getenv("WP_LAUNCHER") == "1" else
                       "Saved locally to .env (restart server to apply).")
            messages.success(request, message)
            return redirect("project_list")
    else:
        form = SettingsForm(initial=initial)
    return render(
        request, "literature/settings.html", {
            "form": form,
            "env_exists": env_file.exists(),
            "opencitations_token_configured": bool(getattr(settings, "OPENCITATIONS_TOKEN", "")),
        }
    )


# ------------------------------------------------------------------------ #
# Screening: Round 1 title+metadata, Round 2 abstract
# ------------------------------------------------------------------------ #

# --- Next pending helper ---
def _next_pending(stage: Stage, exclude_id: int | None = None) -> Paper | None:
    """Return a random PENDING paper (order shuffled to reduce bias).

    Progress stays saved per decision row, so the queue is resumable:
    decided papers leave the pool, pending count only shrinks.
    """
    qs = Paper.objects.filter(
        decisions__stage=stage, decisions__decision="PENDING"
    ).order_by("?")
    if exclude_id is not None:
        qs = qs.exclude(pk=exclude_id)
    return qs.first()


# --- Screening counts helper ---
def _progress(stage: Stage) -> dict[str, int]:
    """Return done/total counts for progress display."""
    total = ScreeningDecision.objects.filter(stage=stage).count()
    done = ScreeningDecision.objects.filter(stage=stage).exclude(decision="PENDING").count()
    pending = total - done
    return {"done": done, "total": total, "pending": pending}


# --- Screening view ---
@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def screening_view(request: HttpRequest, pk: int, stage_name: str) -> HttpResponse:
    """Show one paper at a time for Title or Abstract stage."""
    project = get_object_or_404(Project, pk=pk)
    _ensure_stages(request, project)
    normalized = stage_name.capitalize()
    stage = get_object_or_404(Stage, project=project, name=normalized)
    metadata_form = None
    paper = None
    if request.method == "POST":
        if stage.name != "Abstract":
            return HttpResponse(status=400)
        try:
            paper_id = int(request.POST.get("paper_id", ""))
        except (TypeError, ValueError):
            return HttpResponse(status=400)
        if not 0 < paper_id <= 2**63 - 1:
            return HttpResponse(status=400)
        paper = get_object_or_404(Paper.objects.filter(decisions__stage=stage, decisions__decision="PENDING"), pk=paper_id)
        metadata_form = MissingMetadataForm(request.POST, paper=paper)
        if metadata_form.is_valid():
            from django.db import IntegrityError

            from literature.services.reference_review_service import save_missing_metadata

            try:
                save_missing_metadata(project, paper, metadata_form.cleaned_data)
            except screening_service.InvalidTransition as exc:
                metadata_form.add_error(None, str(exc))
            except IntegrityError:
                metadata_form.add_error(None, "Another entry now uses this identifier. Reload and review duplicates; no details were saved.")
            else:
                messages.success(request, "Missing details saved. Continue screening this paper.")
                from django.urls import reverse

                return redirect(reverse("screening_view", args=[pk, "abstract"]) + f"?paper={paper.pk}")
    elif request.GET.get("paper"):
        if not request.GET["paper"].isdigit():
            return HttpResponse(status=400)
        paper = get_object_or_404(Paper.objects.filter(decisions__stage=stage, decisions__decision="PENDING"), pk=request.GET["paper"])
    paper = paper or _next_pending(stage)
    if paper is None:
        return redirect("screening_complete", pk=project.pk, stage_name=stage.name.lower())
    progress = _progress(stage)
    current_index = progress["done"] + 1
    metadata_form = metadata_form or MissingMetadataForm(paper=paper, initial={"paper_id": paper.pk})
    extra_metadata = [field for field in metadata_form if field.name not in {"paper_id", "abstract"}]
    return render(
        request,
        "literature/screening.html",
        {
            "project": project,
            "stage": stage,
            "paper": paper,
            "progress": progress,
            "current_index": current_index,
            "is_title_round": stage.name == "Title",
            "is_abstract_round": stage.name == "Abstract",
            "abstract_form": metadata_form,
            "extra_metadata_fields": extra_metadata,
            "missing_abstract": not screening_service.abstract_available(paper.abstract),
        },
    )


# --- Screening decide (JSON) ---
@require_POST
def screening_decide(request: HttpRequest) -> JsonResponse:
    """Apply Yes/No decision via fetch(). Returns next paper URL."""
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Expected a JSON object."}, status=400)
    if not all(str(payload.get(key, "")).isdigit() for key in ("paper_id", "stage_id")):
        return JsonResponse({"error": "Valid paper and stage IDs are required."}, status=400)
    paper = get_object_or_404(Paper, pk=payload.get("paper_id"))
    stage = get_object_or_404(Stage, pk=payload.get("stage_id"))
    decision = str(payload.get("decision", "")).upper()
    if decision not in {"ACCEPT", "REJECT"}:
        return JsonResponse({"error": "Decision must be ACCEPT or REJECT."}, status=400)
    try:
        screening_service.decide(
            paper=paper,
            stage=stage,
            decision=decision,
            notes=str(payload.get("notes", "")),
        )
    except screening_service.InvalidTransition as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    nxt = _next_pending(stage)
    if nxt is None:
        if stage.name == "Abstract" and screening_service.pending_references(stage.project).exists():
            from django.urls import reverse

            return JsonResponse({"success": True, "next_paper_url": reverse("reference_review", args=[stage.project_id])})
        from django.urls import reverse

        return JsonResponse(
            {
                "success": True,
                # ponytail: reverse() keeps the worker UUID/epoch script
                # prefix; hardcoded /project paths would lose it.
                "next_paper_url": reverse(
                    "screening_complete", args=[stage.project_id, stage.name.lower()]
                ),
            }
        )
    from django.urls import reverse

    return JsonResponse(
        {
            "success": True,
            "next_paper_url": reverse(
                "screening_view", args=[stage.project_id, stage.name.lower()]
            ),
        }
    )


# --- Screening complete ---
def screening_complete(request: HttpRequest, pk: int, stage_name: str) -> HttpResponse:
    """Empty-state page when a stage queue is done."""
    project = get_object_or_404(Project, pk=pk)
    stage = get_object_or_404(Stage, project=project, name=stage_name.capitalize())
    progress = _progress(stage)
    has_searches = project.searches.exists()
    has_runs = project.searches.filter(runs__isnull=False).exists()
    has_papers = ScreeningDecision.objects.filter(stage__project=project).exists()
    return render(
        request,
        "literature/screening_complete.html",
        {
            "project": project,
            "stage": stage,
            "progress": progress,
            "has_searches": has_searches,
            "has_runs": has_runs,
            "has_papers": has_papers,
            "email_ok": _email_configured(),
            "reference_pending": screening_service.pending_references(project).count(),
        },
    )


@require_http_methods(["GET", "POST"])
def reference_review(request: HttpRequest, pk: int) -> HttpResponse:
    """Resolve missing bibliographies only after Abstract content screening has passed."""
    from literature.services.reference_review_service import complete_references

    project = get_object_or_404(Project, pk=pk)
    pending = screening_service.pending_references(project)
    paper = None
    form = ReferenceCompletionForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action not in {"save", "reject", "retry"} or not str(request.POST.get("paper_id", "")).isdigit():
            return HttpResponse(status=400)
        accepted = ScreeningDecision.objects.filter(stage__project=project, stage__name="Abstract", decision="ACCEPT", paper__discoveries__search__project=project).distinct()
        decision = get_object_or_404(accepted.select_related("paper"), paper_id=request.POST["paper_id"])
        paper = decision.paper
        if screening_service.has_reference_list(paper):
            messages.info(request, "References are already available. No metadata or decision was overwritten.")
            return redirect("reference_review", pk=pk)
        if action == "save":
            form = ReferenceCompletionForm(request.POST)
            if form.is_valid():
                try:
                    complete_references(project, paper, form.cleaned_data["reference_text"])
                except screening_service.InvalidTransition as exc:
                    form.add_error(None, str(exc))
                else:
                    messages.success(request, "References saved. This paper is now finally selected.")
                    return redirect("reference_review", pk=pk)
        elif action == "reject":
            try:
                complete_references(project, paper, reject=True)
            except screening_service.InvalidTransition as exc:
                messages.error(request, str(exc))
                return redirect("reference_review", pk=pk)
            else:
                messages.success(request, "Paper rejected at Abstract screening because references were unavailable.")
                return redirect("reference_review", pk=pk)
        else:
            import httpx

            from literature.services.reference_service import RefbackfillError, enrich_papers

            try:
                enrich_papers([paper])
            except (RefbackfillError, httpx.RequestError, ValueError, TypeError):
                messages.warning(request, "Reference lookup is unavailable. Paste the bibliography or reject this paper.")
            else:
                paper.refresh_from_db()
                if screening_service.has_reference_list(paper):
                    messages.success(request, "References retrieved; paper finally selected.")
                else:
                    messages.warning(request, "No references were retrieved. Paste the bibliography or reject this paper.")
            return redirect("reference_review", pk=pk)
    if paper is None:
        row = pending.select_related("paper").order_by("paper_id").first()
        paper = row.paper if row else None
    return render(request, "literature/reference_review.html", {
        "project": project, "paper": paper, "pending_count": pending.count(),
        "form": form if form.is_bound else ReferenceCompletionForm(initial={"paper_id": paper.pk if paper else None}),
    })


# ------------------------------------------------------------------------ #
# Duplicates
# ------------------------------------------------------------------------ #

# --- Duplicate list ---
def duplicates_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Show same-title groups with authors for confirmation."""
    project = get_object_or_404(Project, pk=pk)
    groups = duplicate_groups(project)
    return render(
        request,
        "literature/duplicates.html",
        {"project": project, "groups": groups},
    )


# --- Duplicate merge ---
@require_POST
def duplicates_merge(request: HttpRequest, pk: int) -> HttpResponse:
    """Merge drop paper into keep paper, preserving audit trail."""
    project = get_object_or_404(Project, pk=pk)
    keep = get_object_or_404(
        Paper.objects.filter(discoveries__search__project=project).distinct(),
        pk=request.POST.get("keep_id"),
    )
    drop = get_object_or_404(
        Paper.objects.filter(discoveries__search__project=project).distinct(),
        pk=request.POST.get("drop_id"),
    )
    if keep.pk == drop.pk:
        messages.error(request, "Pick two different papers to merge.")
        return redirect("duplicates_view", pk=project.pk)
    merge_papers(keep=keep, drop=drop)
    messages.success(request, f"Merged paper {drop.pk} into {keep.pk}. Provenance kept.")
    return redirect("duplicates_view", pk=project.pk)


# --- Duplicate remove ---
@require_POST
def duplicates_remove(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete a duplicate paper (its audit rows go with it)."""
    project = get_object_or_404(Project, pk=pk)
    paper = get_object_or_404(
        Paper.objects.filter(discoveries__search__project=project).distinct(),
        pk=request.POST.get("paper_id"),
    )
    paper.delete()
    messages.success(request, f"Removed paper {request.POST.get('paper_id')}.")
    return redirect("duplicates_view", pk=project.pk)


# ------------------------------------------------------------------------ #
# Plots dashboard
# ------------------------------------------------------------------------ #

# --- Shared dashboard context ---
def _plots_context(project, *, depth: int = 2, prune: bool = False, seed_stage: str = "final", min_chain: int = 1) -> dict:
    """Build every dashboard figure once for embedding anywhere."""
    from literature.plotly_assets import THIRD_PARTY_NOTICE, plotly_version
    from literature.services.citation_service import build_graph
    from literature.services.plots_service import build_citation_figure
    from literature.services.survey_graph_service import build_survey_graph

    metrics = build_metrics(project)
    keyword_counts = keyword_stage_counts(project)
    has_data = Discovery.objects.filter(search__project=project).exists()
    stage_data = stage_year_cumulative(project)
    graph = build_graph(project, depth=depth, prune=prune, seed_stage=seed_stage, min_chain=min_chain)
    survey_graph = build_survey_graph(project)
    pruned_note = graph.get("pruned", 0)
    return {
        "metrics": metrics,
        "keyword_plot": build_keyword_figure(keyword_counts),
        "current_source_plot": build_current_source_bars(metrics.get("current", {})),
        "funnel_plot": build_funnel_figure(metrics),
        "yearly_plot": build_yearly_figure(stage_data),
        "stage_plot": build_stage_cumulative_figure(stage_data),
        "citation_plot": build_citation_figure(graph),
        "survey_plot": build_citation_figure(survey_graph),
        "survey_entries": project.survey_entries.all(),
        "survey_automatic_count": len(survey_graph["automatic_edges"]),
        "citation_nodes": len(graph.get("nodes", [])),
        "citation_edges": len(graph.get("edges", [])),
        "citation_pruned": pruned_note,
        "citation_pruned_edges": graph.get("pruned_edges", 0),
        "citation_missing_references": graph.get("missing_references", 0),
        "prune": prune,
        "min_chain": min_chain,
        "has_data": has_data,
        "depth": depth,
        "seed_stage": seed_stage,
        "plotly_version": plotly_version(),
        "plotly_notice": THIRD_PARTY_NOTICE,
    }


# --- Plots view ---
def _citation_options(request: HttpRequest) -> tuple[int, bool, str, int]:
    """Parse the graph controls shared by the page and its HTML download."""
    try:
        depth = max(0, min(int(request.GET.get("depth", 2)), 10))
    except ValueError:
        depth = 2
    prune = request.GET.get("prune", "") == "on"
    seed_stage = request.GET.get("stage", "final")
    if seed_stage not in {"final", "title", "found"}:
        seed_stage = "final"
    try:
        min_chain = max(1, int(request.GET.get("chain", 1)))
    except ValueError:
        min_chain = 1
    return depth, prune, seed_stage, min_chain


def plots_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Visual dashboard: every plot explains itself in plain English."""
    project = get_object_or_404(Project, pk=pk)
    if request.GET.get("lens") == "history":
        from literature.plotly_assets import plotly_version

        metrics = build_metrics(project)
        return render(request, "literature/plots_history.html", {
            "project": project,
            "metrics": metrics,
            "plotly_version": plotly_version(),
            "source_raw_plot": build_source_pie(metrics["per_source"]),
            "source_duplicates_plot": build_source_pie(metrics["per_source"], metric="dups"),
        })
    depth, prune, seed_stage, min_chain = _citation_options(request)
    context = {"project": project, **_plots_context(project, depth=depth, prune=prune, seed_stage=seed_stage, min_chain=min_chain)}
    return render(request, "literature/plots.html", context)


# --- Download all charts ---
def plots_download(request: HttpRequest, pk: int) -> HttpResponse:
    """Download current charts, or explicitly selected audit charts, offline.

    The installed Plotly.js bundle is inlined once; no CDN or app-server
    resource is required to pan/zoom/hover/download the charts. Node
    popups and paper-list links need the running app and are labelled so.
    No session, CSRF or credential values are embedded.
    """
    from literature.plotly_assets import (
        THIRD_PARTY_NOTICE,
        plotly_version,
        standalone_bundle_tag,
    )
    from literature.services.citation_service import build_graph
    from literature.services.plots_service import build_citation_figure

    project = get_object_or_404(Project, pk=pk)
    metrics = build_metrics(project)
    history = request.GET.get("lens") == "history"
    lens = "Audit / History" if history else "Current"
    parts = [
        f"<h1>{lens} charts for {project.name}</h1>",
        (
            f"<p>Offline chart export (Plotly v{plotly_version()}, served inline; "
            "no internet needed). Hover, pan, zoom and per-chart downloads work "
            "offline. Node popups and paper-list links need the running app.</p>"
        ),
    ]
    if history:
        parts.extend([
            (
                "<p>Historical import records and duplicate hits, not distinct current papers. "
                "Unspecified at import does not mean a current source is blank. "
                "Later metadata corrections preserve these original audits.</p>"
            ),
            build_source_pie(metrics["per_source"]),
            build_source_pie(metrics["per_source"], metric="dups"),
        ])
    else:
        stage_data = stage_year_cumulative(project)
        depth, prune, seed_stage, min_chain = _citation_options(request)
        graph = build_graph(project, depth=depth, prune=prune,
                            seed_stage=seed_stage, min_chain=min_chain)
        parts.extend([
            (
                "<p>Source bars count distinct current papers per displayed source. "
                "Multi-source papers appear in each matching bar; bar totals can exceed "
                "the paper total. Unknown counts only current blank displays. "
                "Historical source pies require the separate Audit / History export.</p>"
            ),
            build_keyword_figure(keyword_stage_counts(project)),
            build_current_source_bars(metrics["current"]),
            build_funnel_figure(metrics),
            build_yearly_figure(stage_data),
            build_stage_cumulative_figure(stage_data),
            build_citation_figure(graph, include_app_script=False),
        ])
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{lens} charts - {project.name}</title></head><body>"
        f"<!-- {THIRD_PARTY_NOTICE} -->"
        + standalone_bundle_tag()
        + "".join(parts)
        + "</body></html>"
    )
    response = HttpResponse(html, content_type="text/html")
    suffix = "-audit" if history else ""
    response["Content-Disposition"] = f'attachment; filename="charts-{project.pk}{suffix}.html"'
    return response


# ------------------------------------------------------------------------ #
# Export
# ------------------------------------------------------------------------ #

# --- Export markdown view ---
def export_markdown_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Download generated Markdown database (DB is truth)."""
    project = get_object_or_404(Project, pk=pk)
    content = build_markdown(project)
    response = HttpResponse(content, content_type="text/markdown")
    response["Content-Disposition"] = f'attachment; filename="{project.name[:40]}-literature.md"'
    return response


# --- Download final or visible citation references ---
def references_download(request: HttpRequest, pk: int, group: str) -> HttpResponse:
    """Return separate lists of final selections and visible citation papers."""
    from django.http import Http404

    from literature.services.citation_service import build_graph

    if group not in {"final", "citations"}:
        raise Http404("Unknown reference group")
    project = get_object_or_404(Project, pk=pk)
    final_ids = set(screening_service.final_decisions(project).values_list("paper_id", flat=True))
    if group == "final":
        ids = final_ids
        heading = "Final selected papers"
    else:
        depth, prune, stage, chain = _citation_options(request)
        graph = build_graph(project, depth=depth, prune=prune,
                            seed_stage=stage, min_chain=chain)
        ids = {node["id"] for node in graph["nodes"] if not node["is_seed"]} - final_ids
        heading = "Citation papers shown in the graph"
    papers = Paper.objects.filter(pk__in=ids)
    response = HttpResponse(
        build_references_markdown(project, papers, heading=heading),
        content_type="text/markdown; charset=utf-8",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="references-{group}-{project.pk}.md"'
    )
    return response
