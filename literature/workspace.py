#!/usr/bin/env python3

'''
@File:   workspace.py

@Brief:  Workspace root memory, safe discovery and minimal worker spawn (WP01.04).

@Date:   03/10/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

import fcntl
import hashlib
import http.client
import http.cookiejar
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path

# Custom library imports
from django.conf import settings

from literature.project_store import (
    APP_SETTINGS_DEFAULTS,
    MANIFEST_FILENAME,
    ProjectStoreError,
    StoreManifest,
    app_data_dir,
    app_registry_path,
    app_settings_path,
    backup_project_store,
    init_app_registry,
    inspect_project_store,
    open_project_store,
    read_manifest,
    restore_project_store,
    save_copy_store,
    validate_package,
)

# ------------------------------------------------------------------------ #
# Private application settings (workspace memory, never research truth)
# ------------------------------------------------------------------------ #

#: Recent-location memory bound; older entries drop off, never auto-deleted files.
_MAX_RECENT = 10


# --- Load workspace preferences; a corrupt file falls back, never crashes ---
def load_app_settings() -> dict:
    """Read private settings.json; missing/corrupt files yield defaults."""
    path = app_settings_path()
    data = dict(APP_SETTINGS_DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return data
    if isinstance(raw, dict):
        if isinstance(raw.get("workspace_roots"), list):
            data["workspace_roots"] = [str(p) for p in raw["workspace_roots"][:8]]
        if isinstance(raw.get("recent_projects"), list):
            data["recent_projects"] = [str(p) for p in raw["recent_projects"][:_MAX_RECENT]]
    return data


# --- Persist workspace preferences beside the registry, never in projects ---
def save_app_settings(data: dict) -> Path:
    """Write settings.json atomically; credentials never belong here."""
    path = app_settings_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectStoreError(f"{path}: cannot create settings dir ({exc})") from exc
    doc = {
        "format_version": 1,
        "workspace_roots": list(data.get("workspace_roots", []))[:8],
        "recent_projects": list(data.get("recent_projects", []))[:_MAX_RECENT],
    }
    tmp = path.parent / f".settings.tmp-{os.getpid()}"
    try:
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise ProjectStoreError(f"{path}: cannot write settings ({exc})") from exc
    return path


# --- Remembered root is the first stored workspace root, if any ---
def get_workspace_root() -> Path | None:
    """Return the remembered workspace root, or None when never chosen."""
    roots = load_app_settings().get("workspace_roots", [])
    if not roots:
        return None
    return Path(roots[0]).expanduser()


# --- Validate and remember a workspace root directory ---
def set_workspace_root(directory: str) -> Path:
    """Remember a workspace root; the directory must already exist and be readable."""
    raw = (directory or "").strip()
    if not raw:
        raise ProjectStoreError("Enter a workspace folder path.")
    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise ProjectStoreError(f"{candidate}: cannot resolve path ({exc})") from exc
    if not resolved.is_dir():
        raise ProjectStoreError(f"{resolved}: not a directory")
    if not os.access(resolved, os.R_OK | os.X_OK):
        raise ProjectStoreError(f"{resolved}: directory is not readable")
    data = load_app_settings()
    roots = [str(resolved)]
    roots += [p for p in data.get("workspace_roots", []) if p != str(resolved)]
    data["workspace_roots"] = roots[:8]
    save_app_settings(data)
    return resolved


# --- Remember a successfully opened location for the next visit ---
def remember_recent(canonical: str) -> None:
    """Prepend a canonical location to recent_projects (memory only)."""
    data = load_app_settings()
    recent = [canonical]
    recent += [p for p in data.get("recent_projects", []) if p != canonical]
    data["recent_projects"] = recent[:_MAX_RECENT]
    save_app_settings(data)


# ------------------------------------------------------------------------ #
# Typed-path target resolution (what the user typed, made explicit)
# ------------------------------------------------------------------------ #


# --- Describe a typed directory without creating or migrating anything ---
def describe_target(directory: str) -> dict:
    """Resolve a typed path and report location, permissions and store state."""
    raw = (directory or "").strip()
    if not raw:
        return {"input": directory, "error": "Enter a folder path."}
    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        return {"input": raw, "error": f"Cannot resolve path ({exc})"}
    info: dict = {"input": raw, "resolved": str(resolved)}
    info["exists"] = resolved.exists()
    info["is_dir"] = resolved.is_dir()
    info["readable"] = bool(os.access(resolved, os.R_OK | os.X_OK)) if resolved.exists() else False
    parent = resolved if resolved.is_dir() else resolved.parent
    info["writable"] = bool(os.access(parent, os.W_OK | os.X_OK)) if parent.exists() else False
    if (resolved / MANIFEST_FILENAME).is_file():
        try:
            manifest = open_project_store(resolved)
        except ProjectStoreError as exc:
            info["store"] = f"Not openable: {exc}"
        else:
            info["store"] = f"Project {manifest.project_uuid} (openable)"
            info["project_uuid"] = str(manifest.project_uuid)
    elif resolved.is_dir() and resolved.exists():
        try:
            populated = any(resolved.iterdir())
        except OSError:
            populated = True
        info["store"] = "No project here yet." if not populated else "Folder has files but no project manifest."
    else:
        info["store"] = "Location does not exist yet."
    return info


# --- New must never overwrite a populated directory ---
def check_new_target(directory: str) -> Path:
    """Validate a creation destination; refuses anything populated."""
    raw = (directory or "").strip()
    if not raw:
        raise ProjectStoreError("Enter a folder path for the new project.")
    dest = Path(raw).expanduser()
    if dest.exists() and not dest.is_dir():
        raise ProjectStoreError(f"{dest.resolve()}: target exists and is not a directory")
    if dest.is_dir():
        try:
            if any(dest.iterdir()):
                raise ProjectStoreError(
                    f"{dest.resolve()}: refusing to overwrite a populated directory"
                )
        except OSError as exc:
            raise ProjectStoreError(f"{dest}: cannot list directory ({exc})") from exc
    parent = dest if dest.is_dir() else dest.parent
    if not parent.exists():
        raise ProjectStoreError(f"{dest}: parent directory is missing")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise ProjectStoreError(f"{dest}: destination is not writable")
    try:
        return dest.resolve()
    except (OSError, RuntimeError) as exc:
        raise ProjectStoreError(f"{dest}: cannot resolve path ({exc})") from exc


# --- Load must never create a missing store ---
def check_load_target(directory: str):
    """Validate an existing store for loading; missing stores are refused."""
    raw = (directory or "").strip()
    if not raw:
        raise ProjectStoreError("Enter the folder path of the project to open.")
    manifest = validate_package(Path(raw).expanduser(), strict_members=False)
    allowed = {"project.json", "project.sqlite3", "exports", "backups",
               "project.sqlite3-wal", "project.sqlite3-shm", "project.sqlite3-journal"}
    try:
        if {p.name for p in manifest.location.iterdir()} - allowed:
            raise ProjectStoreError("unknown project members; refusing Load")
    except OSError as exc:
        raise ProjectStoreError(f"cannot inspect project members ({exc})") from exc
    return manifest


# ------------------------------------------------------------------------ #
# Nonrecursive safe discovery (recognised manifests only)
# ------------------------------------------------------------------------ #


# --- Read-only project display name for a store database ---
def _project_name(database_path: Path) -> str:
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as connection:
            row = connection.execute("SELECT name FROM literature_project LIMIT 1").fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return ""
    return str(row[0]) if row else ""


# --- List recognised projects directly inside a root (never recursive) ---
def discover_workspace(root: Path | str | None) -> dict:
    """List project manifests at most one level deep; aliases dedup, UUID clashes flagged.

    Never recurses, never follows untrusted manifest paths, never copies data.
    Broken/looping links become error entries, never crashes.
    """
    result: dict = {"root": "" if root is None else str(root), "entries": [], "errors": []}
    if root is None:
        result["errors"].append("Choose a workspace folder first.")
        return result
    base = Path(root).expanduser()
    try:
        resolved_base = base.resolve()
    except (OSError, RuntimeError) as exc:
        result["errors"].append(f"{base}: cannot resolve workspace root ({exc})")
        return result
    result["root"] = str(resolved_base)
    if not resolved_base.is_dir():
        result["errors"].append(f"{resolved_base}: not a directory")
        return result
    # ponytail: the root itself plus direct children only; no os.walk anywhere.
    try:
        candidates = [resolved_base] + sorted(resolved_base.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        result["errors"].append(f"{resolved_base}: cannot list directory ({exc})")
        return result
    seen: dict[str, dict] = {}
    uuid_places: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.is_symlink() and not candidate.exists():
            result["errors"].append(f"{candidate.name}: broken link (target missing)")
            continue
        try:
            is_dir = candidate.is_dir()
        except OSError as exc:
            result["errors"].append(f"{candidate.name}: cannot inspect ({exc})")
            continue
        if not is_dir:
            continue
        try:
            report = inspect_project_store(candidate)
        except (ProjectStoreError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            # ponytail: broken/looping links land here; the landing never crashes.
            result["errors"].append(f"{candidate.name}: cannot inspect ({exc})")
            continue
        if "project_uuid" not in report and "manifest_error" in report:
            if "no project.json" in str(report["manifest_error"]):
                continue
            result["errors"].append(f"{candidate.name}: {report['manifest_error']}")
            continue
        location = str(report.get("location", candidate))
        if location in seen:
            seen[location]["aliases"] = seen[location].get("aliases", 0) + 1
            continue
        entry = {
            "directory": location,
            "name": "",
            "project_uuid": str(report.get("project_uuid", "")),
            "complete": bool(report.get("complete", False)),
            "detail": str(report.get("database_error", report.get("manifest_error", ""))),
            "aliases": 0,
            "collision": False,
        }
        if entry["complete"]:
            entry["name"] = _project_name(Path(location) / "project.sqlite3")
        seen[location] = entry
        if entry["project_uuid"]:
            uuid_places.setdefault(entry["project_uuid"], set()).add(location)
    clashes = {u for u, places in uuid_places.items() if len(places) > 1}
    entries = sorted(seen.values(), key=lambda e: (e["name"] or e["directory"]).lower())
    for entry in entries:
        if entry["project_uuid"] in clashes:
            # ponytail: same UUID at different files is an explicit collision,
            # never a merge; the user opens one location deliberately.
            entry["collision"] = True
    result["entries"] = entries
    return result


# ------------------------------------------------------------------------ #
# Minimal worker start (one app-owned process per canonical store)
# ------------------------------------------------------------------------ #

#: How long the manager waits for a fresh worker to answer HTTP.
_WORKER_READY_TIMEOUT = 90


# --- A free loopback port for the next worker; the worker binds it ---
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --- True when a pid is alive (registry reuse check, never a kill) ---
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --- Registry row for a canonical store, if this app recorded one ---
def _registry_row(canonical: str) -> dict | None:
    registry = init_app_registry(app_registry_path())
    try:
        with sqlite3.connect(registry) as connection:
            row = connection.execute(
                "SELECT store_canonical, pid, port, worker_epoch FROM workers WHERE store_canonical = ?",
                (canonical,),
            ).fetchone()
    except (sqlite3.Error, OSError):
        return None
    if not row:
        return None
    return {"store_canonical": row[0], "pid": row[1], "port": row[2], "worker_epoch": row[3]}


# --- Record the worker this app owns for a canonical store ---
def _record_worker(canonical: str, pid: int, port: int, epoch: str) -> None:
    from datetime import UTC, datetime

    registry = init_app_registry(app_registry_path())
    try:
        with sqlite3.connect(registry) as connection:
            connection.execute(
                "DELETE FROM leases WHERE store_canonical = ? AND worker_epoch != ?",
                (canonical, epoch),
            )
            connection.execute(
                "INSERT OR REPLACE INTO workers "
                "(store_canonical, pid, port, worker_epoch, lease_owner, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (canonical, pid, port, epoch, "manager", datetime.now(UTC).isoformat()),
            )
            connection.commit()
    except (sqlite3.Error, OSError) as exc:
        raise ProjectStoreError(f"cannot record worker ({exc})") from exc


# --- Single project id of a store (stdlib read-only; ids stay store-local) ---
def store_project_id(database_path: Path) -> int:
    """Return the one literature_project id; load already proved exactly one."""
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as connection:
            row = connection.execute("SELECT id FROM literature_project LIMIT 1").fetchone()
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise ProjectStoreError(f"{database_path}: cannot read project id ({exc})") from exc
    if not row:
        raise ProjectStoreError(f"{database_path}: store holds no project")
    return int(row[0])


# --- Prove a recorded worker still answers for this exact UUID+epoch ---
def _worker_answers(port: int, store_uuid: str, epoch: str, project_id: int) -> bool:
    url = f"http://127.0.0.1:{port}/p/{store_uuid}/{epoch}/project/{project_id}/papers/"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status == 200
    except (OSError, ValueError, http.client.HTTPException):
        # ponytail: any transport/refusal means "not our live worker"; reuse refused.
        return False


# ------------------------------------------------------------------------ #
# Tab-scoped open handles + manager-owned child lifecycle (WP01.05)
# ------------------------------------------------------------------------ #

#: Positively tracked worker children in this manager process only.
#: Shutdown never signals a registry PID without the matching live Popen here.
_CHILDREN: dict[str, subprocess.Popen[bytes]] = {}

#: Lease/handle bounds (seconds). Heartbeats refresh last_seen; expiry only
#: drops the lease row, never project data. Workers stop only with zero
#: live handles after the grace bound. Tests and the later launcher tune
#: them through WP_HANDLE_TTL / WP_WORKER_GRACE; production uses the defaults.
_HANDLE_TTL_DEFAULT = 300.0
_WORKER_GRACE_DEFAULT = 120.0

#: A same-token heartbeat this fresh means the source tab is still alive, so
#: a second "navigate" arrival with the same token is a duplicated tab, not a
#: same-tab navigation (which unloads its source). Either way the outcome is
#: safe: duplicates mint their own handle, navigations keep theirs.
_FRESH_WINDOW = 90.0


# --- Tunable bound with a safe default; garbage env never breaks cleanup ---
def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


# --- One OS lock file per canonical store (Linux-only package, stdlib only) ---
def _store_lock_path(canonical: str) -> Path:
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
    return app_data_dir() / "locks" / (digest + ".lock")


# --- Serialize check-then-act across threads/processes sharing a registry ---
@contextmanager
def _hold_store_lock(canonical: str):
    """Exclusive per-store lock for acquire/register/stop critical sections.

    Heartbeats and releases stay single-statement SQLite writes outside the
    lock; only spawn/record/lease-insert/stop sequences take it.
    """
    path = _store_lock_path(canonical)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectStoreError(f"{path}: cannot create lock dir ({exc})") from exc
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise ProjectStoreError(f"{path}: cannot open lock ({exc})") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# --- Read one lease row; missing means expired/released, never an error ---
def _lease_row(handle_id: str) -> dict | None:
    registry = init_app_registry(app_registry_path())
    try:
        with sqlite3.connect(registry) as connection:
            row = connection.execute(
                "SELECT handle_id, store_canonical, project_uuid, worker_epoch,"
                " port, created_at, last_seen, tab_token FROM leases"
                " WHERE handle_id = ?",
                (str(handle_id),),
            ).fetchone()
    except (sqlite3.Error, OSError):
        return None
    if not row:
        return None
    return {
        "handle_id": row[0],
        "store_canonical": row[1],
        "project_uuid": row[2],
        "worker_epoch": row[3],
        "port": row[4],
        "created_at": row[5],
        "last_seen": row[6],
        "tab_token": row[7] if len(row) > 7 else None,
    }


# --- Count live leases holding a canonical store open ---
def live_handle_count(canonical: str) -> int:
    """Leases currently holding this store open (stale epochs count until swept)."""
    registry = init_app_registry(app_registry_path())
    try:
        with sqlite3.connect(registry) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE store_canonical = ?",
                (canonical,),
            ).fetchone()
    except (sqlite3.Error, OSError):
        return 0
    return int(row[0]) if row else 0


# --- Record one lease row for an already-running worker (manager or worker use) ---
def register_handle(canonical: str, project_uuid: str, epoch: str, port: int,
                    tab_token: str | None = None) -> str:
    """Insert a tab lease for this exact worker epoch; stale epochs never register."""
    from datetime import UTC, datetime

    with _hold_store_lock(canonical):
        recorded = _registry_row(canonical)
        if (recorded is not None
                and str(recorded.get("worker_epoch")) != str(epoch)):
            # ponytail: the worker restarted under this tab; the tab re-opens
            # deliberately instead of joining a foreign epoch.
            raise ProjectStoreError("worker epoch moved; re-open the project")
        handle_id = f"h{uuid.uuid4().hex[:11]}"
        token = tab_token or f"t{uuid.uuid4().hex[:11]}"
        now = datetime.now(UTC).isoformat()
        registry = init_app_registry(app_registry_path())
        try:
            with sqlite3.connect(registry) as connection:
                connection.execute(
                    "INSERT INTO leases (handle_id, store_canonical, project_uuid,"
                    " worker_epoch, port, created_at, last_seen, tab_token)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (handle_id, canonical, project_uuid, epoch, int(port), now, now, token),
                )
                connection.commit()
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise ProjectStoreError(f"cannot record project handle ({exc})") from exc
        _touch_last_use(canonical)
    return handle_id


# --- Last-use clock for the idle grace: every live tab refreshes its worker ---
def _touch_last_use(canonical: str) -> None:
    """Mark a worker used-now; best-effort, never fails a tab request."""
    from datetime import UTC, datetime

    try:
        registry = init_app_registry(app_registry_path())
        with sqlite3.connect(registry) as connection:
            connection.execute(
                "UPDATE workers SET updated_at = ? WHERE store_canonical = ?",
                (datetime.now(UTC).isoformat(), canonical),
            )
            connection.commit()
    except (sqlite3.Error, OSError, ValueError):
        pass


# --- Page-load ownership: reload/Back keep, duplicates mint, rest re-attach ---
def attach_lease(canonical: str, project_uuid: str, epoch: str, port: int,
                 handle_id: str | None, tab_token: str | None,
                 nav_type: str) -> dict:
    """Bind one page load to a tab-owned handle.

    Reload and history-Back keep the stored handle. A "navigate" arrival
    presenting a handle whose source tab still heartbeats is a duplicated tab
    (sessionStorage was copied) and mints its own handle, so Exit in one tab
    can never release the survivor. A stale source means same-tab navigation
    and keeps the handle. Unknown/expired handles and epoch moves mint anew
    or fail, never join a foreign worker.
    """
    from datetime import UTC, datetime

    with _hold_store_lock(canonical):
        recorded = _registry_row(canonical)
        if (recorded is not None
                and str(recorded.get("worker_epoch")) != str(epoch)):
            raise ProjectStoreError("worker epoch moved; re-open the project")
        lease = _lease_row(handle_id) if handle_id else None
        if (lease is not None and lease["project_uuid"] == project_uuid
                and lease["worker_epoch"] == str(epoch)):
            if nav_type in ("same_tab", "reload", "back_forward"):
                _refresh_lease(lease["handle_id"], tab_token)
                _touch_last_use(canonical)
                return {"handle_id": lease["handle_id"],
                        "tab_token": lease["tab_token"] or tab_token or "",
                        "reused": True}
            if nav_type == "navigate" and lease.get("tab_token") and tab_token \
                    and lease["tab_token"] == tab_token and _lease_fresh(lease):
                # ponytail: source tab alive with the same token -> duplicate.
                pass
            else:
                _refresh_lease(lease["handle_id"], tab_token)
                _touch_last_use(canonical)
                return {"handle_id": lease["handle_id"],
                        "tab_token": tab_token or lease["tab_token"] or "",
                        "reused": True}
        fresh_id = f"h{uuid.uuid4().hex[:11]}"
        fresh_token = f"t{uuid.uuid4().hex[:11]}"
        now = datetime.now(UTC).isoformat()
        registry = init_app_registry(app_registry_path())
        try:
            with sqlite3.connect(registry) as connection:
                connection.execute(
                    "INSERT INTO leases (handle_id, store_canonical, project_uuid,"
                    " worker_epoch, port, created_at, last_seen, tab_token)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (fresh_id, canonical, project_uuid, epoch, int(port),
                     now, now, fresh_token),
                )
                connection.commit()
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise ProjectStoreError(f"cannot record project handle ({exc})") from exc
        _touch_last_use(canonical)
    return {"handle_id": fresh_id, "tab_token": fresh_token, "reused": False}


# --- Fresh means the source tab still heartbeats (duplicates stay alive) ---
def _lease_fresh(lease: dict) -> bool:
    from datetime import UTC, datetime

    try:
        seen = datetime.fromisoformat(str(lease.get("last_seen")))
    except ValueError:
        return False
    return (datetime.now(UTC) - seen).total_seconds() <= _FRESH_WINDOW


# --- Same-tab navigation adopts the arriving token and refreshes the lease ---
def _refresh_lease(handle_id: str, tab_token: str | None) -> None:
    from datetime import UTC, datetime

    registry = init_app_registry(app_registry_path())
    try:
        with sqlite3.connect(registry) as connection:
            if tab_token:
                connection.execute(
                    "UPDATE leases SET last_seen = ?, tab_token = ?"
                    " WHERE handle_id = ?",
                    (datetime.now(UTC).isoformat(), tab_token, str(handle_id)),
                )
            else:
                connection.execute(
                    "UPDATE leases SET last_seen = ? WHERE handle_id = ?",
                    (datetime.now(UTC).isoformat(), str(handle_id)),
                )
            connection.commit()
    except (sqlite3.Error, OSError, ValueError):
        pass


# --- Open (or reuse) the worker, then issue this tab's own handle ---
def acquire_handle(directory: Path | str, tab_token: str | None = None) -> dict:
    """Ensure the store worker and record one tab-scoped lease on it.

    A/A tabs share one worker (reuse) with distinct handles; A/B stores
    never share. The handle spans navigation/redirects via sessionStorage;
    unload alone never releases it. The locked register refuses a moved
    epoch, so a racing restart mints fresh instead of joining wrong.
    """
    manifest = open_project_store(directory)
    canonical = str(manifest.location)
    worker = start_worker(directory)
    handle_id = register_handle(
        canonical, str(manifest.project_uuid), worker["epoch"], worker["port"],
        tab_token=tab_token)
    return {**worker, "handle_id": handle_id, "store_canonical": canonical,
            "project_uuid": str(manifest.project_uuid)}


# --- Refresh a tab handle; stale epochs fail instead of following a reused port ---
def heartbeat_handle(handle_id: str) -> dict | None:
    """Touch last_seen when the lease still matches the recorded worker epoch.

    Returns None when the handle is unknown/released/expired, or when the
    worker epoch moved on (restart/port-reuse): the tab must re-open
    deliberately instead of writing through a stale prefix.
    """
    from datetime import UTC, datetime

    lease = _lease_row(handle_id)
    if lease is None:
        return None
    recorded = _registry_row(lease["store_canonical"])
    if recorded is None or str(recorded.get("worker_epoch")) != lease["worker_epoch"]:
        # ponytail: epoch moved (restart); stale handle fails, never follows the port.
        return None
    try:
        int(recorded["port"])
    except (TypeError, ValueError):
        return None
    registry = init_app_registry(app_registry_path())
    try:
        with sqlite3.connect(registry) as connection:
            connection.execute(
                "UPDATE leases SET last_seen = ? WHERE handle_id = ?",
                (datetime.now(UTC).isoformat(), handle_id),
            )
            connection.commit()
    except (sqlite3.Error, OSError):
        return None
    _touch_last_use(lease["store_canonical"])
    return {**lease, "port": int(recorded["port"]),
            "worker_epoch": str(recorded["worker_epoch"])}


# --- Explicit Back/Exit releases only this tab's handle ---
def release_handle(handle_id: str) -> bool:
    """Release idempotently; raise ProjectStoreError if absence cannot be committed."""
    lease = _lease_row(handle_id)
    registry = init_app_registry(app_registry_path())
    try:
        with sqlite3.connect(registry) as connection:
            cursor = connection.execute(
                "DELETE FROM leases WHERE handle_id = ?", (str(handle_id),))
            connection.commit()
    except (sqlite3.Error, OSError) as exc:
        raise ProjectStoreError(f"cannot release project handle ({exc})") from exc
    if cursor.rowcount > 0 and lease is not None:
        _touch_last_use(lease["store_canonical"])
    return True  # A committed DELETE also proves an already-absent lease released.


# --- Stop ONLY a positively tracked child; registry PIDs alone never signal ---
def stop_worker(canonical: str, timeout: float = 20.0, *, _locked: bool = False) -> dict:
    """Terminate the owned worker for a store after its handles are gone.

    Operates solely on the live Popen started by this process whose pid
    still matches the registry row. Zero/negative/reused/unrelated pids,
    missing ownership (manager restart), live handles, or an already-exited
    child all refuse without signalling anything. The per-store lock holds
    across check and signal, so a concurrent acquire either joins before the
    check (stop refuses) or spawns after the forget (never killed).
    """
    with nullcontext() if _locked else _hold_store_lock(canonical):
        proc = _CHILDREN.get(canonical)
        if proc is None:
            # ponytail: no Popen identity here (e.g. manager restarted) -> never
            # signal the registry pid; adoption-by-reuse only, kill ownership stays
            # with the spawner. Documented limitation, not a silent kill.
            return {"stopped": False, "reason": "not-owned"}
        pid = proc.pid
        if not isinstance(pid, int) or pid <= 0:
            return {"stopped": False, "reason": "bad-pid"}
        recorded = _registry_row(canonical)
        if recorded is not None:
            try:
                recorded_pid = int(recorded["pid"])
            except (TypeError, ValueError):
                return {"stopped": False, "reason": "bad-recorded-pid"}
            if recorded_pid != pid:
                # ponytail: pid reused or row belongs elsewhere; never signal.
                return {"stopped": False, "reason": "pid-mismatch"}
        if proc.poll() is not None:
            _CHILDREN.pop(canonical, None)
            try:
                forget_worker(canonical)
            except (OSError, ValueError):
                pass
            return {"stopped": False, "reason": "already-exited"}
        if live_handle_count(canonical) > 0:
            # ponytail: another tab still holds this store; never kill its worker.
            return {"stopped": False, "reason": "handles-live"}
        try:
            proc.terminate()
        except OSError:
            return {"stopped": False, "reason": "terminate-failed"}
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                return {"stopped": False, "reason": "kill-failed"}
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                return {"stopped": False, "reason": "still-running"}
        _CHILDREN.pop(canonical, None)
        forget_worker(canonical)
        try:
            open_project_store(canonical)
        except ProjectStoreError as exc:
            return {"stopped": True, "reason": f"stopped-but-unverifiable ({exc})"}
        return {"stopped": True, "reason": "stopped"}


# ------------------------------------------------------------------------ #
# Bounded drain channel (reject new work, wait admitted, then stop)
# ------------------------------------------------------------------------ #

#: How long one drain HTTP round takes before the attempt is abandoned.
_DRAIN_HTTP_TIMEOUT = 10.0


# --- Cookie jar primed with the worker's own path-scoped CSRF token ---
def _drain_session(base: str, prefix: str, project_id: int):
    """Opener holding the worker CSRF cookie; genuine token, no bypass.

    Primes from the drain endpoint itself: always 200 with no handle, so
    error responses never eat the cookie before the first POST.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(
        f"{base}{prefix}/project/{project_id}/lifecycle/drain/", method="GET")
    try:
        with opener.open(request, timeout=_DRAIN_HTTP_TIMEOUT):
            pass
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise ProjectStoreError(f"drain status unreachable ({exc})") from exc
    token = ""
    for cookie in jar:
        if cookie.name.startswith("wpcsrft_"):
            token = cookie.value
            break
    if not token:
        raise ProjectStoreError("drain CSRF cookie missing")
    return opener, token


# --- One authenticated drain POST (epoch prefix + worker CSRF, bounded) ---
def _drain_post(opener, token: str, url: str, fields: dict | None = None) -> dict:
    data = urllib.parse.urlencode(fields or {}, doseq=True).encode()
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-CSRFToken": token})
    try:
        with opener.open(request, timeout=_DRAIN_HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, http.client.HTTPException,
            ValueError) as exc:
        raise ProjectStoreError(f"drain request failed ({exc})") from exc


# --- Reject new mutations, wait admitted ones out, then stop the owned child ---
def drain_and_stop(canonical: str, in_flight_timeout: float = 30.0,
                    stop_timeout: float = 20.0) -> dict:
    """Drain a worker before signalling it; never strands an admitted write.

    Sets the worker drain flag (new unsafe requests get 409), polls the
    in-flight counter until admitted transactions finish, then stops only the
    positively tracked child. A drain that never empties refuses instead of
    killing live work; an unreachable worker refuses instead of blind kill.
    """
    with _hold_store_lock(canonical):
        return _drain_and_stop_locked(canonical, in_flight_timeout, stop_timeout)


def _drain_and_stop_locked(canonical: str, in_flight_timeout: float,
                           stop_timeout: float) -> dict:
    """Serialize lease admission through drain/stop; cancel every refused drain."""
    if canonical not in _CHILDREN:
        return {"stopped": False, "reason": "not-owned"}
    if live_handle_count(canonical):
        return {"stopped": False, "reason": "handles-live"}
    try:
        # No research DB reads: an admitted writer may hold an EXCLUSIVE lock.
        manifest = read_manifest(canonical)
    except ProjectStoreError as exc:
        return {"stopped": False, "reason": f"unopenable ({exc})"}
    recorded = _registry_row(canonical)
    if recorded is None:
        return {"stopped": False, "reason": "unknown-worker"}
    try:
        port = int(recorded["port"])
        epoch = str(recorded["worker_epoch"])
    except (TypeError, ValueError):
        return {"stopped": False, "reason": "bad-recorded-worker"}
    project_id = 0  # Drain is process-scoped and performs no Project lookup.
    base = f"http://127.0.0.1:{port}"
    prefix = f"/p/{manifest.project_uuid}/{epoch}"
    opener = token = None
    stopped = False
    url = f"{base}{prefix}/project/{project_id}/lifecycle/drain/"
    try:
        opener, token = _drain_session(base, prefix, project_id)
        started = _drain_post(
            opener, token, url,
            {"command": "start"})
        deadline = time.time() + in_flight_timeout
        pending = int(started.get("in_flight", 0))
        while pending > 0 and time.time() < deadline:
            time.sleep(0.5)
            try:
                with opener.open(url, timeout=_DRAIN_HTTP_TIMEOUT) as response:
                    status = json.loads(response.read().decode())
            except (OSError, http.client.HTTPException, ValueError):
                break
            if status.get("draining") is not True:
                break
            pending = int(status.get("in_flight", 0))
        if pending > 0:
            return {"stopped": False, "reason": "drain-timeout"}
        result = stop_worker(canonical, timeout=stop_timeout, _locked=True)
        stopped = result["stopped"]
        return result
    except ProjectStoreError as exc:
        return {"stopped": False, "reason": f"drain-unreachable ({exc})"}
    finally:
        if not stopped and opener is not None:
            try:
                _drain_post(opener, token, url, {"command": "cancel"})
            except ProjectStoreError:
                pass  # Unreachable workers require an explicit reopen/recovery.


# --- Bounded idle sweep: expire leases, then drain only handle-free workers ---
def sweep_idle(lease_ttl: float | None = None,
               worker_grace: float | None = None) -> dict:
    """Drop expired leases; drain handle-free workers idle past the grace bound.

    Bounds default from WP_HANDLE_TTL / WP_WORKER_GRACE so tests and the
    later launcher tune them without code changes. The grace clock is the
    worker row's last-use stamp (refreshed by every heartbeat/attach), not
    its start time. Unload/beacon loss lands here as expiry (a hint), never
    as authoritative durability: committed SQLite rows are untouched, only
    lease rows delete. Returns {expired, stopped, skipped}.
    """
    from datetime import UTC, datetime

    if lease_ttl is None:
        lease_ttl = _env_seconds("WP_HANDLE_TTL", _HANDLE_TTL_DEFAULT)
    if worker_grace is None:
        worker_grace = _env_seconds("WP_WORKER_GRACE", _WORKER_GRACE_DEFAULT)

    now = datetime.now(UTC)
    registry = init_app_registry(app_registry_path())
    expired = 0
    try:
        with sqlite3.connect(registry) as connection:
            rows = connection.execute(
                "SELECT handle_id, last_seen FROM leases").fetchall()
            stale = []
            for handle_id, last_seen in rows:
                try:
                    seen = datetime.fromisoformat(str(last_seen))
                except ValueError:
                    stale.append(handle_id)
                    continue
                if (now - seen).total_seconds() > lease_ttl:
                    stale.append(handle_id)
            for handle_id in stale:
                connection.execute(
                    "DELETE FROM leases WHERE handle_id = ?", (handle_id,))
            expired = len(stale)
            connection.commit()
            workers = connection.execute(
                "SELECT store_canonical, updated_at FROM workers").fetchall()
    except (sqlite3.Error, OSError):
        return {"expired": 0, "stopped": [], "skipped": []}
    stopped: list[str] = []
    skipped: list[str] = []
    for canonical, updated_at in workers:
        if live_handle_count(str(canonical)) > 0:
            continue
        try:
            touched = datetime.fromisoformat(str(updated_at))
        except ValueError:
            continue
        if (now - touched).total_seconds() < worker_grace:
            skipped.append(str(canonical))
            continue
        # ponytail: short drain wait inside sweeps; the launcher later owns
        # the patient shutdown path with a longer bound.
        try:
            result = drain_and_stop(str(canonical), in_flight_timeout=5.0)
        except (OSError, ValueError, ProjectStoreError):
            skipped.append(str(canonical))
            continue
        if result.get("stopped"):
            stopped.append(str(canonical))
        else:
            skipped.append(str(canonical))
    return {"expired": expired, "stopped": stopped, "skipped": skipped}


# --- Public lookup of the recorded worker for a canonical store ---
def find_worker(canonical: str) -> dict | None:
    """Return the recorded worker row for a store, if any (phase05 owns lifecycle)."""
    return _registry_row(canonical)


# --- Forget a registry worker row (tests/recovery; never deletes project files) ---
def forget_worker(canonical: str) -> None:
    """Drop a worker registry row; project files are untouched (phase05 owns lifecycle)."""
    registry = init_app_registry(app_registry_path())
    try:
        with sqlite3.connect(registry) as connection:
            connection.execute("DELETE FROM leases WHERE store_canonical = ?", (canonical,))
            connection.execute("DELETE FROM workers WHERE store_canonical = ?", (canonical,))
            connection.commit()
    except (sqlite3.Error, OSError):
        pass


# --- Start (or reuse) the app-owned worker for a validated store directory ---
def start_worker(directory: Path | str) -> dict:
    """Ensure one live worker serves this store; returns base URL parts.

    Reuse AND spawn take the operation lock, so neither can pass a restore.
    Concurrent first-openers elect exactly one child. A port already held by an
    unrelated listener is skipped with a fresh port, never signalled.
    """
    manifest = open_project_store(directory)
    canonical = str(manifest.location)
    with _hold_store_lock("uuid:" + str(manifest.project_uuid)):
        manifest = _register_location(manifest)
        canonical = str(manifest.location)
        with _hold_store_lock(canonical):
            # Revalidate after waiting: restore may have exchanged the inode.
            manifest = open_project_store(manifest.location)
            store_uuid = str(manifest.project_uuid)
            database_path = manifest.location / "project.sqlite3"
            project_id = store_project_id(database_path)
            reused = _reusable_worker(canonical, store_uuid, project_id)
            if reused is not None:
                if os.getenv("WP_LAUNCHER") == "1":
                    child = _CHILDREN.get(canonical)
                    if child is None or child.poll() is not None or child.pid != reused["pid"]:
                        raise ProjectStoreError("previous manager's worker is still draining; retry after it closes")
                return reused
            if os.getenv("WP_LAUNCHER") == "1":
                # Never spawn a duplicate behind a wedged child or crash recovery.
                # The inode closure gate is positive ownership, not registry liveness.
                from literature.project_store import store_file_lock

                previous = _CHILDREN.get(canonical)
                if previous is not None and previous.poll() is None:
                    raise ProjectStoreError("owned worker is busy/unreachable; retry, do not duplicate it")
                with store_file_lock(manifest, closed=True):
                    pass
            return _spawn_worker(canonical, store_uuid, database_path, project_id)


# --- Explicit UUID collisions never silently open as independent projects ---
def _register_location(manifest: StoreManifest) -> StoreManifest:
    from datetime import UTC, datetime

    registry = init_app_registry(app_registry_path())
    try:
        with sqlite3.connect(registry) as connection:
            places = connection.execute(
                "SELECT location FROM projects WHERE project_uuid=?",
                (str(manifest.project_uuid),),
            ).fetchall()
            for (place,) in places:
                if place == str(manifest.location):
                    continue
                try:
                    other = open_project_store(place)
                except ProjectStoreError:
                    # ponytail: missing old location needs explicit operator
                    # resolution, not silent adoption of an identity clone.
                    raise ProjectStoreError(f"same-UUID collision with registered {place}; use Save Copy") from None
                if other.db_identity != manifest.db_identity:
                    raise ProjectStoreError(f"same-UUID collision with {place}; use Save Copy, never merge")
                return other  # same physical DB alias: reuse its worker
            connection.execute(
                "INSERT OR REPLACE INTO projects VALUES (?, ?, ?)",
                (str(manifest.location), str(manifest.project_uuid), datetime.now(UTC).isoformat()),
            )
            connection.commit()
    except sqlite3.Error as exc:
        raise ProjectStoreError(f"cannot register store identity ({exc})") from exc
    return manifest


# --- Manager operations share acquire/stop locks; primitive gates own the inode ---
def operate_store(action: str, source: Path | str, destination: Path | str) -> StoreManifest:
    """Save Copy/Backup/Restore; never stop a worker implicitly to perform a write."""
    operations = {"copy": save_copy_store, "backup": backup_project_store,
                  "restore": restore_project_store}
    if action not in operations:
        raise ProjectStoreError("unknown store operation")
    try:
        paths = sorted({str(Path(p).expanduser().resolve()) for p in (source, destination)})
    except (OSError, RuntimeError) as exc:
        raise ProjectStoreError(f"invalid operation path ({exc})") from exc
    with ExitStack() as locks:
        for path in paths:
            locks.enter_context(_hold_store_lock(path))
        closed_paths = [paths[0], paths[1]] if action == "restore" and len(paths) == 2 else []
        if action == "backup":
            closed_paths = [str(Path(source).expanduser().resolve())]
        for path in closed_paths:
            # Registry errors are NOT zero handles / closed. Own Popen and
            # inode flock are independent positive checks, not PID guesses.
            registry = init_app_registry(app_registry_path())
            try:
                with sqlite3.connect(registry) as connection:
                    held = connection.execute("SELECT COUNT(*) FROM leases WHERE store_canonical=?", (path,)).fetchone()[0]
                    row = connection.execute("SELECT pid FROM workers WHERE store_canonical=?", (path,)).fetchone()
            except (sqlite3.Error, OSError) as exc:
                raise ProjectStoreError(f"cannot prove store closure ({exc})") from exc
            child = _CHILDREN.get(path)
            if held or row or (child is not None and child.poll() is None):
                raise ProjectStoreError("store must be CLOSED; Exit all tabs, then Close worker")
        return operations[action](Path(source).expanduser(), Path(destination).expanduser())


def close_store(directory: Path | str) -> dict:
    """Explicitly drain a positively owned, handle-free worker; no registry PID kills."""
    manifest = read_manifest(directory)
    canonical = str(manifest.location)
    child = _CHILDREN.get(canonical)
    if child is None:
        manifest = open_project_store(directory)
        # Physical lock probe is the closure proof for never-opened stores.
        from literature.project_store import store_file_lock

        if _registry_row(canonical) is not None and os.getenv("WP_LAUNCHER") != "1":
            raise ProjectStoreError("worker not owned by this manager; close it with its original manager")
        with _hold_store_lock(canonical), store_file_lock(manifest, closed=True):
            # Crash recovery only forgets runtime rows after positive physical
            # closure; no registry PID is adopted or signalled.
            if os.getenv("WP_LAUNCHER") == "1":
                forget_worker(canonical)
            return {"stopped": True, "reason": "already closed"}
    if child.poll() is not None:
        result = stop_worker(canonical)
        return {"stopped": result["reason"] == "already-exited", "reason": result["reason"]}
    if live_handle_count(canonical):
        raise ProjectStoreError("Exit all project tabs before closing the worker")
    return drain_and_stop(canonical)


# --- Recorded worker that still answers for this exact UUID+epoch, if any ---
def _reusable_worker(canonical: str, store_uuid: str, project_id: int) -> dict | None:
    recorded = _registry_row(canonical)
    if recorded is None:
        return None
    try:
        pid = int(recorded["pid"])
        port = int(recorded["port"])
    except (TypeError, ValueError):
        return None
    if pid and port and _pid_alive(pid) and _worker_answers(
        port, store_uuid, str(recorded["worker_epoch"]), project_id
    ):
        return {
            "base": f"http://127.0.0.1:{port}",
            "prefix": f"/p/{store_uuid}/{recorded['worker_epoch']}",
            "port": port,
            "epoch": str(recorded["worker_epoch"]),
            "pid": pid,
            "project_id": project_id,
            "reused": True,
        }
    return None


# --- True when anything already listens on a loopback port (never our kill) ---
def _port_occupied(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


# --- Spawn one runserver child under the store lock, retrying port collisions ---
def _spawn_worker(canonical: str, store_uuid: str, database_path: Path,
                  project_id: int) -> dict:
    epoch = f"e{uuid.uuid4().hex[:7]}"
    env = dict(os.environ)
    env["WP_ROLE"] = "worker"
    env["LOCAL_SURVEY_DB"] = str(database_path)
    env["WP_PROJECT_UUID"] = store_uuid
    env["WP_WORKER_EPOCH"] = epoch
    deadline = time.time() + _WORKER_READY_TIMEOUT
    last_error = "project worker did not answer in time"
    for _ in range(3):
        port = _free_port()
        supervised = os.getenv("WP_LAUNCHER") == "1"
        command = ([sys.executable, str(Path(settings.BASE_DIR) / "launch.py"), "--worker", "--port", str(port)]
                   if supervised else
                   [sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"])
        ownership_fd = None
        if supervised:
            identity = database_path.stat()
            ownership_fd = os.open(
                _store_lock_path(f"worker:{identity.st_dev}:{identity.st_ino}"),
                os.O_CREAT | os.O_RDWR, 0o600,
            )
            try:
                fcntl.flock(ownership_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(ownership_fd)
                raise ProjectStoreError("previous worker still owns this store; wait for its drain and retry") from exc
            env["WP_WORKER_LOCK_FD"] = str(ownership_fd)
        try:
            proc = subprocess.Popen(
                command,
                cwd=Path(settings.BASE_DIR),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(int(env["WP_OWNER_FD"]), ownership_fd) if supervised else (),
            )
        except OSError as exc:
            raise ProjectStoreError(f"cannot start project worker ({exc})") from exc
        finally:
            if ownership_fd is not None:
                # Child inherits the same locked open-file-description. Closing
                # our copy (NOT flock UNLOCK) preserves its lifetime ownership.
                os.close(ownership_fd)
        if supervised:
            _CHILDREN[canonical] = proc  # Own it even if readiness/registry fails.
            print(f"Started owned worker PID {proc.pid}: {canonical}", flush=True)
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if _worker_answers(port, store_uuid, epoch, project_id):
                break
            time.sleep(0.3)
        if proc.poll() is None and _worker_answers(port, store_uuid, epoch, project_id):
            assert proc.pid is not None
            _record_worker(canonical, proc.pid, port, epoch)
            # ponytail: reap a live owned predecessor first (a worker wedged
            # behind a lock fails reuse probes); only this Popen is signalled.
            previous = _CHILDREN.get(canonical)
            if previous is not None and previous is not proc and previous.poll() is None:
                try:
                    previous.terminate()
                    previous.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        previous.kill()
                    except OSError:
                        pass
                except OSError:
                    pass
            _CHILDREN[canonical] = proc
            return {
                "base": f"http://127.0.0.1:{port}",
                "prefix": f"/p/{store_uuid}/{epoch}",
                "port": port,
                "epoch": epoch,
                "pid": proc.pid,
                "project_id": project_id,
                "reused": False,
            }
        # ponytail: reap our own failed child, then decide: an occupied port
        # means an unrelated listener won the race, so retry elsewhere; a
        # free port means our child genuinely failed, so raise loudly.
        try:
            if proc.poll() is None:
                proc.terminate()
                if supervised:
                    proc.wait()  # Its graceful server joins any admitted work.
                else:
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            else:
                proc.wait(timeout=5)
        except OSError:
            pass
        if _port_occupied(port):
            last_error = f"port {port} held by another listener; retrying"
            continue
        raise ProjectStoreError("project worker exited before answering")
    raise ProjectStoreError(f"cannot start project worker ({last_error})")
