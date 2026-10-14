#!/usr/bin/env python3

'''
@File:   views.py

@Brief:  Project, search, screening and export views (no login, local-first).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import json
import os
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

# Custom library Imports
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods, require_POST
from dotenv import set_key

from literature.forms import (
    DoiSearchForm,
    LookupPaperForm,
    ManualPaperForm,
    MissingMetadataForm,
    PaperDetailsForm,
    ProjectForm,
    ProjectNameForm,
    ReferenceCompletionForm,
    SearchForm,
    SettingsForm,
    StatusCorrectionForm,
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
from literature.services.export_service import (
    build_markdown,
    build_references_markdown,
)
from literature.services.manual_paper_service import (
    add_manual_paper,
    finish_manual_addition,
    selected_keyword,
    update_paper_details,
)
from literature.services.metrics_service import (
    build_metrics,
    display_sources,
    keyword_stage_counts,
    stage_year_cumulative,
)
from literature.services.plots_service import (
    build_current_source_bars,
    build_funnel_figure,
    build_keyword_figure,
    build_stage_cumulative_figure,
    build_yearly_figure,
)
from literature.services.search_service import (
    configuration_preview,
    effective_providers,
    run_search,
    save_current_configuration,
    update_current_configuration,
)

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


# --- Stage bootstrap on explicit writes only ---
def _ensure_stages(request: HttpRequest, project) -> None:
    """Bootstrap stages only for an explicit mutation request.

    Args:
        request: Owning request; safe methods never repair research state.
        project: Project whose stages a write path needs.

    Kwargs:
        None

    Returns:
        None
    """
    if request.method == "POST":
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


# --- Owning-worker display name edit ---
@require_http_methods(["GET", "POST"])
def project_name(request: HttpRequest, pk: int) -> HttpResponse:
    """Save an explicit display name without renaming the project folder.

    Args:
        request: Local form GET or CSRF-protected POST.
        pk: Project identifier in the fixed owning database.

    Kwargs:
        None

    Returns:
        HttpResponse: Name form or dashboard redirect after a valid save.
    """
    project = get_object_or_404(Project, pk=pk)
    form = ProjectNameForm(
        request.POST if request.method == "POST" else None, instance=project)
    # The manager middleware never admits this research route.
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(
            request, "Project display name saved; its folder is unchanged.")
        return redirect("project_detail", pk=pk)
    return render(request, "literature/project_name.html", {
        "form": form, "project": project,
    })


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
    """Show project results, current search criteria and screening counts.

    Args:
        request: Read-only dashboard GET with chart display options.
        pk: Project identifier in the owning database.

    Kwargs:
        None

    Returns:
        HttpResponse: Dashboard with one shared metrics projection.
    """
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
    for name in ("Title", "Abstract"):
        counts.setdefault(name, {"pending": 0, "accept": 0, "reject": 0})
    current_search = project.searches.filter(is_current=True).first()
    if current_search:
        current_search.display_providers = effective_providers(
            current_search.sources_selected)
    metrics = build_metrics(project)
    dup_groups = duplicate_groups(project)
    plots = _plots_context(
        project, metrics=metrics, include_survey=True,
        options=_chart_options(request), native_controls=True)
    return render(
        request,
        "literature/project_detail.html",
        {
            "project": project,
            "stages": stages,
            "counts": counts,
            "current_search": current_search,
            "metrics": metrics,
            "dup_count": len(dup_groups),
            **plots,
        },
    )


# ------------------------------------------------------------------------ #
# Project paper management
# ------------------------------------------------------------------------ #

@require_http_methods(["GET", "HEAD"])
def papers_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Search and page through this project's stored papers and statuses.

    The normal list and bounded HUD share query, membership and status rules.

    Args:
        request: Read-only list GET; view=hud selects the fragment.
        pk: Project identifier bounding discoveries and decision statuses.

    Kwargs:
        None

    Returns:
        HttpResponse: Escaped, filtered 25-row page or HTML fragment.
            HUD reads no abstracts, bibliographies or provider payloads and
            does not rebuild charts or calculate project revision hashes.
    """
    from django.db.models import (
        BooleanField,
        Case,
        CharField,
        F,
        OuterRef,
        Subquery,
        Value,
        When,
    )
    from django.db.models.fields.json import KT
    from django.db.models.functions import (
        Coalesce,
        Lower,
        NullIf,
        Substr,
        Trim,
    )

    from literature.services.metrics_service import project_revision as project_revision_stamp
    project = get_object_or_404(Project, pk=pk)
    query = (request.GET.get("q") or "").strip()[:200]
    hud = request.GET.get("view") == "hud"
    # IN avoids duplicate rows and DISTINCT projections of large sort fields.
    base = Paper.objects.filter(pk__in=Discovery.objects.filter(
        search__project=project).values("paper_id"))
    papers = base
    if query:
        papers = papers.filter(Q(title__icontains=query)
                               | Q(doi__icontains=query))
    decisions = ScreeningDecision.objects.filter(
        stage__project=project, paper_id=OuterRef("pk"))
    # Same exact ASCII audience tag and Unicode outer whitespace as screening.
    tag = r"^\s*" + "".join(
        f"[{letter.lower()}{letter.upper()}]" if letter.isalpha() else letter
        for letter in "international audience") + r"\s*$"
    papers = papers.annotate(
        _title_decision=Subquery(decisions.filter(
            stage__name="Title").values("decision")[:1]),
        _abstract_decision=Subquery(
            decisions.filter(stage__name="Abstract").values("decision")[:1]
        ),
        _has_refs=Case(
            When(~Q(references=[]) | Q(reference_text__regex=r"\S"),
                 then=Value(True)), default=Value(False),
            output_field=BooleanField()),
        _title_ready=Case(
            When(title__regex=r"\S", then=Value(True)),
            default=Value(False), output_field=BooleanField()),
        _abstract_ready=Case(
            When(abstract__regex=tag, then=Value(False)),
            When(abstract__regex=r"\S", then=Value(True)),
            default=Value(False), output_field=BooleanField()),
    ).alias(
        _linked_keyword=Subquery(
            PaperKeywordAssignment.objects.filter(
                project=project,
                paper_id=OuterRef("pk"),
            ).values("keyword")[:1]
        ),
    )
    statuses = {
        "all": ("All papers", Q()),
        "title_pending": ("Title pending", Q(_title_decision="PENDING")),
        "title_rejected": ("Title rejected", Q(_title_decision="REJECT")),
        "abstract_pending": ("Abstract pending",
                             Q(_abstract_decision="PENDING")),
        "abstract_rejected": ("Abstract rejected",
                              Q(_abstract_decision="REJECT")),
        "references_pending": (
            "References pending", Q(_abstract_decision="ACCEPT",
                                    _has_refs=False)),
        "final": ("Final kept", Q(_abstract_decision="ACCEPT",
                                  _has_refs=True)),
        "unqueued": ("Not queued for Title", Q(_title_decision__isnull=True)),
    }
    status = request.GET.get("status", "all")
    filter_error = ""
    if status not in statuses:
        status = "all"
        filter_error = "Unknown paper status; showing all statuses."
    papers = papers.filter(statuses[status][1])
    if hud:
        # ponytail: 25 rows, 240-character previews; Manage has full details.
        papers = papers.only("id", "publication_year", "sources").annotate(
            _hud_title=Substr("title", 1, 241))
    else:
        papers = papers.annotate(_linked_keyword=F("_linked_keyword"))
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
                        When(_abstract_ready=False,
                             then=Value("Pending — abstract needed")),
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
    direction = request.GET.get(
        "direction", "desc" if sort == "year" else "asc")
    if direction not in {"asc", "desc"}:
        direction = "desc" if sort == "year" else "asc"
    order = (
        ordering[sort].asc(nulls_first=True)
        if direction == "asc"
        else ordering[sort].desc(nulls_last=True)
    )
    papers = papers.order_by(order, Lower("title"), "pk")
    page_query = request.GET.copy()
    page_query.pop("page", None)
    page_query["q"], page_query["sort"], page_query["direction"] = query, sort, direction
    if "status" in request.GET or status != "all":
        page_query["status"] = status
    else:
        page_query.pop("status", None)
    headers = []
    for fields in (
        (("title", "Paper"),) if hud
        else (("title", "Paper"), ("doi", "DOI")),
        (("year", "Year"),),
        (("source", "Source"),) if hud
        else (("source", "Source"), ("keywords", "Keywords")),
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
    page.object_list = list(page.object_list)
    project_total = (base.count() if hud and (query or status != "all")
                     else page.paginator.count)
    labels = {"ACCEPT": "Yes", "REJECT": "No", "PENDING": "Pending",
              "NOT_QUEUED": "Not queued"}
    rows = []
    for paper in page.object_list:
        title_status = paper._title_decision or "NOT_QUEUED"
        abstract_status = paper._abstract_decision or "NOT_QUEUED"
        references_pending = (
            abstract_status == "ACCEPT" and not paper._has_refs)
        if references_pending:
            abstract_label = "Passed content; references pending"
        elif abstract_status == "ACCEPT":
            abstract_label = "Final selected"
        elif abstract_status == "PENDING" and not paper._abstract_ready:
            abstract_label = "Pending — abstract needed"
        else:
            abstract_label = (labels[abstract_status]
                              if abstract_status != "NOT_QUEUED" else "—")
        preview = paper._hud_title if hud else paper.title
        sources = display_sources(paper.sources)
        source_preview = ", ".join(sources)
        rows.append(
            {
                "paper": paper,
                "complete": (paper._title_ready and paper._abstract_ready
                             and paper._has_refs),
                "title_ready": paper._title_ready,
                "references_pending": references_pending,
                "title_status": title_status,
                "abstract_status": abstract_status,
                "title_label": (labels[title_status] if paper._title_ready
                                else "Needs title"),
                "abstract_label": abstract_label,
                "title_preview": preview[:240] + (
                    "…" if len(preview) > 240 else ""),
                "source_preview": source_preview[:240] + (
                    "…" if len(source_preview) > 240 else ""),
                "linked_keyword": "" if hud else paper._linked_keyword or "",
                # Display only: manual is a legacy entry method, not a known discovery origin.
                "display_sources": sources,
            }
        )
    return render(
        request,
        "literature/paper_overview_fragment.html" if hud
        else "literature/papers.html",
        {
            "project": project,
            "rows": rows,
            "page": page,
            "query": query,
            "headers": headers,
            "sort": sort,
            "direction": direction,
            "page_query": page_query.urlencode(),
            "revision": "" if hud else project_revision_stamp(project),
            "hud": hud, "status": status, "filter_error": filter_error,
            "project_total": project_total,
            "status_choices": [
                {"value": key, "label": label, "selected": key == status}
                for key, (label, _) in statuses.items()],
        },
    )


@require_http_methods(["GET", "POST"])
def paper_add(request: HttpRequest, pk: int) -> HttpResponse:
    """Save entered metadata, provenance, audit and queue in one transaction.

    Args:
        request: Local entry GET or CSRF-protected manual/DOI-selection POST.
        pk: Project identifier in the fixed owning database.

    Kwargs:
        None

    Returns:
        HttpResponse: Entry form with feedback or saved-paper redirect.

    No provider is called by a local save. Failed stale/locked saves roll
    back every paper, discovery, audit and stage write before feedback.
    """
    from urllib.parse import quote

    from django.urls import reverse

    from literature.services.search_service import is_complete

    project = get_object_or_404(Project, pk=pk)
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
                with transaction.atomic():
                    search, keyword = selected_keyword(
                        project, manual_form.cleaned_data["search_keyword"])
                    paper, created, discovery = add_manual_paper(
                        project, search, keyword, manual_form.cleaned_data,
                    )
                    project.ensure_default_stages()
                    finish_manual_addition(
                        project, paper, discovery, created=created,
                        keyword=keyword)
            except ValueError as exc:
                manual_form.add_error("search_keyword", str(exc))
            except IntegrityError:
                manual_form.add_error(
                    None, "Another save changed this paper. "
                    "Nothing was saved; reload and retry.")
            except OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                manual_form.add_error(
                    None, "Another database save is in progress. "
                    "Nothing was saved; try again in a moment.")
            else:
                messages.success(request, "Paper added to the project keyword." if created else
                                 "Existing paper linked to this project keyword.")
                if not is_complete(paper):
                    messages.warning(
                        request, "Missing abstracts and references can be completed during screening.")
                return redirect("paper_manage", pk=project.pk, paper_id=paper.pk)
    return render(request, "literature/paper_add.html", {
        "project": project, "search_form": search_form, "manual_form": manual_form,
        "has_keywords": bool(manual_form.fields["search_keyword"].choices),
    })


@require_http_methods(["GET", "POST"])
def paper_lookup(request: HttpRequest, pk: int) -> HttpResponse:
    """Preview metadata and save explicit current assignments or saved links.

    Args:
        request: Cache-only GET, online fetch POST or signed local save POST.
        pk: Owning project ID in the fixed database.

    Kwargs:
        None

    Returns:
        HttpResponse: Entry panel with errors or successful atomic redirect.

    Fetching returns a preview only; saving never repeats provider work.
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
    from literature.services.reference_service import (
        RefbackfillError,
        lookup_doi,
    )
    from literature.services.search_service import is_complete

    project = get_object_or_404(Project, pk=pk)
    doi_form = DoiSearchForm(
        {"doi": request.GET.get("doi") or request.POST.get("doi") or ""})
    if not doi_form.is_valid():
        messages.error(request, "Enter a valid DOI to look up.")
        return redirect("paper_add", pk=project.pk)
    doi = doi_form.cleaned_data["doi"]
    # Canonical papers are global; assignments and screening are scoped.
    paper = Paper.objects.filter(doi__iexact=doi).first()
    needs_origin = paper is None or not paper.discoveries.filter(
        search__project=project).exclude(source="manual").exists()
    metadata = {}
    lookup_error = ""
    preview_token = request.POST.get("preview_token", "")
    refresh_url = (reverse("paper_lookup", args=[pk]) + "?"
                   + urlencode({"doi": doi}))
    fetch_requested = (request.method == "POST"
                       and request.POST.get("action") == "fetch")
    if (request.method == "POST"
            and request.POST.get("action") not in {"save", "fetch"}):
        return HttpResponse(status=400)
    if request.method == "POST" and not fetch_requested:
        form = LookupPaperForm(
            request.POST, project=project, needs_origin=needs_origin,
            paper=paper)
        valid = form.is_valid()
        try:
            # Save the displayed preview, not a second provider response.
            preview = signing.loads(
                preview_token, salt="paper-entry", max_age=1800)
            if preview["project"] != pk or preview["doi"] != doi:
                raise signing.BadSignature("Preview identity mismatch")
            if getattr(request, "worker_route", False) and (
                preview.get("worker_uuid") != getattr(
                    request, "worker_uuid", "")
                or preview.get("worker_epoch") != getattr(
                    request, "worker_epoch", "")
            ):
                raise signing.BadSignature("Preview worker mismatch")
            metadata = preview["metadata"]
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            form.add_error(
                None, "This preview has expired or is invalid. "
                "Reload the local preview; fetch uncached DOI metadata "
                "explicitly before saving.")
            valid = False
        if valid:
            try:
                _, created = save_entry(
                    project, doi, metadata, form.cleaned_data,
                    preview["paper_id"], preview.get("version"))
            except ValueError as exc:
                form.add_error(None, str(exc))
            except IntegrityError:
                form.add_error(
                    None, "Another save changed this entry. "
                    "Look up the DOI again before saving.")
            except OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                form.add_error(
                    None, "Another database save is in progress. "
                    "Try saving again in a moment.")
            else:
                messages.success(
                    request, "Paper added to the database."
                    if created else "Entry updated.")
                return redirect(refresh_url)
    else:
        if fetch_requested and paper is None:
            try:
                metadata = build_metadata(lookup_doi(doi), doi)
            except (RefbackfillError, httpx.RequestError, ValueError, TypeError):
                lookup_error = (
                    "Online DOI fetch failed; nothing was saved. "
                    "Retry or add a paper by hand.")
        preview_token = signing.dumps({
            "project": pk, "doi": doi, "paper_id": paper.pk if paper else None,
            "version": entry_version(project, paper) if paper else None,
            "metadata": metadata,
            "worker_uuid": getattr(request, "worker_uuid", ""),
            "worker_epoch": getattr(request, "worker_epoch", ""),
        }, salt="paper-entry", compress=True)
        form = LookupPaperForm(
            initial=entry_initial(project, paper), project=project,
            needs_origin=needs_origin, paper=paper)

    if paper:
        # Display stored metadata without requesting providers or saving it.
        metadata = build_metadata({
            "title": paper.title, "authors": paper.authors,
            "abstract": paper.abstract,
            "publication_year": paper.publication_year, "venue": paper.venue,
            "references": paper.references,
            "reference_text": paper.reference_text,
        }, doi)
    has_current_search = project.searches.filter(is_current=True).exists()
    return render(request, "literature/paper_lookup.html", {
        "project": project, "doi": doi, "metadata": metadata, "paper": paper,
        "form": form, "preview_token": preview_token,
        "refresh_url": refresh_url,
        "lookup_error": lookup_error,
        "references": [{"identifier": ref, "url": reference_url(ref)}
                       for ref in metadata.get("references", [])],
        "complete": (is_complete(paper if paper else Paper(**metadata))
                     if metadata else False),
        "has_current_search": has_current_search,
        "has_searches": (has_current_search
                         or bool(entry_initial(project, paper).get(
                             "search_keyword"))),
        "can_save_preview": bool(paper or metadata.get("title")),
        "other_project": paper is not None and not paper.discoveries.filter(
            search__project=project).exists(),
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
                messages.success(
                    request, "Paper details saved. Complete papers enter Title screening.")
                return redirect("paper_manage", pk=project.pk, paper_id=paper.pk)
    elif request.method == "POST" and request.POST.get("action") == "status":
        correction_form = StatusCorrectionForm(request.POST)
        if correction_form.is_valid():
            stage = project.stages.get(
                name=correction_form.cleaned_data["stage"])
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
                messages.success(
                    request, "Screening status corrected and recorded in the audit history.")
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

def _search_lock_feedback(view: Callable) -> Callable:
    """Return retryable SQLite lock feedback without retrying database work.

    Args:
        view: Owning search view, including its initial configuration reads.

    Kwargs:
        None

    Returns:
        Callable: Wrapped view preserving unrelated errors and CSRF rules.
    """
    @wraps(view)
    def guarded(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        """Render a GET-capable retry link without further DB reads on locks.

        Args:
            request: Search request.
            *args: Positional URL parameters including project identity.

        Kwargs:
            **kwargs: Named URL parameters, notably pk and search_id.

        Returns:
            HttpResponse: Normal response or retryable HTTP 503.
        """
        from django.urls import reverse
        from django.utils.html import format_html

        try:
            return view(request, *args, **kwargs)
        except OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            project_id = kwargs.get("pk", args[0] if args else None)
            retry_url = reverse("search_create", args=[project_id])
            return HttpResponse(format_html(
                '<h1>Search settings not saved</h1><p>Another database '
                'operation is in progress. Reload and retry shortly.</p>'
                '<a href="{}">Reload search settings</a>', retry_url,
            ), status=503)
    return guarded


# --- Search create ---
@require_http_methods(["GET", "POST"])
@_search_lock_feedback
def search_create(request: HttpRequest, pk: int) -> HttpResponse:
    """Configure or explicitly adopt/combine future settings, keeping history.

    Args:
        request: Read-only proposal GET or signed confirmation POST.
        pk: Owning project ID.

    Kwargs:
        None

    Returns:
        HttpResponse: Proposal with errors or successful settings redirect.
    """
    project = get_object_or_404(Project, pk=pk)
    current = project.searches.filter(is_current=True).first()
    if current:
        if request.method == "POST":
            messages.warning(
                request, "This project already has current settings. "
                "Edit them before saving.")
            return redirect("search_update", pk=pk, search_id=current.pk)
        return search_update(request, pk, current.pk)
    binding = [getattr(request, "worker_uuid", ""),
               getattr(request, "worker_epoch", "")]
    preview = configuration_preview(project, binding)
    form = SearchForm(
        request.POST if request.method == "POST" else None,
        initial=preview["proposal"])
    if request.method == "POST":
        adopt = request.POST.get("action") == "adopt"
        if adopt or form.is_valid():
            try:
                save_current_configuration(
                    project, {} if adopt else form.cleaned_data,
                    request.POST.get("configuration_token", ""), binding,
                    adopt=adopt, translated=request.POST.get("translated")
                    == "on")
            except ValueError as exc:
                if adopt:
                    form.is_valid()
                form.add_error(None, str(exc))
            except IntegrityError:
                # Re-read the committed winner outside the failed atomic block.
                winner = project.searches.filter(is_current=True).first()
                if winner:
                    messages.warning(
                        request, "Another tab saved current settings; "
                        "review them before editing.")
                    return redirect(
                        "search_update", pk=pk, search_id=winner.pk)
                form.add_error(
                    None, "Settings could not be saved; reload and retry.")
            else:
                messages.success(
                    request, "Search settings saved. "
                    "Find papers now to add run history.")
                return redirect("project_detail", pk=pk)
    return render(request, "literature/search_form.html", {
        "form": form, "project": project, "editing": False, **preview,
    })


# --- Search update ---
@require_http_methods(["GET", "POST"])
@_search_lock_feedback
def search_update(
    request: HttpRequest, pk: int, search_id: int,
) -> HttpResponse:
    """Edit signed current settings or display historical settings read-only.

    Args:
        request: Editor GET or signed settings POST.
        pk: Owning project ID.
        search_id: Same-project configuration identity from the URL.

    Kwargs:
        None

    Returns:
        HttpResponse: Editor, read-only historical page, refusal or redirect.
    """
    project = get_object_or_404(Project, pk=pk)
    search = get_object_or_404(Search, pk=search_id, project=project)
    if not search.is_current:
        if request.method == "POST":
            return HttpResponse(
                "Historical search settings are read-only.", status=403)
        return render(request, "literature/search_history_settings.html", {
            "project": project, "search": search,
        })
    binding = [getattr(request, "worker_uuid", ""),
               getattr(request, "worker_epoch", "")]
    if request.method == "POST":
        configuration_token = request.POST.get("configuration_token", "")
        form = SearchForm(request.POST, instance=search)
        if form.is_valid():
            try:
                updated = update_current_configuration(
                    project, search.pk, form.cleaned_data,
                    configuration_token, binding)
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request, f"Updated '{updated.name}'. "
                    "Find papers now to add a run.")
                return redirect("project_detail", pk=project.pk)
    else:
        # Sign and render the same snapshot, even if another writer just saved.
        preview = configuration_preview(project, binding)
        captured = next((source for source in preview["source_configurations"]
                         if source["pk"] == search.pk), None)
        if captured is None or not captured["is_current"]:
            return redirect("search_create", pk=pk)
        for field, value in captured.items():
            if field != "pk":
                setattr(search, field, value)
        configuration_token = preview["configuration_token"]
        form = SearchForm(
            instance=search,
            initial={
                "keywords_json": json.dumps(search.keywords or []),
                "venues_json": json.dumps(search.venues or []),
                "author_names_json": json.dumps(search.author_names or []),
                "sources_selected": effective_providers(
                    search.sources_selected),
            },
        )
    return render(
        request,
        "literature/search_form.html",
        {"form": form, "project": project, "editing": True, "search": search,
         "configuration_token": configuration_token},
    )


# --- Search run ---
@require_POST
@_search_lock_feedback
def search_run(request: HttpRequest, pk: int, search_id: int) -> HttpResponse:
    """Run current settings only, preserving isolated per-provider audits.

    Args:
        request: CSRF-protected online action POST.
        pk: Owning project ID.
        search_id: Same-project current configuration identity.

    Kwargs:
        None

    Returns:
        HttpResponse: Dashboard or provider-settings redirect with feedback.
    """
    project = get_object_or_404(Project, pk=pk)
    search = get_object_or_404(Search, pk=search_id, project=project)
    if not search.is_current:
        messages.error(
            request, "Historical search settings cannot run. "
            "Configure the current search first.")
        return redirect("project_detail", pk=pk)
    if (set(effective_providers(search.sources_selected)) != {"scopus"}
            and not _email_configured()):
        messages.warning(
            request, "Add your email in Settings first "
            "(OpenAlex polite pool).")
        if getattr(request, "manager_base", ""):
            return redirect(f"{request.manager_base}/settings/")
        return redirect("settings_view")
    try:
        runs = run_search(search.pk)
    except OperationalError:
        raise  # The outer guard reports contention without database retries.
    except (ValueError, Search.DoesNotExist) as exc:
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
    notify = (messages.warning if any(
        run.status != "completed" for run in runs)
        else messages.success)
    notify(request, "Search completed — " + "; ".join(parts) + ".")
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
    gate_papers(list(Paper.objects.filter(discoveries__search__project=project).distinct(
    ).only("id", "title")), project.stages.get(name="Title"))
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
        messages.warning(
            request, "OpenCitations rate limited this batch; try again later.")
    elif result["failed"]:
        messages.warning(
            request, f"{result['failed']} citation lookups failed; retry later.")
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
    """Save explicit provider settings to the selected credential file.

    Args:
        request: Local Settings GET or CSRF-protected credential POST.

    Kwargs:
        None

    Returns:
        HttpResponse: Settings form with blank Elsevier secret fields,
            configured-value placeholders, validation/save errors or a
            successful credential-save redirect. Saved secrets are not echoed.

    Side Effects:
        Write local credential values only after a valid POST, never to the
        database. Blank optional fields preserve saved values; restarting
        the server/launcher applies changes to process settings.
    """
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
                if form.cleaned_data.get("openalex_email"):
                    set_key(str(env_file), "OPENALEX_EMAIL",
                            form.cleaned_data["openalex_email"])
                if form.cleaned_data.get("semantic_scholar_key"):
                    set_key(
                        str(env_file),
                        "SEMANTIC_SCHOLAR_KEY",
                        form.cleaned_data["semantic_scholar_key"],
                    )
                if form.cleaned_data.get("opencitations_token"):
                    set_key(str(env_file), "OPENCITATIONS_TOKEN",
                            form.cleaned_data["opencitations_token"])
                if form.cleaned_data.get("elsevier_api_key"):
                    set_key(str(env_file), "ELSEVIER_API_KEY",
                            form.cleaned_data["elsevier_api_key"])
                if form.cleaned_data.get("elsevier_insttoken"):
                    set_key(str(env_file), "ELSEVIER_INSTTOKEN",
                            form.cleaned_data["elsevier_insttoken"])
            except OSError as exc:
                form.add_error(None, f"Cannot save the credential file: {exc}")
            else:
                message = (
                    "Saved to private credentials (restart launcher to apply)."
                    if os.getenv("WP_LAUNCHER") == "1" else
                    "Saved locally to .env (restart server to apply).")
                if form.cleaned_data.get("elsevier_api_key"):
                    message += (
                        " Elsevier Scopus API key saved. Its field stays blank "
                        "on reload for security; this does not delete the key.")
                messages.success(request, message)
                return redirect("project_list")
    else:
        form = SettingsForm(initial=initial)
    for field, setting_name in (
        ("elsevier_api_key", "ELSEVIER_API_KEY"),
        ("elsevier_insttoken", "ELSEVIER_INSTTOKEN"),
    ):
        if getattr(settings, setting_name, ""):
            form.fields[field].widget.attrs["placeholder"] = (
                "Configured (hidden)")
    return render(
        request, "literature/settings.html", {
            "form": form,
            "env_exists": env_file.exists(),
            "opencitations_token_configured": bool(
                getattr(settings, "OPENCITATIONS_TOKEN", "")),
            "elsevier_api_key_configured": bool(
                getattr(settings, "ELSEVIER_API_KEY", "")),
            "elsevier_insttoken_configured": bool(
                getattr(settings, "ELSEVIER_INSTTOKEN", "")),
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
    done = ScreeningDecision.objects.filter(
        stage=stage).exclude(decision="PENDING").count()
    pending = total - done
    return {"done": done, "total": total, "pending": pending}


# --- Screening view ---
@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def screening_view(request: HttpRequest, pk: int, stage_name: str) -> HttpResponse:
    """Show queued content without repairing stages on GET.

    Args:
        request: Screening GET or unknown-field completion POST.
        pk: Owning project identifier.
        stage_name: Title or Abstract route name, case-insensitive.

    Kwargs:
        None

    Returns:
        HttpResponse: Next queued paper or truthful empty-round page.
    """
    project = get_object_or_404(Project, pk=pk)
    _ensure_stages(request, project)
    normalized = stage_name.capitalize()
    if (not getattr(request, "worker_route", False)
            and normalized in {"Title", "Abstract"}
            and not project.stages.filter(name=normalized).exists()):
        return redirect(
            "screening_complete", pk=pk, stage_name=normalized.lower())
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
        paper = get_object_or_404(Paper.objects.filter(
            decisions__stage=stage, decisions__decision="PENDING"), pk=paper_id)
        metadata_form = MissingMetadataForm(request.POST, paper=paper)
        if metadata_form.is_valid():
            from django.db import IntegrityError

            from literature.services.reference_review_service import save_missing_metadata

            try:
                save_missing_metadata(
                    project, paper, metadata_form.cleaned_data)
            except screening_service.InvalidTransition as exc:
                metadata_form.add_error(None, str(exc))
            except IntegrityError:
                metadata_form.add_error(
                    None, "Another entry now uses this identifier. Reload and review duplicates; no details were saved.")
            else:
                messages.success(
                    request, "Missing details saved. Continue screening this paper.")
                from django.urls import reverse

                return redirect(reverse("screening_view", args=[pk, "abstract"]) + f"?paper={paper.pk}")
    elif request.GET.get("paper"):
        if not request.GET["paper"].isdigit():
            return HttpResponse(status=400)
        paper = get_object_or_404(Paper.objects.filter(
            decisions__stage=stage, decisions__decision="PENDING"), pk=request.GET["paper"])
    paper = paper or _next_pending(stage)
    if paper is None:
        return redirect("screening_complete", pk=project.pk, stage_name=stage.name.lower())
    progress = _progress(stage)
    current_index = progress["done"] + 1
    metadata_form = metadata_form or MissingMetadataForm(
        paper=paper, initial={"paper_id": paper.pk})
    extra_metadata = [
        field for field in metadata_form if field.name not in {"paper_id", "abstract"}]
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
    """Distinguish an empty, unqueued or finished round without GET writes.

    Args:
        request: Local empty-round GET.
        pk: Owning project identifier.
        stage_name: Title or Abstract route name, case-insensitive.

    Kwargs:
        None

    Returns:
        HttpResponse: Counts and a state-specific next action.
    """
    project = get_object_or_404(Project, pk=pk)
    normalized = stage_name.capitalize()
    if normalized not in {"Title", "Abstract"}:
        return HttpResponse(status=404)
    stage = project.stages.filter(name=normalized).first()
    if stage is None and getattr(request, "worker_route", False):
        return HttpResponse(status=404)
    progress = (_progress(stage) if stage else
                {"done": 0, "total": 0, "pending": 0})
    has_searches = project.searches.filter(is_current=True).exists()
    has_runs = project.searches.filter(runs__isnull=False).exists()
    has_papers = Discovery.objects.filter(search__project=project).exists()
    return render(
        request,
        "literature/screening_complete.html",
        {
            "project": project,
            "stage": stage or {"name": normalized},
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
        accepted = ScreeningDecision.objects.filter(
            stage__project=project, stage__name="Abstract", decision="ACCEPT", paper__discoveries__search__project=project).distinct()
        decision = get_object_or_404(accepted.select_related(
            "paper"), paper_id=request.POST["paper_id"])
        paper = decision.paper
        if screening_service.has_reference_list(paper):
            messages.info(
                request, "References are already available. No metadata or decision was overwritten.")
            return redirect("reference_review", pk=pk)
        if action == "save":
            form = ReferenceCompletionForm(request.POST)
            if form.is_valid():
                try:
                    complete_references(
                        project, paper, form.cleaned_data["reference_text"])
                except screening_service.InvalidTransition as exc:
                    form.add_error(None, str(exc))
                else:
                    messages.success(
                        request, "References saved. This paper is now finally selected.")
                    return redirect("reference_review", pk=pk)
        elif action == "reject":
            try:
                complete_references(project, paper, reject=True)
            except screening_service.InvalidTransition as exc:
                messages.error(request, str(exc))
                return redirect("reference_review", pk=pk)
            else:
                messages.success(
                    request, "Paper rejected at Abstract screening because references were unavailable.")
                return redirect("reference_review", pk=pk)
        else:
            import httpx

            from literature.services.reference_service import RefbackfillError, enrich_papers

            try:
                enrich_papers([paper])
            except (RefbackfillError, httpx.RequestError, ValueError, TypeError):
                messages.warning(
                    request, "Reference lookup is unavailable. Paste the bibliography or reject this paper.")
            else:
                paper.refresh_from_db()
                if screening_service.has_reference_list(paper):
                    messages.success(
                        request, "References retrieved; paper finally selected.")
                else:
                    messages.warning(
                        request, "No references were retrieved. Paste the bibliography or reject this paper.")
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
    messages.success(
        request, f"Merged paper {drop.pk} into {keep.pk}. Provenance kept.")
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
def _plots_context(
    project, *, depth: int = 2, prune: bool = False,
    seed_stage: str = "final", min_chain: int = 1,
    metrics: dict | None = None, include_survey: bool = True,
    options: dict | None = None, include_app_script: bool = True,
    native_controls: bool = False,
) -> dict:
    """Build figures with optional reuse of the dashboard metrics.

    Args:
        project: Project whose papers and curated entries own the figures.

    Kwargs:
        depth: Citation hop depth, defaulting to two.
        prune: Display the citation backbone, defaulting to False.
        seed_stage: Citation seed lens, defaulting to final selection.
        min_chain: Minimum citation group size, defaulting to one.
        metrics: Existing metrics projection, defaulting to a fresh build.
        include_survey: Render the curated graph, defaulting to True.
        options: Normalized display options. Defaults to None, using the
            existing citation keyword arguments and other chart defaults.
        include_app_script: Include local popup hooks. Defaults to True.
        native_controls: Replace small modebars with native page controls.
            Defaults to False; standalone downloads retain modebars.

    Returns:
        dict: Figure markup, counts and locally served Plotly metadata.
    """
    from literature.plotly_assets import THIRD_PARTY_NOTICE, plotly_version
    from literature.services.citation_service import build_graph
    from literature.services.plots_service import build_citation_figure
    from literature.services.survey_graph_service import build_survey_graph

    if options is None:
        options = _chart_options()
        options.update(depth=depth, prune="on" if prune else "",
                       stage=seed_stage, chain=min_chain)
    depth, prune = options["depth"], options["prune"] == "on"
    seed_stage, min_chain = options["stage"], options["chain"]
    plot_config = {"displayModeBar": False} if native_controls else None
    if metrics is None:
        metrics = build_metrics(project)
    keyword_counts = keyword_stage_counts(project)
    has_data = Discovery.objects.filter(search__project=project).exists()
    stage_data = _year_window(stage_year_cumulative(project), options)
    graph = build_graph(
        project, depth=depth, prune=prune,
        seed_stage=seed_stage, min_chain=min_chain)
    survey_graph = build_survey_graph(project) if include_survey else None
    pruned_note = graph.get("pruned", 0)
    context = {
        "metrics": metrics,
        "keyword_plot": build_keyword_figure(
            keyword_counts, order=options["keyword_order"],
            step=options["keyword_step"], plot_config=plot_config),
        "current_source_plot": build_current_source_bars(
            metrics.get("current", {}), order=options["source_order"],
            plot_config=plot_config),
        "funnel_plot": build_funnel_figure(
            metrics, counts=options["flow_counts"] == "on",
            plot_config=plot_config),
        "yearly_plot": build_yearly_figure(
            stage_data, step=options["yearly_step"], plot_config=plot_config),
        "stage_plot": build_stage_cumulative_figure(
            stage_data, step=options["totals_step"],
            inline_controls=include_app_script, plot_config=plot_config),
        "citation_plot": build_citation_figure(
            graph, include_app_script=include_app_script,
            plot_config=plot_config),
        "survey_plot": (build_citation_figure(
            survey_graph, plot_config=plot_config)
            if include_survey else ""),
        "survey_entries": (project.survey_entries.all()
                           if include_survey else ()),
        "survey_automatic_count": (len(survey_graph["automatic_edges"])
                                   if include_survey else 0),
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
    encoded = {key: value for key, value in options.items()
               if key != "feedback" and value not in (None, "")}
    steps = (("all", "All steps"), ("found", "Found"),
             ("title", "Title passed"), ("final", "Final kept"))
    # Two actual page callers share the same card and option contract.
    definitions = (
        ("keyword", "Hits per keyword", "keyword_plot",
         ("keyword_order", "keyword_step"),
         ("Counts retain historical discoveries; final excludes references "
          "pending.")),
        ("source", "Current papers by source", "current_source_plot",
         ("source_order",),
         ("Distinct current papers per displayed source. Multi-source papers "
          "appear in each matching bar; unnamed origins are not a bar.")),
        ("flow", "How papers flow through each check", "funnel_plot",
         ("flow_counts",), ("Band width represents counts; hiding labels "
                            "preserves every category and band.")),
        ("yearly", "Papers per year", "yearly_plot",
         ("yearly_step", "year_from", "year_to"),
         "Points mean IN that year. Undated papers remain in the project."),
        ("totals", "Running totals by year at each step", "stage_plot",
         ("totals_step", "year_from", "year_to"),
         ("Points mean THROUGH that year, including earlier years outside "
          "the window. Final excludes papers awaiting references.")),
        ("citation", "How your kept papers link together", "citation_plot",
         ("stage", "depth", "chain", "prune"),
         ("Follow citing → cited links backward from the chosen seeds. "
          "Years orient softly, not fixed rows; displayed edges and cycles "
          "are retained.")),
    )
    labels = {
        "keyword_order": "Keyword order", "keyword_step": "Keyword step",
        "source_order": "Source order", "flow_counts": "Count labels",
        "yearly_step": "Yearly step", "totals_step": "Running-total step",
        "year_from": "From year", "year_to": "To year",
        "stage": "Citation seed lens", "depth": "Steps back through citations",
        "chain": "Minimum group size", "prune": "Show shortest-path backbone",
    }
    menus = {
        "keyword_order": (("az", "A–Z"), ("found_desc", "Most found first")),
        "keyword_step": steps, "yearly_step": steps, "totals_step": steps,
        "source_order": (("canonical", "Provider order"), ("name", "Name"),
                         ("count_desc", "Largest count first")),
        "flow_counts": (("on", "Show counts"), ("off", "Hide counts")),
        "stage": (("final", "Final kept"), ("title", "Title passed (draft)"),
                  ("found", "Everything found (draft)")),
    }
    cards = []
    for kind, title, figure, fields, description in definitions:
        controls = []
        for key in fields:
            controls.append({
                "name": key, "label": labels[key], "value": options[key],
                "menu": [{"value": value, "label": label,
                          "selected": options[key] == value}
                         for value, label in menus.get(key, ())],
                "minimum": 0 if key == "depth" else 1,
                "maximum": (10 if key == "depth" else
                            9999 if key in {"year_from", "year_to"} else ""),
            })
        cards.append({
            "kind": kind, "title": title, "html": context[figure],
            "description": description, "controls": controls,
            "preserved": {key: value for key, value in encoded.items()
                          if key not in fields},
        })
    context.update(chart_cards=cards, chart_options=options,
                   chart_query=urlencode(encoded),
                   chart_window_empty=not stage_data["years"],
                   native_chart_controls=native_controls)
    return context


# --- Plots view ---
def _chart_options(request: HttpRequest | None = None) -> dict:
    """Validate all chart display options at their owning GET boundary.

    Args:
        None

    Kwargs:
        request: Local page/download GET. Defaults to None for defaults.

    Returns:
        dict: Whitelisted settings and visible feedback for invalid values.
    """
    raw = request.GET if request is not None else {}
    feedback = []
    choices = {
        "keyword_order": ("az", "found_desc"),
        "keyword_step": ("all", "found", "title", "final"),
        "source_order": ("canonical", "name", "count_desc"),
        "flow_counts": ("on", "off"),
        "yearly_step": ("all", "found", "title", "final"),
        "totals_step": ("all", "found", "title", "final"),
        "stage": ("final", "title", "found"), "prune": ("", "on"),
    }
    options = {}
    for key, allowed in choices.items():
        value = raw.get(key, allowed[0])
        if value not in allowed:
            feedback.append(f"Invalid {key}; using {allowed[0] or 'off'}.")
            value = allowed[0]
        options[key] = value
    for key, default, lower, upper in (
        ("depth", 2, 0, 10), ("chain", 1, 1, None),
        ("year_from", None, 1, 9999), ("year_to", None, 1, 9999),
    ):
        value = raw.get(key, default)
        try:
            value = (None if value in (None, "") and default is None
                     else int(value))
            if value is not None and (
                value < lower or (upper is not None and value > upper)
            ):
                raise ValueError
        except (ValueError, TypeError):
            fallback = default if default is not None else "all years"
            feedback.append(f"Invalid {key}; using {fallback}.")
            value = default
        options[key] = value
    if (options["year_from"] is not None and options["year_to"] is not None
            and options["year_from"] > options["year_to"]):
        feedback.append("From year is later than To year; showing all years.")
        options["year_from"] = options["year_to"] = None
    options["feedback"] = feedback
    return options


def _citation_options(request: HttpRequest) -> tuple[int, bool, str, int]:
    """Parse the graph controls shared by the page and its HTML download.

    Args:
        request: Local chart or citation-reference GET.

    Kwargs:
        None

    Returns:
        tuple[int, bool, str, int]: Depth, prune, lens and minimum group size.
    """
    options = _chart_options(request)
    return (options["depth"], options["prune"] == "on", options["stage"],
            options["chain"])


def _year_window(data: dict, options: dict) -> dict:
    """Slice displayed years only after whole-corpus totals are computed.

    Args:
        data: Full IN/THROUGH arrays and undated counts.
        options: Validated optional lower and upper display years.

    Kwargs:
        None

    Returns:
        dict: Windowed arrays; THROUGH never restarts at the lower bound.
    """
    indices = [index for index, year in enumerate(data["years"])
               if (options["year_from"] is None
                   or year >= options["year_from"])
               and (options["year_to"] is None or year <= options["year_to"])]
    return {
        **data, "years": [data["years"][index] for index in indices],
        "windowed": (options["year_from"] is not None
                     or options["year_to"] is not None),
        **{section: {key: [values[index] for index in indices]
                     for key, values in data.get(section, {}).items()}
           for section in ("series", "yearly")},
    }


def plots_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Visual dashboard: every plot explains itself in plain English.

    Charts use shared display options without a separate history lens.

    Args:
        request: Read-only chart GET with whitelisted display options.
        pk: Project identifier in the owning database.

    Kwargs:
        None

    Returns:
        HttpResponse: Local chart page with option feedback.
    """
    project = get_object_or_404(Project, pk=pk)
    context = {"project": project, **_plots_context(
        project, options=_chart_options(request), native_controls=True)}
    return render(request, "literature/plots.html", context)


# --- Download all charts ---
def plots_download(request: HttpRequest, pk: int) -> HttpResponse:
    """Download the project's charts as standalone offline HTML.

    The installed Plotly.js bundle is inlined once; no CDN or app-server
    resource is required to pan/zoom/hover/download the charts. Node
    popups and paper-list links need the running app and are labelled so.
    No session, CSRF or credential values are embedded.

    Args:
        request: Read-only download GET with chart display options.
        pk: Project identifier in the owning database.

    Kwargs:
        None

    Returns:
        HttpResponse: Standalone HTML attachment with normalized figures.
    """
    from html import escape

    from literature.plotly_assets import (
        THIRD_PARTY_NOTICE,
        plotly_version,
        standalone_bundle_tag,
    )

    project = get_object_or_404(Project, pk=pk)
    metrics = build_metrics(project)
    parts = [
        f"<h1>Charts for {escape(project.name)}</h1>",
        (
            f"<p>Offline chart export (Plotly v{plotly_version()}, served inline; "
            "no internet needed). Hover, pan, zoom and per-chart downloads work "
            "offline. Node popups and paper-list links need the running app.</p>"
        ),
    ]
    context = _plots_context(
        project, metrics=metrics, include_survey=False,
        options=_chart_options(request), include_app_script=False)
    if context["chart_options"]["feedback"]:
        parts.append("<section aria-label='Chart settings feedback'>"
                     "<h2>Chart settings adjusted</h2><ul>" + "".join(
                         f"<li>{escape(problem)}</li>" for problem in
                         context["chart_options"]["feedback"])
                     + "</ul></section>")
    parts.append(
        "<p>Source bars count distinct current papers per displayed source. "
        "Multi-source papers appear in each matching bar; bar totals can exceed "
        "the paper total. Blank origins are not a source bar.</p>")
    parts.extend(context[key] for key in (
        "keyword_plot", "current_source_plot", "funnel_plot",
        "yearly_plot", "stage_plot", "citation_plot"))
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Charts - {escape(project.name)}</title></head><body>"
        f"<!-- {THIRD_PARTY_NOTICE} -->"
        + standalone_bundle_tag()
        + "".join(parts)
        + "</body></html>"
    )
    response = HttpResponse(html, content_type="text/html")
    response["Content-Disposition"] = f'attachment; filename="charts-{project.pk}.html"'
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
    """Return separate lists of final selections and visible citation papers.

    Citation membership uses the same normalized options as the figures.

    Args:
        request: Local read-only reference download GET.
        pk: Project identifier in the owning database.
        group: final or citations reference list.

    Kwargs:
        None

    Returns:
        HttpResponse: Markdown attachment with any option feedback.

    Raises:
        Http404: If the group or owning project does not exist.
    """
    from django.http import Http404

    from literature.services.citation_service import build_graph

    if group not in {"final", "citations"}:
        raise Http404("Unknown reference group")
    project = get_object_or_404(Project, pk=pk)
    final_ids = set(screening_service.final_decisions(
        project).values_list("paper_id", flat=True))
    options = _chart_options(request)
    if group == "final":
        ids = final_ids
        heading = "Final selected papers"
    else:
        graph = build_graph(
            project, depth=options["depth"], prune=options["prune"] == "on",
            seed_stage=options["stage"], min_chain=options["chain"])
        ids = {node["id"] for node in graph["nodes"]
               if not node["is_seed"]} - final_ids
        heading = "Citation papers shown in the graph"
    papers = Paper.objects.filter(pk__in=ids)
    feedback = "".join(f"> {problem}\n" for problem in options["feedback"])
    response = HttpResponse(
        feedback + build_references_markdown(project, papers, heading=heading),
        content_type="text/markdown; charset=utf-8",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="references-{group}-{project.pk}.md"'
    )
    return response
