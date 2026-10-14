#!/usr/bin/env python3

"""
Enforce fixed worker bindings, UUID/epoch routes and tab mutation admission.

Manager and research roles never switch database aliases per request. Cookie
helpers scope names/paths; middleware preserves thread-local URL prefix state.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   worker.py

@Brief:  Enforce fixed worker bindings, UUID/epoch routes and tab mutation admission.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

# Custom library Imports
from django.http import HttpResponse, HttpResponseNotFound
from django.urls import get_script_prefix, set_script_prefix

# ------------------------------------------------------------------------ #
# Roles and identity (env fixed before Django setup; never switched per request)
# ------------------------------------------------------------------------ #

#: Routes that read or mutate research rows. Everything else (settings/admin)
#: is app state. Manager serves app state only; workers serve research only
#: under their own /p/<uuid>/<epoch>/ prefix; developer keeps legacy paths.
RESEARCH_PREFIXES = ("project", "screening", "p/")

#: Manager may serve these top-level paths; all research paths fail closed.
#: The workspace landing (WP01.04) lists manifests only, never research rows.
MANAGER_ALLOWED = ("", "settings", "settings/", "admin",
                   "admin/", "workspace", "workspace/")

#: HTTP methods that never mutate research rows.
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# --- Match /p/<uuid>/<epoch>[/rest]; ordinary record URLs never carry a DB path ---
_PREFIX_RE = re.compile(
    r"^p/(?P<uuid>[^/]+)/(?P<epoch>[^/]+?)(?P<rest>/.*)?$"
)


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Immutable process role; developer preserves legacy single-DB behaviour ---
def role() -> str:
    """
    Read the process role expected to be fixed before Django startup.

    Args:
        None

    Kwargs:
        None

    Returns:
        str: WP_ROLE value, defaulting to developer; not validated here.
    """
    return os.getenv("WP_ROLE", "developer")


# --- Worker manifest UUID this process is bound to (empty when unbound) ---
def expected_uuid() -> str:
    """
    Read the worker's configured logical project UUID.

    Args:
        None

    Kwargs:
        None

    Returns:
        str: WP_PROJECT_UUID, or empty text when the binding is unspecified.
    """
    return os.getenv("WP_PROJECT_UUID", "")


# --- Worker epoch/instance this process serves (stale handles fail closed) ---
def expected_epoch() -> str:
    """
    Read the worker instance epoch used to reject stale routes and leases.

    Args:
        None

    Kwargs:
        None

    Returns:
        str: WP_WORKER_EPOCH, or empty text when the epoch is unspecified.
    """
    return os.getenv("WP_WORKER_EPOCH", "")


# --- Short cookie suffix so names never collide across workers/manager ---
def cookie_suffix(store_uuid: str) -> str:
    """
    Derive a short cookie label from a valid project UUID.

    Args:
        store_uuid (str): UUID accepted by uuid.UUID.

    Kwargs:
        None

    Returns:
        str: First eight lowercase UUID hex characters, not a security token.

    Raises:
        ValueError: If the UUID text is malformed.
    """
    # ponytail: 8 hex chars is a browser label, not a security boundary;
    # isolation comes from distinct names+paths, UUID validation stays server-side.
    return uuid.UUID(store_uuid).hex[:8]


# --- Non-colliding session/CSRF names per worker; manager has its own pair ---
def cookie_names(store_uuid: str = "") -> tuple[str, str]:
    """
    Choose the worker or manager session and CSRF cookie names.

    Args:
        None

    Kwargs:
        store_uuid (str): Worker UUID. Defaults to empty text for the manager.

    Returns:
        tuple[str, str]: (session name, CSRF name), using a UUID suffix for
            workers or (mgrsessionid, mgrcsrftoken) for the manager.

    Raises:
        ValueError: If nonempty UUID text is malformed.
    """
    if store_uuid:
        short = cookie_suffix(store_uuid)
        return f"wpsess_{short}", f"wpcsrft_{short}"
    return "mgrsessionid", "mgrcsrftoken"


# --- Path scope for worker cookies so browsers never send A cookies to B ---
def cookie_path(store_uuid: str, epoch: str) -> str:
    """
    Build a worker cookie path with a trailing slash.

    Args:
        store_uuid (str): Project UUID text, already validated by the caller.
        epoch (str): Worker epoch text, already selected by the caller.

    Kwargs:
        None

    Returns:
        str: /p/<uuid>/<epoch>/; this helper does not validate either value.
    """
    return f"/p/{store_uuid}/{epoch}/"


# --- Public prefix helper for the launcher (phase04 contract) ---
def url_prefix(store_uuid: str, epoch: str) -> str:
    """
    Build the public worker route prefix without a trailing slash.

    Args:
        store_uuid (str): Project UUID text, already validated by the caller.
        epoch (str): Worker epoch text, already selected by the caller.

    Kwargs:
        None

    Returns:
        str: /p/<uuid>/<epoch>; this helper does not validate either value.
    """
    return f"/p/{store_uuid}/{epoch}"


def manager_control_base() -> str:
    """
    Accept only a configured HTTP loopback manager origin with a port.

    Args:
        None

    Kwargs:
        None

    Returns:
        str: http://<netloc> from WP_MANAGER_BASE, or empty text for invalid
            scheme/host/port, credentials, path, query or fragment.
    """
    try:
        parts = urlsplit(os.getenv("WP_MANAGER_BASE", ""))
        if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost")
                or not parts.port or parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment):
            return ""
    except ValueError:
        return ""
    return f"http://{parts.netloc}"


# --- Research URL test used by the middleware fail-closed rules ---
def is_research_path(path: str) -> bool:
    """
    Test whether a path begins with one of the research route prefixes.

    Args:
        path (str): Usually prefix-stripped request path; leading / is ignored.

    Kwargs:
        None

    Returns:
        bool: True for a project, screening or p/ string prefix, not a full
            URL-resolution or database-access test.
    """
    stripped = path.lstrip("/")
    return stripped.startswith(RESEARCH_PREFIXES)


# ------------------------------------------------------------------------ #
# Fixed-store binding (startup/cache-bound, proven against the DB itself)
# ------------------------------------------------------------------------ #

#: Cached startup validation; recomputed never (immutable worker binding).
#: Serialise first-request validation so exactly one lifetime ownership lock survives.
_bound_store_cache: dict | None = None
_BIND_LOCK = threading.Lock()


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Prove env UUID against manifest + identity row + canonical dev/ino ---
def bound_store() -> dict:
    """
    Validate the worker's fixed database once and cache the binding.

    Reads the manifest UUID, the stdlib-owned ``wp_store_identity`` row and
    the canonical ``(st_dev, st_ino)`` of the file named by the startup
    ``DATABASES`` setting. Env-string agreement alone never authorises a
    request; a wrong file behind a right-looking UUID fails closed here.

    Args:
        None

    Kwargs:
        None

    Returns:
        dict: Cached binding with ok=False/error, or ok=True plus uuid, db,
            configured_db, dev, ino, store and ownership lock context.

    Side Effects:
        Serialize first validation under _BIND_LOCK, cache even a refusal,
        and retain a successful shared inode lock for the worker lifetime.
    """
    global _bound_store_cache
    with _BIND_LOCK:
        if _bound_store_cache is None:
            _bound_store_cache = _validate_fixed_store()
    return _bound_store_cache


# --- One read-only validation; every failure mode refuses to serve ---
def _validate_fixed_store() -> dict:
    """
    Prove the fixed database, manifest and environment identify one store.

    Args:
        None

    Kwargs:
        None

    Returns:
        dict: On refusal, ok=False and an error string. On success, ok=True,
            normalized uuid, resolved db Path, absolute configured_db Path,
            dev/ino integers, canonical store text and ownership context.

    Side Effects:
        Read manifest/identity and run read-only Load checks, which may build
        a temporary trusted schema. Enter a shared database inode lock on
        success; the returned context must be retained to keep ownership.
    """
    from django.conf import settings as dj_settings

    from literature.project_store import (
        DATABASE_FILENAME,
        IDENTITY_TABLE,
        MANIFEST_FILENAME,
        STORE_FORMAT_VERSION,
        open_project_store,
        store_file_lock,
    )

    try:
        raw_uuid = uuid.UUID(expected_uuid())
    except (ValueError, AttributeError, TypeError) as exc:
        return {"ok": False, "error": f"bad WP_PROJECT_UUID ({exc})"}
    if not expected_epoch():
        return {"ok": False, "error": "missing WP_WORKER_EPOCH"}
    try:
        configured_path = Path(dj_settings.DATABASES["default"]["NAME"])
        db_path = configured_path.resolve()
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return {"ok": False, "error": f"unusable database setting ({exc})"}
    if configured_path.name != DATABASE_FILENAME or not db_path.is_file():
        return {"ok": False, "error": f"{db_path}: not a project store database"}
    manifest_path = configured_path.parent / MANIFEST_FILENAME
    try:
        manifest_uuid = uuid.UUID(json.loads(
            manifest_path.read_text(encoding="utf-8"))["project_uuid"])
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        return {"ok": False, "error": f"{manifest_path}: bad manifest ({exc})"}
    if manifest_uuid != raw_uuid:
        return {"ok": False, "error": "manifest UUID disagrees with worker env"}
    try:
        uri = db_path.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as connection:
            rows = connection.execute(
                f"SELECT project_uuid, format_version FROM {IDENTITY_TABLE}"
            ).fetchall()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {"ok": False, "error": f"{db_path}: identity unreadable ({exc})"}
    if len(rows) != 1:
        return {"ok": False, "error": f"{db_path}: {len(rows)} identity rows"}
    try:
        identity_uuid = uuid.UUID(rows[0][0])
    except (ValueError, AttributeError, TypeError) as exc:
        return {"ok": False, "error": f"{db_path}: bad identity UUID ({exc})"}
    if identity_uuid != raw_uuid or rows[0][1] != STORE_FORMAT_VERSION:
        return {"ok": False, "error": f"{db_path}: identity disagrees with worker env"}
    try:
        file_stat = db_path.stat()
    except OSError as exc:
        return {"ok": False, "error": f"{db_path}: cannot stat ({exc})"}
    # Positive physical-file ownership survives missing registry rows and
    # manager restarts; Backup/Restore EX locks cannot pass a live worker.
    from literature.project_store import ProjectStoreError

    try:
        manifest = open_project_store(configured_path.parent)
        ownership = store_file_lock(manifest, closed=False)
        ownership.__enter__()
    except ProjectStoreError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "uuid": str(raw_uuid),
        "db": db_path,
        "configured_db": configured_path.absolute(),
        "dev": file_stat.st_dev,
        "ino": file_stat.st_ino,
        "store": str(manifest.location),
        "ownership": ownership,
    }


# --- Mutation-time guard: the file behind the binding must be the same one ---
def _store_file_intact(bound: dict) -> bool:
    """
    Recheck the configured database member against the bound device/inode.

    Args:
        bound (dict): Successful binding with configured_db, dev and ino keys.

    Kwargs:
        None

    Returns:
        bool: True if stat matches; False if the path/key is unavailable or
            the physical database member changed.
    """
    # ponytail: one stat call per unsafe request; a swapped file fails closed
    # without ever trusting a path taken from a record URL.
    try:
        file_stat = bound["configured_db"].stat()
    except (OSError, KeyError):
        return False
    return (file_stat.st_dev, file_stat.st_ino) == (bound["dev"], bound["ino"])


# ------------------------------------------------------------------------ #
# Tab-handle enforcement + drain coordination (WP01.05 reopen)
# ------------------------------------------------------------------------ #

#: Admitted mutating requests currently inside the view layer, plus the drain
#: flag. Guarded by _DRAIN_LOCK; shared by the dev-server threads of one worker.
_DRAIN_LOCK = threading.Lock()
_DRAIN_STATE = {"in_flight": 0, "draining": False}


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Snapshot of admitted mutations (manager drain polls this, never the DB) ---
def drain_snapshot() -> dict:
    """
    Read drain admission state under its process-local thread lock.

    Args:
        None

    Kwargs:
        None

    Returns:
        dict: draining boolean and in_flight admitted mutation count.
    """
    with _DRAIN_LOCK:
        return {"draining": bool(_DRAIN_STATE["draining"]),
                "in_flight": int(_DRAIN_STATE["in_flight"])}


# --- Drain flag flip; admitted requests finish, new unsafe work is refused ---
def set_draining(value: bool) -> dict:
    """
    Set drain admission state without cancelling already admitted mutations.

    Args:
        value (bool): Truth value used for the new draining flag.

    Kwargs:
        None

    Returns:
        dict: Fresh draining boolean and unchanged in_flight mutation count.

    Side Effects:
        Update process-local _DRAIN_STATE while holding _DRAIN_LOCK.
    """
    with _DRAIN_LOCK:
        _DRAIN_STATE["draining"] = bool(value)
        return {"draining": bool(_DRAIN_STATE["draining"]),
                "in_flight": int(_DRAIN_STATE["in_flight"])}


# --- Deny a mutation in the caller's content type (forms vs JSON fetch) ---
def _mutation_denied(request, status: int, message: str):
    """
    Build a mutation refusal in the caller's JSON or text content type.

    Args:
        request (HttpRequest): Request whose Content-Type/Accept selects JSON.
        status (int): HTTP status to return, usually 410 or 409.
        message (str): Error text for the response body.

    Kwargs:
        None

    Returns:
        HttpResponse: JsonResponse with ok=False/error for JSON callers,
            otherwise a text response, both using the supplied status.
    """
    # ponytail: the screening decision fetch reads data.error, so a JSON
    # denial keeps the re-open message instead of a parse failure.
    content_type = request.content_type or ""
    accepts = request.META.get("HTTP_ACCEPT", "") or ""
    if "application/json" in content_type or "application/json" in accepts:
        from django.http import JsonResponse

        return JsonResponse({"ok": False, "error": message}, status=status)
    return HttpResponse(message, status=status)


# --- Lifecycle paths mint or describe handles, so they carry no handle yet ---
def _is_lifecycle_path(rest: str) -> bool:
    """
    Detect the lifecycle substring used to exempt self-validating endpoints.

    Args:
        rest (str): Prefix-stripped path; a false value is treated as empty.

    Kwargs:
        None

    Returns:
        bool: True when /lifecycle/ occurs, not a full route validity check.
    """
    return "/lifecycle/" in (rest or "")


# --- A tab handle authorises one tab: header first, then the hidden form field ---
def _presented_handle(request) -> str:
    """
    Read the tab handle from its header before falling back to form data.

    Args:
        request (HttpRequest): Request with META and lazily parsed POST data.

    Kwargs:
        None

    Returns:
        str: X-Project-Handle, then wp_handle, or empty text if absent or the
            form lookup raises AttributeError, ValueError or OSError.

    Side Effects:
        Accessing POST can trigger Django request-body parsing.
    """
    handle_id = request.META.get("HTTP_X_PROJECT_HANDLE", "") or ""
    if handle_id:
        return str(handle_id)
    try:
        return str(request.POST.get("wp_handle", "") or "")
    except (AttributeError, ValueError, OSError):
        return ""


# --- Lease must exist for this exact UUID+epoch; anything else fails closed ---
def _valid_tab_handle(request, bound: dict) -> bool:
    """
    Require a presented tab lease matching this UUID and worker epoch.

    Args:
        request (HttpRequest): Request presenting a header or form tab handle.
        bound (dict): Successful fixed-store binding containing uuid.

    Kwargs:
        None

    Returns:
        bool: True for an existing matching lease; False for missing handles,
            imports, lease reads or mismatched UUID/epoch. No heartbeat-age
            expiry calculation is performed here.

    Raises:
        ProjectStoreError: If private registry initialization fails before
            the lease lookup; this failure is not caught here.

    Side Effects:
        The lease helper initializes/upgrades private registry tables if needed
        before reading; research rows are not modified.
    """
    # ponytail: leases govern shutdown AND mutation rights; UUID/epoch alone
    # only prove the store, the handle proves the tab still owns it.
    handle_id = _presented_handle(request)
    if not handle_id:
        return False
    try:
        from literature.workspace import _lease_row
    except ImportError:
        return False
    try:
        lease = _lease_row(handle_id)
    except (sqlite3.Error, OSError, ValueError):
        return False
    if not lease:
        return False
    return (lease["project_uuid"] == bound["uuid"]
            and lease["worker_epoch"] == expected_epoch())


# ------------------------------------------------------------------------ #
# Middleware: validate UUID/epoch, then rewrite to the legacy route table
# ------------------------------------------------------------------------ #


class WorkerBindingMiddleware:
    """
    Bind research requests to the fixed worker store before any view runs.

    Developer (default) keeps legacy int-pk URLs untouched. A worker serves
    research only under its own /p/<uuid>/<epoch>/ prefix with a
    startup-validated store binding; other routes are refused, and
    settings/admin/app-credential routes are never served
    by a worker. The worker script prefix is set per request (restored
    afterwards) so ``reverse()``, ``{% url %}``, redirects and JSON URLs all
    keep the UUID/epoch prefix with thread/request isolation. A manager serves
    the manifest-only workspace plus settings/admin, never research rows.

    Unsafe worker requests additionally require an intact member and, outside
    lifecycle endpoints, a matching tab lease and drain admission. This does
    not replace or bypass Django's separate CSRF middleware checks.
    """

    def __init__(self, get_response):
        """
        Retain Django's downstream middleware/view callable.

        Args:
            get_response (Callable): Downstream callable accepting a request
                and returning an HTTP response.

        Kwargs:
            None

        Returns:
            None
        """
        self.get_response = get_response

    def __call__(self, request):
        """
        Bind routes and count admitted mutations across downstream handling.

        Args:
            request (HttpRequest): Incoming request to annotate and route.

        Kwargs:
            None

        Returns:
            HttpResponse: Downstream response or binding/drain refusal.

        Side Effects:
            Annotate request context and possibly rewrite path_info. Set and
            restore the thread-local script prefix, including on downstream
            failure. Count unsafe admissions under _DRAIN_LOCK and decrement
            in finally; a concurrent drain refuses new admission with 409.
            Downstream exceptions propagate after state restoration.
        """
        request.manager_base = manager_control_base() if role() == "worker" else ""
        request.legacy_project = role() == "developer" and is_research_path(request.path_info)
        request.project_copy_url = ""
        prefix = self._bind(request)
        if prefix is None:
            return self.get_response(request)
        if not isinstance(prefix, str):
            return prefix
        # ponytail: thread-local prefix, restored after the response, so
        # concurrent workers on reused threads cannot leak prefixes.
        original = get_script_prefix()
        set_script_prefix(prefix)
        if not getattr(request, "_wp_track", False):
            try:
                return self.get_response(request)
            finally:
                set_script_prefix(original)
        # ponytail: admitted mutations hold the counter across the view, so a
        # bounded drain waits for exactly these before any shutdown signal.
        with _DRAIN_LOCK:
            if _DRAIN_STATE["draining"]:
                admitted = False
            else:
                _DRAIN_STATE["in_flight"] += 1
                admitted = True
        if not admitted:
            set_script_prefix(original)
            return _mutation_denied(
                request, 409,
                "Worker is draining for shutdown; re-open the project.")
        try:
            return self.get_response(request)
        finally:
            with _DRAIN_LOCK:
                _DRAIN_STATE["in_flight"] -= 1
            set_script_prefix(original)

    # --- Attach or reject worker context; never rebind DATABASES ---
    def _bind(self, request):
        """
        Attach fixed-store context or refuse routes without DB rebinding.

        Args:
            request (HttpRequest): Request with initialized manager_base;
                path_info and method select role, identity and admission gates.

        Kwargs:
            None

        Returns:
            str | HttpResponse | None: Worker script prefix on success, 404
                for wrong roles/UUID/epoch/member/routes, 409 during drain, or
                410 for missing/foreign leases. None leaves script-prefix
                handling unchanged for allowed manager/developer requests.

        Side Effects:
            Annotate UUID/epoch, worker flags and verified store/copy links;
            strip valid prefixes from path_info. Worker validation can cache
            a lifetime inode lock, and handle checks can initialize the private
            registry. Mark non-lifecycle unsafe worker calls for tracking.
        """
        path = request.path_info.lstrip("/")
        current_role = role()
        match = _PREFIX_RE.match(path)
        if match is not None:
            raw_uuid, epoch, rest = (
                match.group("uuid"),
                match.group("epoch"),
                match.group("rest") or "/",
            )
            try:
                store_uuid = str(uuid.UUID(raw_uuid))
            except ValueError:
                return HttpResponseNotFound("Unknown project worker.")
            request.worker_uuid = store_uuid
            request.worker_epoch = epoch
            # ponytail: only a real worker binds tokens/cookies to the prefix;
            # developer keeps legacy behaviour even on manually prefixed URLs.
            request.worker_route = current_role == "worker"
            if current_role == "worker":
                bound = bound_store()
                if not bound["ok"]:
                    # ponytail: misconfigured worker serves nothing, no fallback.
                    return HttpResponseNotFound("Unknown project worker.")
                if store_uuid != bound["uuid"] or epoch != expected_epoch():
                    # ponytail: stale/wrong-project handles fail closed, no fallback.
                    return HttpResponseNotFound("Unknown project worker.")
                if not is_research_path(rest):
                    # ponytail: workers never serve settings/admin/credentials.
                    return HttpResponseNotFound("Unknown project worker.")
                request.project_store_location = bound["store"]
                if request.manager_base:
                    # Paths come from the proven fixed worker binding, never
                    # from ordinary research URL parameters or the browser.
                    request.project_copy_url = (
                        request.manager_base + "/workspace/?"
                        + urlencode({"directory": bound["store"], "operation": "copy"})
                        + "#project-operations"
                    )
                if request.method not in _SAFE_METHODS and not _store_file_intact(bound):
                    return HttpResponseNotFound("Unknown project worker.")
                if request.method not in _SAFE_METHODS and not _is_lifecycle_path(rest):
                    # ponytail: drain rejects new work before handle checks, so
                    # shutdown never admits a write it will not finish.
                    with _DRAIN_LOCK:
                        draining = _DRAIN_STATE["draining"]
                    if draining:
                        return _mutation_denied(
                            request, 409,
                            "Worker is draining for shutdown; re-open the project.")
                    if not _valid_tab_handle(request, bound):
                        # ponytail: released/expired/foreign handles fail here
                        # with no DB touch; stale epochs already 404 above.
                        return _mutation_denied(
                            request, 410,
                            "Missing or expired project handle; re-open the project.")
                    request._wp_track = True
            elif current_role == "manager":
                return HttpResponseNotFound("Unknown project worker.")
            # ponytail: strip the prefix so the legacy route table stays canonical.
            request.path_info = rest
            if current_role == "worker":
                return f"/p/{bound['uuid']}/{expected_epoch()}"
            return None
        request.worker_uuid = ""
        request.worker_epoch = ""
        request.worker_route = False
        if current_role == "worker":
            # ponytail: a worker without its UUID/epoch prefix serves nothing,
            # not even settings; missing context fails closed, never legacy DB.
            return HttpResponseNotFound("Unknown project worker.")
        if current_role == "manager":
            if is_research_path(path):
                return HttpResponseNotFound("Unknown project worker.")
            first = path.split("/", 1)[0]
            # ponytail: manager serves the manifest-only workspace landing plus
            # settings/admin; research rows stay fail-closed.
            if first not in MANAGER_ALLOWED:
                return HttpResponseNotFound("Unknown project worker.")
            return None
        return None
