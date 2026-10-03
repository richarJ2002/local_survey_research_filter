#!/usr/bin/env python3

'''
@File:   workspace_views.py

@Brief:  Manager New/Load workspace landing leading to real workers (WP01.04).

@Date:   03/10/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

from django import forms

# Custom library imports
from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from literature import workspace as ws
from literature.forms import WorkspaceNewForm, WorkspaceOpenForm, WorkspaceRootForm
from literature.models import Project
from literature.project_store import ProjectStoreError, create_project_store

# ------------------------------------------------------------------------ #
# Landing context
# ------------------------------------------------------------------------ #


# --- Shared landing context: remembered root, one-level discovery, preview ---
def _landing_context(preview_dir: str = "") -> dict:
    remembered = ws.get_workspace_root()
    discovery = ws.discover_workspace(remembered) if remembered is not None else {
        "root": "", "entries": [], "errors": [],
    }
    preview = ws.describe_target(preview_dir) if preview_dir else None
    email_ok = bool((getattr(settings, "OPENALEX_EMAIL", "") or "").strip())
    return {
        "remembered_root": str(remembered) if remembered is not None else "",
        "discovery": discovery,
        "preview": preview,
        "email_ok": email_ok,
        "root_form": WorkspaceRootForm(initial={"directory": str(remembered) if remembered else ""}),
        "new_form": WorkspaceNewForm(),
        "open_form": WorkspaceOpenForm(),
        "operation_form": StoreOperationForm(),
    }


# --- Manager landing: New/Load toggle, typed paths, recognised projects ---
@ensure_csrf_cookie
def workspace_home(request: HttpRequest) -> HttpResponse:
    """Serve the manager landing; never reads research rows, only manifests."""
    # ponytail: opportunistic bounded cleanup on the highest-traffic manager
    # surface; failures never break the landing, the launcher owns patience.
    try:
        ws.sweep_idle()
    except (OSError, ValueError, ProjectStoreError):
        pass
    context = _landing_context(request.GET.get("directory", ""))
    directory = request.GET.get("directory", "")
    operation = request.GET.get("operation", "")
    if directory and operation in {choice[0] for choice in StoreOperationForm.base_fields["action"].choices}:
        try:
            # A prefill is a read-only hint, not approval to write/copy. Never
            # inherit destination or confirmation from a navigation query.
            manifest = ws.check_load_target(directory)
            context["operation_form"] = StoreOperationForm(initial={
                "action": operation,
                "source": str(manifest.location),
            })
        except ProjectStoreError as exc:
            messages.error(request, f"Cannot preselect this project: {exc}")
    return render(request, "literature/workspace_home.html", context)


# ------------------------------------------------------------------------ #
# Mutations (remember root, create-at-location, load-existing)
# ------------------------------------------------------------------------ #


class StoreOperationForm(forms.Form):
    """Native path inputs; destination means backup package for Backup/Restore."""
    action = forms.ChoiceField(choices=(
        ("copy", "Save Copy (new project ID)"), ("backup", "Backup (same project ID)"),
        ("restore", "Restore (same project ID)"), ("close", "Close worker after exiting all tabs"),
    ))
    source = forms.CharField(label="Project folder (backup folder for Restore)", max_length=4096)
    destination = forms.CharField(label="Destination folder (existing project for Restore)", max_length=4096, required=False)
    confirm = forms.BooleanField(label="I confirm the resolved paths; Restore replaces the project and retains a recovery sibling")


@require_POST
def workspace_operation(request: HttpRequest) -> HttpResponse:
    """Manager-only CSRF-protected copy/recovery tools, with explicit closure."""
    from literature.worker import role

    if role() != "manager":
        return HttpResponse(status=404)
    form = StoreOperationForm(request.POST)
    if form.is_valid():
        action = form.cleaned_data["action"]
        source = form.cleaned_data["source"]
        destination = form.cleaned_data["destination"]
        try:
            if action == "close":
                result = ws.close_store(source)
                if not result["stopped"]:
                    raise ProjectStoreError(f"Close refused: {result['reason']}")
                messages.success(request, "Project worker closed. Committed edits remain on disk.")
            else:
                if not destination.strip():
                    raise ProjectStoreError("Enter the destination folder.")
                result = ws.operate_store(action, source, destination)
                messages.success(request, f"{action.title()} complete at {result.location}; project ID {result.project_uuid}.")
                if action == "restore":
                    messages.info(request, f"Previous project retained at {result.recovery_location}; inspect it before deleting anything.")
            return redirect("project_list")
        except ProjectStoreError as exc:
            form.add_error(None, str(exc))
    context = _landing_context(request.POST.get("source", ""))
    context["operation_form"] = form
    context["destination_preview"] = ws.describe_target(request.POST.get("destination", ""))
    return render(request, "literature/workspace_home.html", context)


# --- Remember the typed workspace root in private app state ---
@require_POST
def workspace_set_root(request: HttpRequest) -> HttpResponse:
    """Remember a workspace root; validates readability, writes settings only."""
    form = WorkspaceRootForm(request.POST)
    if not form.is_valid():
        context = _landing_context("")
        context["root_form"] = form
        return render(request, "literature/workspace_home.html", context)
    try:
        resolved = ws.set_workspace_root(form.cleaned_data["directory"])
    except ProjectStoreError as exc:
        messages.error(request, str(exc))
        context = _landing_context(form.cleaned_data["directory"])
        context["root_form"] = form
        return render(request, "literature/workspace_home.html", context)
    messages.success(request, f"Workspace folder set to {resolved}.")
    return redirect("project_list")


# --- Create at a typed location, then hand the browser to a real worker ---
@require_POST
def workspace_new(request: HttpRequest) -> HttpResponse:
    """Create one store at the typed location; New never overwrites populated data."""
    form = WorkspaceNewForm(request.POST)
    if not form.is_valid():
        context = _landing_context(request.POST.get("directory", ""))
        context["new_form"] = form
        return render(request, "literature/workspace_home.html", context)
    directory = form.cleaned_data["directory"]
    try:
        ws.check_new_target(directory)
        manifest = create_project_store(directory, form.cleaned_data["name"])
        ws.remember_recent(str(manifest.location))
        worker = ws.start_worker(manifest.location)
    except ProjectStoreError as exc:
        messages.error(request, str(exc))
        context = _landing_context(directory)
        context["new_form"] = form
        return render(request, "literature/workspace_home.html", context)
    messages.success(request, f"Created project at {manifest.location}.")
    return redirect(f"{worker['base']}{worker['prefix']}/project/{worker['project_id']}/{_manager_query(request)}")


# --- Load an existing store at a typed location, then hand off to its worker ---
@require_POST
def workspace_open(request: HttpRequest) -> HttpResponse:
    """Open an existing store; Load never creates a missing store."""
    form = WorkspaceOpenForm(request.POST)
    if not form.is_valid():
        context = _landing_context(request.POST.get("directory", ""))
        context["open_form"] = form
        return render(request, "literature/workspace_home.html", context)
    directory = form.cleaned_data["directory"]
    try:
        manifest = ws.check_load_target(directory)
        ws.remember_recent(str(manifest.location))
        worker = ws.start_worker(manifest.location)
    except ProjectStoreError as exc:
        messages.error(request, str(exc))
        context = _landing_context(directory)
        context["open_form"] = form
        return render(request, "literature/workspace_home.html", context)
    return redirect(f"{worker['base']}{worker['prefix']}/project/{worker['project_id']}/{_manager_query(request)}")


# ------------------------------------------------------------------------ #
# Worker lifecycle (same-origin tab handles; manager 404s these research paths)
# ------------------------------------------------------------------------ #


# --- Manager base for the explicit Exit hop, carried once via sessionStorage ---
def _manager_query(request: HttpRequest) -> str:
    """`?manager=<base>` so the worker tab can hop back explicitly."""
    from urllib.parse import quote

    try:
        base = request.build_absolute_uri("/")
    except (ValueError, KeyError):
        return ""
    # ponytail: one query param, no cookies or global active-project state.
    return f"?manager={quote(base.rstrip('/'), safe='')}"



# --- Tab handle endpoints live under the worker prefix (UUID/epoch checked first) ---
@ensure_csrf_cookie
def project_lifecycle(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    """Attach/heartbeat/release/status one tab-scoped handle (worker only).

    The middleware already rejected wrong-UUID/stale-epoch prefixes before
    this runs; expired handles answer 410 and never follow a reused port to
    another epoch/store. Unload sends nothing authoritative here. The drain
    channel is epoch-prefix + worker-CSRF authenticated for the owning
    manager; it never mints handles and never touches research rows.
    """
    from literature import worker as worker_mod

    bound = worker_mod.bound_store()
    if not bound.get("ok"):
        return JsonResponse({"alive": False, "error": "unknown worker"}, status=404)
    canonical = bound["store"]
    if action == "drain" and request.method == "POST":
        # ponytail: drain never touches the research DB (not even a Project
        # lookup), so it answers while a contented writer still holds the
        # store lock; epoch prefix + worker CSRF authenticate it.
        command = request.POST.get("command", "")
        if command == "start":
            snapshot = worker_mod.set_draining(True)
            return JsonResponse({"alive": True, **snapshot})
        if command == "cancel":
            snapshot = worker_mod.set_draining(False)
            return JsonResponse({"alive": True, **snapshot})
        return JsonResponse({"alive": False, "error": "unknown drain command"}, status=400)
    if action == "drain" and request.method == "GET":
        return JsonResponse({"alive": True, **worker_mod.drain_snapshot()})
    project = get_object_or_404(Project, pk=pk)
    if action == "attach" and request.method == "POST":
        # ponytail: the worker attaches a lease on itself; it never spawns
        # or reuses via start_worker (that would fork from the wrong process).
        recorded = ws.find_worker(canonical)
        try:
            if (recorded is not None
                    and str(recorded.get("worker_epoch")) == worker_mod.expected_epoch()):
                port = int(recorded["port"])
            else:
                port = int(request.get_port())
            attached = ws.attach_lease(
                canonical, bound["uuid"], worker_mod.expected_epoch(), port,
                request.POST.get("handle_id") or None,
                request.POST.get("tab_token") or None,
                request.POST.get("nav") or "navigate")
        except ProjectStoreError as exc:
            # ponytail: stale epoch or unrecordable lease is a re-open, not a crash.
            return JsonResponse({"alive": False, "error": str(exc)[:200]}, status=410)
        except (ValueError, TypeError) as exc:
            return JsonResponse({"alive": False, "error": str(exc)[:200]}, status=500)
        return JsonResponse({
            "alive": True,
            "handle_id": attached["handle_id"],
            "tab_token": attached["tab_token"],
            "reused": attached["reused"],
            "prefix": worker_mod.url_prefix(bound["uuid"], worker_mod.expected_epoch()),
            "project_id": project.pk,
        })
    if action == "heartbeat" and request.method == "POST":
        touched = ws.heartbeat_handle(request.POST.get("handle_id", ""))
        if touched is None:
            return JsonResponse({"alive": False}, status=410)
        return JsonResponse({"alive": True, "handle_id": touched["handle_id"]})
    if action == "release" and request.method == "POST":
        try:
            released = ws.release_handle(request.POST.get("handle_id", ""))
        except ProjectStoreError:
            return JsonResponse({
                "alive": False, "released": False,
                "error": "Could not release this tab; retry Exit. Your handle is retained.",
            }, status=503)
        return JsonResponse({"alive": True, "released": released})
    if action == "status" and request.method == "GET":
        lease = ws._lease_row(request.GET.get("handle_id", ""))
        if lease is None or lease["worker_epoch"] != worker_mod.expected_epoch():
            return JsonResponse({"alive": False}, status=410)
        return JsonResponse({
            "alive": True,
            "project_id": project.pk,
            "handles": ws.live_handle_count(canonical),
        })
    return JsonResponse({"alive": False, "error": "unknown action"}, status=404)
