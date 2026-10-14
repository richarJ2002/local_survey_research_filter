#!/usr/bin/env python3

"""Create, validate and copy versioned single-project SQLite stores.

Load refuses format/schema drift instead of upgrading research data. Registry
state lives outside portable stores; copies bind snapshots to locked inodes.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   project_store.py

@Brief:  Create, validate and copy versioned single-project SQLite stores.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import ctypes
import fcntl
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

# Custom library Imports
from django.conf import settings

# ------------------------------------------------------------------------ #
# Frozen formats (WP01.02; UUID URL / cookie wiring lands in WP01.03)
# ------------------------------------------------------------------------ #

#: Only manifest version this code reads/writes. Newer refuses load;
#: older requires an explicit backup+upgrade (never auto-upgraded).
STORE_FORMAT_VERSION = 1

#: Manifest file inside every project directory.
MANIFEST_FILENAME = "project.json"

#: Authoritative research database member. Manifest ``database`` must
#: equal this exactly (no separators), so stored paths cannot traverse.
DATABASE_FILENAME = "project.sqlite3"

#: Members a store primitive may create. Share/backup whitelists reuse this.
EXPORTS_DIRNAME = "exports"
BACKUPS_DIRNAME = "backups"

#: Exclusive creation-claim file. Held only while create runs; a surviving
#: claim means an interrupted creation and needs explicit removal.
CLAIM_FILENAME = ".project-claim.lock"

#: Raw identity row inside each project.sqlite3 (stdlib-owned, outside the
#: Django ORM so legacy models/migrations stay untouched). Lets open()
#: prove the manifest UUID and the database file agree with each other.
IDENTITY_TABLE = "wp_store_identity"

#: Frozen private registry schema (XDG data dir, never inside packages).
REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  location TEXT PRIMARY KEY,
  project_uuid TEXT NOT NULL,
  last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
  store_canonical TEXT PRIMARY KEY,
  pid INTEGER NOT NULL,
  port INTEGER NOT NULL,
  worker_epoch TEXT NOT NULL,
  lease_owner TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
  handle_id TEXT PRIMARY KEY,
  store_canonical TEXT NOT NULL,
  project_uuid TEXT NOT NULL,
  worker_epoch TEXT NOT NULL,
  port INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  tab_token TEXT
);
""".strip()

#: Frozen application settings file schema (JSON, XDG data dir).
APP_SETTINGS_FILENAME = "settings.json"
APP_SETTINGS_DEFAULTS = {
    "format_version": 1,
    "workspace_roots": [],
    "recent_projects": [],
}

#: Worker subprocess timeout (seconds); expiry leaves partial files for inspect().
_WORKER_TIMEOUT = 300


# ------------------------------------------------------------------------ #
# Errors and values
# ------------------------------------------------------------------------ #


# --- Store failures are one explicit type; callers map to UI messages ---
class ProjectStoreError(Exception):
    """A project directory is missing, unrecognised, corrupt or refused."""


# --- Validated manifest; location is canonical, symlinks preserved ---
@dataclass(frozen=True)
class StoreManifest:
    """Represent a validated manifest and its resolved directory location.

    The UUID identifies the logical project, not its physical location.
    ``database`` is the fixed member name; ``created_at`` is stored text.
    ``db_identity`` is the optional (device, inode) pair of that member.
    ``recovery_location`` is returned by Restore, never stored in JSON.
    """

    format_version: int
    project_uuid: uuid.UUID
    database: str
    location: Path
    created_at: str
    # ponytail: (st_dev, st_ino) of project.sqlite3 once stat()ed; None for
    # manifest-only reads. Later phases dedup by (location, db_identity);
    # symlinked stores are allowed and resolve to the same key.
    db_identity: tuple[int, int] | None = None
    #: Returned only by Restore; not persisted in the package manifest.
    recovery_location: Path | None = None


# --- Read-only schema report; drift must never auto-apply ---
@dataclass(frozen=True)
class SchemaStatus:
    """Report sorted applied, missing and extra literature migration names.

    ``in_sync`` means both differences are empty; no migration is applied.
    """

    in_sync: bool
    applied: tuple[str, ...]
    missing: tuple[str, ...]
    extra: tuple[str, ...]


# ------------------------------------------------------------------------ #
# Private application state paths (XDG, outside project packages/source)
# ------------------------------------------------------------------------ #


# --- XDG dirs with home fallbacks; no I/O, so safe at import ---
def app_data_dir() -> Path:
    """Locate private registry/settings state without creating directories.

    Args:
        None

    Kwargs:
        None

    Returns:
        Path: App directory under XDG_DATA_HOME or ~/.local/share.
    """
    base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "local-survey-filter"


# --- Companion cache dir for disposable assets (Plotly, thumbnails) ---
def app_cache_dir() -> Path:
    """Locate disposable app caches without creating directories.

    Args:
        None

    Kwargs:
        None

    Returns:
        Path: App directory under XDG_CACHE_HOME or ~/.cache.
    """
    base = os.getenv("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "local-survey-filter"


# --- Concrete private-state members frozen for later phases ---
def app_registry_path() -> Path:
    """Locate the runtime registry outside research project folders.

    Args:
        None

    Kwargs:
        None

    Returns:
        Path: Private registry.sqlite3 path, not a research database.
    """
    return app_data_dir() / "registry.sqlite3"


# --- Settings live beside the registry, never inside project folders ---
def app_settings_path() -> Path:
    """Locate workspace preferences beside the private runtime registry.

    Args:
        None

    Kwargs:
        None

    Returns:
        Path: Private settings.json path, not a project package member.
    """
    return app_data_dir() / APP_SETTINGS_FILENAME


# --- Create registry tables only; research rows must never land here ---
def init_app_registry(path: Path | str) -> Path:
    """Create registry tables and add tab_token to older lease tables.

    Args:
        path (Path | str): Registry database file to initialize.

    Kwargs:
        None

    Returns:
        Path: Supplied registry path, without canonicalization.

    Raises:
        ProjectStoreError: If the parent or registry cannot be initialized.

    Side Effects:
        Create parent directories and commit runtime tables/lease upgrades.
        Research records and settings files are not written.
    """
    target = Path(path)
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectStoreError(
            f"{target}: cannot create registry dir ({exc})") from exc
    try:
        with sqlite3.connect(target) as connection:
            connection.executescript(REGISTRY_SCHEMA)
            # ponytail: pre-tab-token registries gain the column in place;
            # research stores and settings files are untouched.
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(leases)")}
            if "tab_token" not in columns:
                connection.execute(
                    "ALTER TABLE leases ADD COLUMN tab_token TEXT")
            connection.commit()
    except (sqlite3.Error, OSError) as exc:
        raise ProjectStoreError(
            f"{target}: cannot init registry ({exc})") from exc
    return target


# ------------------------------------------------------------------------ #
# Read-only database access (quoting-safe, always wrapped)
# ------------------------------------------------------------------------ #


# --- Single URI builder; resolve().as_uri() keeps ?/#/% names intact ---
def _connect_ro(database_path: Path | str) -> sqlite3.Connection:
    """Open the resolved SQLite file through a quoting-safe read-only URI.

    Args:
        database_path (Path | str): Existing database file to open.

    Kwargs:
        None

    Returns:
        sqlite3.Connection: Read-only connection the caller must close.

    Raises:
        ProjectStoreError: If the path is unusable or SQLite cannot open it.
    """
    try:
        uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    except (OSError, ValueError) as exc:
        raise ProjectStoreError(
            f"{database_path}: unusable database path ({exc})") from exc
    try:
        return sqlite3.connect(uri, uri=True, timeout=30)
    except (sqlite3.Error, OSError) as exc:
        raise ProjectStoreError(
            f"{database_path}: cannot open database ({exc})") from exc


# ------------------------------------------------------------------------ #
# Manifest helpers
# ------------------------------------------------------------------------ #


# --- Parse + validate user-supplied JSON before touching any database ---
def _parse_manifest(data: object, location: Path) -> StoreManifest:
    """Validate manifest fields without reading or creating a database.

    Args:
        data (object): Decoded JSON with version, project UUID and database.
        location (Path): Canonical store directory to attach to the result.

    Kwargs:
        None

    Returns:
        StoreManifest: Validated fields with no database inode attached;
            created_at is stringified and defaults to an empty string.

    Raises:
        ProjectStoreError: If fields are missing/invalid, the database name
            differs, or the version needs an explicit upgrade/refuses load.
    """
    if not isinstance(data, dict):
        raise ProjectStoreError(f"{location}: manifest is not a JSON object")
    try:
        version = data["format_version"]
        raw_uuid = data["project_uuid"]
        database = data["database"]
    except KeyError as exc:
        raise ProjectStoreError(f"{location}: manifest missing {exc}") from exc
    # ponytail: exact int type, so True/1.0/"1" can never pass as version 1.
    if type(version) is not int or version != STORE_FORMAT_VERSION:
        if type(version) is int and version > STORE_FORMAT_VERSION:
            raise ProjectStoreError(
                f"{location}: manifest version {version} is newer than supported "
                f"{STORE_FORMAT_VERSION}; refusing load"
            )
        raise ProjectStoreError(
            f"{location}: manifest version {version!r} requires an explicit "
            "backup-before-upgrade; refusing load"
        )
    try:
        parsed_uuid = uuid.UUID(str(raw_uuid))
    except (ValueError, AttributeError) as exc:
        raise ProjectStoreError(
            f"{location}: bad project_uuid {raw_uuid!r}") from exc
    # ponytail: exact-name match, not a path join, so "../x" can never escape.
    if database != DATABASE_FILENAME:
        raise ProjectStoreError(
            f"{location}: manifest database {database!r} must be {DATABASE_FILENAME!r}"
        )
    created = data.get("created_at", "")
    return StoreManifest(
        format_version=version,
        project_uuid=parsed_uuid,
        database=DATABASE_FILENAME,
        location=location,
        created_at=str(created),
    )


# --- Read-only manifest load; Load path never creates anything ---
def read_manifest(directory: Path | str) -> StoreManifest:
    """Read and validate an existing manifest without creating store files.

    Args:
        directory (Path | str): Store folder; directory symlinks are allowed.

    Kwargs:
        None

    Returns:
        StoreManifest: Parsed manifest with a resolved location and no inode.

    Raises:
        ProjectStoreError: If the directory/manifest is missing, unreadable,
            corrupt, or has unsupported identity, database or version fields.
    """
    raw = Path(directory)
    if not raw.is_dir():
        raise ProjectStoreError(f"{raw}: not a project directory")
    # ponytail: canonical target for later dedup; symlinks stay allowed.
    location = raw.resolve()
    manifest_path = location / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ProjectStoreError(
            f"{location}: unrecognised store, no {MANIFEST_FILENAME}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectStoreError(
            f"{location}: corrupt manifest ({exc})") from exc
    return _parse_manifest(data, location)


# ------------------------------------------------------------------------ #
# Worker-scoped database bootstrap (subprocess keeps the default alias fixed)
# ------------------------------------------------------------------------ #


# --- Migrate runs in a child whose default DB is already the target ---
def _run_migrate(database_path: Path) -> None:
    """Migrate a deliberate target with dotenv and worker binding disabled.

    Args:
        database_path (Path): Target DB path, made absolute for dispatch.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If spawning, migration or its timeout fails.

    Side Effects:
        Run installed migrations on the target without rebinding the parent.
    """
    base = Path(settings.BASE_DIR)
    # Bootstrap needs neither credentials nor an inherited worker identity.
    env = dict(os.environ, LOCAL_SURVEY_DB=str(database_path.absolute()),
               PYTHON_DOTENV_DISABLED="1", WP_ROLE="developer",
               WP_PROJECT_UUID="", WP_WORKER_EPOCH="")
    try:
        completed = subprocess.run(
            [sys.executable, "launch.py", "django", "migrate", "--noinput"],
            capture_output=True,
            check=False,
            text=True,
            timeout=_WORKER_TIMEOUT,
            cwd=base,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectStoreError(
            f"migrate failed for {database_path}: {exc}") from exc
    if completed.returncode != 0:
        raise ProjectStoreError(
            f"migrate failed for {database_path}: {completed.stderr[-2000:]}"
        )


# ------------------------------------------------------------------------ #
# Module setup
# ------------------------------------------------------------------------ #

# --- Single Project row via the worker's own fixed default alias ---
_CREATE_PROJECT_SCRIPT = """
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from literature.models import Project

project = Project.objects.create(name=sys.argv[1])
project.ensure_default_stages()
print(f"OK project={project.pk}", flush=True)
"""


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Child process owns the write; the parent never rebinds its own DB ---
def _run_create_project(database_path: Path, name: str) -> None:
    """Create one Project and its default stages in a fixed-target child.

    Args:
        database_path (Path): Database passed as the child's default target.
        name (str): Name for the new Project row.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If spawning times out/fails, the child fails, or
            its output lacks the project-creation success marker.

    Side Effects:
        Run Django bootstrap and write project/stage rows in the child;
        the parent's database alias is never rebound.
    """
    base = Path(settings.BASE_DIR)
    env = dict(os.environ, LOCAL_SURVEY_DB=str(database_path))
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _CREATE_PROJECT_SCRIPT, name],
            capture_output=True,
            check=False,
            text=True,
            timeout=_WORKER_TIMEOUT,
            cwd=base,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectStoreError(
            f"project bootstrap failed for {database_path}: {exc}"
        ) from exc
    if completed.returncode != 0 or "OK project=" not in completed.stdout:
        raise ProjectStoreError(
            f"project bootstrap failed for {database_path}: {completed.stderr[-2000:]}"
        )


# --- Identity row written with stdlib so Django migrations stay untouched ---
def _write_identity(database_path: Path, project_uuid: uuid.UUID) -> None:
    """Create the stdlib-owned identity table and insert the store identity.

    Args:
        database_path (Path): SQLite file receiving the identity row.
        project_uuid (uuid.UUID): Logical project UUID to store as text.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If SQLite cannot create or insert the identity.

    Side Effects:
        Open a writable connection and commit the UUID/current format row.
    """
    try:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {IDENTITY_TABLE} "
                "(project_uuid TEXT PRIMARY KEY, format_version INTEGER NOT NULL)"
            )
            connection.execute(
                f"INSERT INTO {IDENTITY_TABLE} (project_uuid, format_version) VALUES (?, ?)",
                (str(project_uuid), STORE_FORMAT_VERSION),
            )
            connection.commit()
    except (sqlite3.Error, OSError) as exc:
        raise ProjectStoreError(
            f"{database_path}: cannot write store identity ({exc})"
        ) from exc


# ------------------------------------------------------------------------ #
# Exclusive creation claim (no check-then-act overwrite window)
# ------------------------------------------------------------------------ #


# --- Explain who holds a surviving claim and how to recover explicitly ---
def _claim_holder_message(claim: Path, dest: Path) -> str:
    """Describe a surviving claim without deleting it or signalling its PID.

    Args:
        claim (Path): Claim JSON file whose pid is probed with signal zero.
        dest (Path): Destination named in the recovery message.

    Kwargs:
        None

    Returns:
        str: Live-holder or stale/unreadable claim message with removal advice.
    """
    try:
        pid = json.loads(claim.read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError, AttributeError):
        pid = None
    alive = False
    if type(pid) is int:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        except OSError:
            alive = False
    state = f"held by live pid {pid}" if alive else "stale or unreadable"
    return (
        f"{dest}: creation claim {state}; remove {claim} explicitly if stale "
        "(claims are never auto-deleted)"
    )


# --- Atomic mkdir plus O_EXCL claim; losers refuse, never overwrite ---
def _exclusive_claim(destination: Path) -> Path:
    """Claim an absent or empty directory without overwriting existing files.

    Args:
        destination (Path): Store directory whose parent must already exist.

    Kwargs:
        None

    Returns:
        Path: Exclusive claim file; the caller owns its eventual removal.

    Raises:
        ProjectStoreError: If the target is populated/not a directory, a
            claim exists, or directory/claim operations fail before writing.
        OSError: If writing the newly opened claim fails.

    Side Effects:
        Create the directory if absent and write PID/time to an O_EXCL file.
        Failed creation may leave a claim requiring explicit recovery.
    """
    dest = Path(destination)
    try:
        dest.mkdir(mode=0o700, parents=False)
    except FileExistsError:
        pass
    except FileNotFoundError as exc:
        raise ProjectStoreError(
            f"{dest}: parent directory is missing") from exc
    except NotADirectoryError as exc:
        raise ProjectStoreError(
            f"{dest}: parent path is not a directory") from exc
    except OSError as exc:
        raise ProjectStoreError(
            f"{dest}: cannot create directory ({exc})") from exc
    if not dest.is_dir():
        raise ProjectStoreError(
            f"{dest}: target exists and is not a directory")
    try:
        populated = any(dest.iterdir())
    except OSError as exc:
        raise ProjectStoreError(
            f"{dest}: cannot list directory ({exc})") from exc
    if populated:
        raise ProjectStoreError(
            f"{dest}: refusing to overwrite a populated directory")
    claim = dest / CLAIM_FILENAME
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ProjectStoreError(_claim_holder_message(claim, dest)) from None
    except OSError as exc:
        raise ProjectStoreError(
            f"{dest}: cannot claim directory ({exc})") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
        )
    return claim


# ------------------------------------------------------------------------ #
# Creation, inspection, open validation
# ------------------------------------------------------------------------ #


# --- Explicit creation; partial results are left for inspect(), never wiped ---
def create_project_store(destination: Path | str, name: str) -> StoreManifest:
    """Create a manifest, migrated database and one project's default stages.

    Args:
        destination (Path | str): Absent/empty store directory with an
            existing parent; populated targets are refused.
        name (str): Name for the store's single Project row.

    Kwargs:
        None

    Returns:
        StoreManifest: New UUID, canonical location and (device, inode) pair.

    Raises:
        ProjectStoreError: If claiming, file creation, migration, identity
            insertion, project bootstrap or database stat fails.

    Side Effects:
        Create exports/backups folders and authoritative store files. Remove
        this call's claim in finally; retain partial files on failure.
    """
    dest = Path(destination)
    claim = _exclusive_claim(dest)
    try:
        # ponytail: post-claim recheck closes the check-then-act window.
        try:
            raced = [entry for entry in dest.iterdir() if entry != claim]
        except OSError as exc:
            raise ProjectStoreError(
                f"{dest}: cannot list directory ({exc})") from exc
        if raced:
            raise ProjectStoreError(
                f"{dest}: directory populated during claim; refusing load of {raced[0].name}"
            )
        try:
            (dest / EXPORTS_DIRNAME).mkdir(mode=0o700, exist_ok=True)
            (dest / BACKUPS_DIRNAME).mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise ProjectStoreError(
                f"{dest}: cannot create store dirs ({exc})") from exc

        project_uuid = uuid.uuid4()
        created_at = datetime.now(UTC).isoformat()
        manifest_doc = {
            "format_version": STORE_FORMAT_VERSION,
            "project_uuid": str(project_uuid),
            "database": DATABASE_FILENAME,
            "created_at": created_at,
        }
        # ponytail: tmp+replace survives a mid-write crash; cleanup stays explicit.
        tmp_path = dest / f".{MANIFEST_FILENAME}.tmp-{os.getpid()}"
        try:
            tmp_path.write_text(json.dumps(
                manifest_doc, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp_path, dest / MANIFEST_FILENAME)
        except OSError as exc:
            raise ProjectStoreError(
                f"{dest}: cannot write manifest ({exc})") from exc

        database_path = dest / DATABASE_FILENAME
        _run_migrate(database_path)
        _write_identity(database_path, project_uuid)
        _run_create_project(database_path, name)
        try:
            file_stat = database_path.stat()
        except OSError as exc:
            raise ProjectStoreError(
                f"{dest}: cannot stat new database ({exc})") from exc
        return StoreManifest(
            format_version=STORE_FORMAT_VERSION,
            project_uuid=project_uuid,
            database=DATABASE_FILENAME,
            location=dest.resolve(),
            created_at=created_at,
            db_identity=(file_stat.st_dev, file_stat.st_ino),
        )
    finally:
        # ponytail: our claim always goes away; partial files stay for inspect().
        try:
            claim.unlink()
        except OSError:
            pass


# --- Read-only health report; complete means open() would succeed ---
def inspect_project_store(directory: Path | str) -> dict[str, object]:
    """Report store health without writing to the inspected directory.

    Args:
        directory (Path | str): Candidate project directory to inspect.

    Kwargs:
        None

    Returns:
        dict[str, object]: Location and complete flag, then available UUID,
            format_version, stringified db_identity, identity_match,
            project_count and SchemaStatus under schema. Refusals populate
            manifest_error or database_error and leave complete false.

    Side Effects:
        Read manifest/database; the Load gate may bootstrap a temporary
        trusted schema, never migrate the inspected database.
    """
    raw = Path(directory)
    report: dict[str, object] = {"location": str(raw), "complete": False}
    try:
        manifest = read_manifest(raw)
    except ProjectStoreError as exc:
        report["manifest_error"] = str(exc)
        return report
    report["location"] = str(manifest.location)
    report["project_uuid"] = str(manifest.project_uuid)
    report["format_version"] = manifest.format_version
    try:
        db_path = manifest.location / manifest.database
        manifest_with_id = _attach_db_identity(manifest)
        report["db_identity"] = [str(part)
                                 for part in manifest_with_id.db_identity or ()]
        _check_identity(db_path, manifest.project_uuid)
        report["identity_match"] = True
        report["project_count"] = _project_count(db_path)
        report["schema"] = schema_status(db_path)
    except ProjectStoreError as exc:
        report["database_error"] = str(exc)
        return report
    try:
        open_project_store(raw)
    except ProjectStoreError as exc:
        report["database_error"] = str(exc)
        return report
    report["complete"] = True
    return report


# --- Stat the DB file for the (dev, ino) dedup key used by later phases ---
def _attach_db_identity(manifest: StoreManifest) -> StoreManifest:
    """Attach the database member's physical (device, inode) identity.

    Args:
        manifest (StoreManifest): Validated manifest with a store location.

    Kwargs:
        None

    Returns:
        StoreManifest: Frozen replacement with db_identity populated.

    Raises:
        ProjectStoreError: If stat fails or the member is not a regular file.
    """
    try:
        file_stat = (manifest.location / manifest.database).stat()
    except OSError as exc:
        raise ProjectStoreError(
            f"{manifest.location}: cannot stat database ({exc})"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ProjectStoreError(
            f"{manifest.location}: database is not a regular file")
    return replace(manifest, db_identity=(file_stat.st_dev, file_stat.st_ino))


# --- Identity table must hold exactly one row agreeing on UUID + version ---
def _check_identity(database_path: Path, expected: uuid.UUID) -> None:
    """Require exactly one database identity matching UUID and format version.

    Args:
        database_path (Path): Store database opened read-only for validation.
        expected (uuid.UUID): UUID from the manifest to compare as text.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If opening/reading fails, the identity row count
            is not one, the UUID differs, or the version is unsupported.
    """
    with _connect_ro(database_path) as connection:
        try:
            rows = connection.execute(
                f"SELECT project_uuid, format_version FROM {IDENTITY_TABLE}"
            ).fetchall()
        except sqlite3.Error as exc:
            raise ProjectStoreError(
                f"{database_path}: identity table unreadable ({exc})"
            ) from exc
    if not rows:
        raise ProjectStoreError(
            f"{database_path}: no store identity (not created here?)")
    if len(rows) != 1:
        raise ProjectStoreError(
            f"{database_path}: {len(rows)} identity rows; refusing ambiguous store"
        )
    stored_uuid, stored_version = rows[0]
    if stored_uuid != str(expected):
        raise ProjectStoreError(
            f"{database_path}: manifest UUID does not match the database identity"
        )
    if stored_version != STORE_FORMAT_VERSION:
        raise ProjectStoreError(
            f"{database_path}: identity version {stored_version!r} needs an explicit "
            "backup-before-upgrade; refusing load"
        )


# --- Exactly one logical project per store; read-only count ---
def _project_count(database_path: Path) -> int:
    """Count logical projects through a read-only database connection.

    Args:
        database_path (Path): Store database containing literature_project.

    Kwargs:
        None

    Returns:
        int: Number of Project rows, including zero or multiple rows.

    Raises:
        ProjectStoreError: If opening the file or reading the table fails.
    """
    with _connect_ro(database_path) as connection:
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM literature_project").fetchone()
        except sqlite3.Error as exc:
            raise ProjectStoreError(
                f"{database_path}: project table unreadable ({exc})"
            ) from exc
    return int(row[0])


# --- Read-only FK sweep plus whole-file integrity check ---
def _check_integrity(database_path: Path) -> None:
    """Require SQLite integrity and an empty foreign-key violation report.

    Args:
        database_path (Path): Existing database to inspect read-only.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If opening/checks fail, foreign keys are violated,
            or integrity_check does not return ok.
    """
    with _connect_ro(database_path) as connection:
        try:
            violations = list(connection.execute("PRAGMA foreign_key_check"))
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            raise ProjectStoreError(
                f"{database_path}: integrity check failed ({exc})"
            ) from exc
    if violations:
        raise ProjectStoreError(
            f"{database_path}: {len(violations)} foreign-key violation(s)"
        )
    if integrity is None or integrity[0] != "ok":
        raise ProjectStoreError(
            f"{database_path}: corrupt database ({integrity})")


# --- Load gate: validates everything, creates nothing, migrates nothing ---
def open_project_store(directory: Path | str) -> StoreManifest:
    """Validate a store for Load without upgrading or writing its database.

    Args:
        directory (Path | str): Existing portable store directory.

    Kwargs:
        None

    Returns:
        StoreManifest: Canonical location and attached database inode after
            structure, identity, single-project, integrity and history checks.

    Raises:
        ProjectStoreError: If manifest/member validation fails, the schema
            differs, project count is not one, or integrity/history drifts.
        sqlite3.Error: If disabling trusted_schema on the candidate fails.

    Side Effects:
        The cached trusted-schema reference may migrate a temporary database.
        The candidate itself is opened only read-only; drift refuses Load.
    """
    manifest = read_manifest(directory)
    database_path = manifest.location / manifest.database
    if not database_path.exists():
        raise ProjectStoreError(
            f"{manifest.location}: database member missing, refusing load"
        )
    manifest = _attach_db_identity(manifest)
    with closing(_connect_ro(database_path)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        if _schema(connection) != _trusted_schema():
            raise ProjectStoreError(
                f"{manifest.location}: unsupported database structure")
    _check_identity(database_path, manifest.project_uuid)
    count = _project_count(database_path)
    if count == 0:
        raise ProjectStoreError(
            f"{manifest.location}: store holds no project (incomplete?)"
        )
    if count > 1:
        raise ProjectStoreError(
            f"{manifest.location}: store holds {count} projects; same-UUID copies "
            "are a collision, never a merge"
        )
    _check_integrity(database_path)
    status = schema_status(database_path)
    if not status.in_sync:
        raise ProjectStoreError(
            f"{manifest.location}: schema drift missing={list(status.missing)} "
            f"extra={list(status.extra)}; explicit backup-before-upgrade "
            "required, refusing load"
        )
    return manifest


# ------------------------------------------------------------------------ #
# Controlled schema checks and WAL-safe backup (no auto-upgrade anywhere)
# ------------------------------------------------------------------------ #


# --- Applied names must equal on-disk names exactly, both directions ---
def schema_status(database_path: Path | str) -> SchemaStatus:
    """Compare stored literature migration names with all on-disk migrations.

    Args:
        database_path (Path | str): Existing database with migration history.

    Kwargs:
        None

    Returns:
        SchemaStatus: Sorted applied names and missing/extra differences;
            in_sync is true only when both differences are empty.

    Raises:
        ProjectStoreError: If opening/history reading or migration listing
            fails. Missing/extra migrations are reported, not applied.
    """
    db = Path(database_path)
    with _connect_ro(db) as connection:
        try:
            applied = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM django_migrations WHERE app = 'literature'"
                )
            }
        except sqlite3.Error as exc:
            raise ProjectStoreError(
                f"{db}: migration history unreadable ({exc})") from exc
    try:
        disk = {
            path.stem
            for path in (Path(settings.BASE_DIR) / "literature" / "migrations").glob(
                "[0-9]*.py"
            )
        }
    except OSError as exc:
        raise ProjectStoreError(
            f"{db}: cannot list migrations ({exc})") from exc
    # ponytail: literature-app only; contrib apps ship with Django itself.
    missing = tuple(sorted(disk - applied))
    extra = tuple(sorted(applied - disk))
    return SchemaStatus(
        in_sync=not missing and not extra,
        applied=tuple(sorted(applied)),
        missing=missing,
        extra=extra,
    )


# ------------------------------------------------------------------------ #
# WP01.08 durable copies: save copy (new UUID), backup/restore (same UUID)
# ------------------------------------------------------------------------ #

# ponytail: only authoritative members travel; generated exports are omitted
# rather than trusting arbitrary files (or links) placed in exports/.
PACKAGE_MEMBERS = frozenset({MANIFEST_FILENAME, DATABASE_FILENAME})


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Flush one file's bytes so a crash cannot leave a truncated member ---
def _fsync_file(path: Path) -> None:
    """Flush an existing file's bytes to durable storage.

    Args:
        path (Path): File opened read-only to fsync its descriptor.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If opening or flushing the file fails.
    """
    try:
        with open(path, "rb") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ProjectStoreError(f"{path}: cannot fsync ({exc})") from exc


# --- Flush a directory entry so a rename cannot vanish on crash ---
def _fsync_dir(directory: Path) -> None:
    """Flush directory entries and close the descriptor after the attempt.

    Args:
        directory (Path): Directory whose entries must survive a crash.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If opening or flushing the directory fails.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        raise ProjectStoreError(
            f"{directory}: cannot fsync dir ({exc})") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise ProjectStoreError(
            f"{directory}: cannot fsync dir ({exc})") from exc
    finally:
        os.close(fd)


# --- Per-table row counts for a store DB; used to prove nothing was lost ---
def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Count rows in every SQLite table on the supplied connection.

    Args:
        connection (sqlite3.Connection): Open connection/snapshot to inspect.

    Kwargs:
        None

    Returns:
        dict[str, int]: Table names mapped to counts, including SQLite tables.
    """
    counts = {}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        escaped = name.replace('"', '""')
        counts[name] = connection.execute(
            f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
    return counts


# --- WAL-safe copy of one store DB; source opened read-only, never checkpointed ---
def _backup_db_file(
    source_db: Path | sqlite3.Connection, dest_db: Path, *, manifest: StoreManifest | None = None
) -> None:
    """Copy one committed SQLite snapshot and verify destination row counts.

    Args:
        source_db (Path | sqlite3.Connection): Read-only source path or
            caller-owned connection, retained without closing when supplied.
        dest_db (Path): Writable destination database receiving the backup.

    Kwargs:
        manifest (StoreManifest | None): Inode/UUID gate checked around the
            backup. Defaults to None, which skips these package checks.

    Returns:
        None

    Raises:
        ProjectStoreError: If backup exceeds its 30-second progress deadline,
            counts differ, member/identity checks fail, or copy/fsync fails.

    Side Effects:
        Start a source read transaction if needed; a caller-owned connection's
        transaction remains open. Write and fsync dest_db.
        Retain partial output on failure, never checkpoint the source WAL.
    """
    try:
        with ExitStack() as resources:
            if isinstance(source_db, sqlite3.Connection):
                src = source_db
            else:
                src = resources.enter_context(closing(_connect_ro(source_db)))
            dst = resources.enter_context(closing(sqlite3.connect(dest_db)))
            # Pin counts and backup to ONE committed read snapshot, not a
            # separate count before a concurrent writer commits.
            if not src.in_transaction:
                src.execute("BEGIN")
            if manifest is not None:
                _check_member(manifest)
                _require_snapshot_identity(src, manifest.project_uuid)
            expected = _table_counts(src)
            deadline = time.monotonic() + 30

            def progress(status: int, remaining: int, total: int) -> None:
                """Abort backup progress after the shared snapshot deadline.

                Args:
                    status (int): SQLite backup status code, unused here.
                    remaining (int): Uncopied pages, unused by the time gate.
                    total (int): Total source pages, unused by the time gate.

                Kwargs:
                    None

                Returns:
                    None

                Raises:
                    ProjectStoreError: If the 30-second deadline has elapsed.
                """
                if time.monotonic() > deadline:
                    raise ProjectStoreError(
                        "snapshot timed out; staging retained")

            src.backup(dst, pages=256, progress=progress, sleep=0.05)
            if _table_counts(dst) != expected:
                raise ProjectStoreError("snapshot row counts disagree")
            if manifest is not None:
                _check_member(manifest)
    except (sqlite3.Error, OSError) as exc:
        raise ProjectStoreError(
            f"{source_db}: backup copy failed ({exc})") from exc
    _fsync_file(dest_db)


def _schema(connection: sqlite3.Connection) -> tuple:
    """Read every SQLite schema object, including internal sequence/index rows.

    Args:
        connection (sqlite3.Connection): Open database connection to inspect.

    Kwargs:
        None

    Returns:
        tuple: Rows of (type, name, tbl_name, sql), ordered by type and name.

    Raises:
        ProjectStoreError: If sqlite_master cannot be read.
    """
    # Compare EVERY object, including the exact sqlite_sequence/autoindexes
    # generated by our migrations. No prefix or LIKE exception can hide objects.
    try:
        return tuple(connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        ))
    except sqlite3.Error as exc:
        raise ProjectStoreError(
            f"unreadable database structure ({exc})") from exc


@lru_cache(maxsize=1)
def _trusted_schema() -> tuple:
    """Build and cache a reference from trusted source-tree migrations.

    Args:
        None

    Kwargs:
        None

    Returns:
        tuple: (type, name, tbl_name, sql) rows ordered by type/name.

    Raises:
        ProjectStoreError: If migration, identity creation or schema reading
            fails for the temporary database.

    Side Effects:
        Migrate a temporary database, add identity, then remove its directory.
        Package-provided code is never executed to construct the reference.
    """
    with tempfile.TemporaryDirectory(prefix="wp-schema-") as tmp:
        db = Path(tmp) / DATABASE_FILENAME
        _run_migrate(db)
        _write_identity(db, uuid.uuid4())
        with closing(_connect_ro(db)) as connection:
            return _schema(connection)


def validate_package(directory: Path | str, *, strict_members: bool = True) -> StoreManifest:
    """Validate package membership, trusted structure and owned research rows.

    Args:
        directory (Path | str): Existing portable package or store directory.

    Kwargs:
        strict_members (bool): Require exactly project.json/project.sqlite3.
            Defaults to True; False permits additional directory members.

    Returns:
        StoreManifest: Fully Load-validated manifest with physical identity.

    Raises:
        ProjectStoreError: If manifest keys/members, schema, paper ownership,
            identity, project count, integrity or migration history is invalid.

    Side Effects:
        Read candidate files only; first trusted-schema validation may migrate
        a separate temporary reference database.
    """
    try:
        manifest = read_manifest(directory)
        doc = json.loads(
            (manifest.location / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        if set(doc) != {"format_version", "project_uuid", "database", "created_at"}:
            raise ProjectStoreError("unknown manifest members")
        if strict_members and {p.name for p in manifest.location.iterdir()} != PACKAGE_MEMBERS:
            raise ProjectStoreError(
                "unknown package members (only project.json/project.sqlite3 allowed)")
        with closing(_connect_ro(manifest.location / DATABASE_FILENAME)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            if _schema(connection) != _trusted_schema():
                raise ProjectStoreError(
                    "unsupported database structure (tables/indexes/views/triggers)")
            orphan = connection.execute(
                "SELECT id FROM literature_paper WHERE id NOT IN ("
                "SELECT paper_id FROM literature_discovery UNION SELECT paper_id FROM literature_screeningdecision "
                "UNION SELECT paper_id FROM literature_screeningrevision UNION SELECT paper_id FROM literature_paperkeywordassignment "
                "UNION SELECT paper_id FROM literature_surveyentry WHERE paper_id IS NOT NULL) LIMIT 1"
            ).fetchone()
            if orphan:
                raise ProjectStoreError(
                    "unowned paper rows; explicit conversion/ownership resolution required")
        return open_project_store(directory)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise ProjectStoreError(
            f"{directory}: invalid package ({exc})") from exc


@contextmanager
def store_file_lock(manifest: StoreManifest, *, closed: bool) -> Iterator[BinaryIO]:
    """Positive inode ownership gate, shared by workers across XDG registries.

    Every bound worker holds SH for its lifetime. EX proves no participating
    worker has this file open; a missing registry row is never this proof.
    Unmanaged SQLite clients do not implement this application lock protocol.

    Args:
        manifest (StoreManifest): Store with the expected database inode.

    Kwargs:
        closed (bool): Required; True requests an exclusive lock proving no
            participating worker holds a shared lock, False requests shared.

    Returns:
        contextlib.AbstractContextManager: Yield a read-only BinaryIO handle
            owning the nonblocking flock until the context closes the file.

    Raises:
        ProjectStoreError: If opening/locking fails or the inode has changed.
    """
    try:
        with open(manifest.location / DATABASE_FILENAME, "rb") as handle:
            fcntl.flock(
                handle, (fcntl.LOCK_EX if closed else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            file_stat = os.fstat(handle.fileno())
            if manifest.db_identity != (file_stat.st_dev, file_stat.st_ino):
                raise ProjectStoreError(
                    "store file moved; re-open before operating")
            yield handle
    except OSError as exc:
        raise ProjectStoreError(
            f"store must be CLOSED / ownership lock unavailable ({exc})") from exc


def _check_member(manifest: StoreManifest) -> None:
    """Refuse a changed logical member even when the old inode remains locked.

    Args:
        manifest (StoreManifest): Expected location, inode, UUID and creation
            timestamp captured before the operation.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If manifest/member validation fails or inode,
            UUID or created_at no longer matches.
    """
    current = read_manifest(manifest.location)
    current = _attach_db_identity(current)
    if (current.db_identity != manifest.db_identity
            or current.project_uuid != manifest.project_uuid
            or current.created_at != manifest.created_at):
        raise ProjectStoreError(
            "locked store member changed; refusing publication")


def _require_snapshot_identity(connection: sqlite3.Connection, expected: uuid.UUID) -> None:
    """Require the snapshot's single identity row to match UUID and version.

    Args:
        connection (sqlite3.Connection): Open snapshot transaction to inspect.
        expected (uuid.UUID): Original locked store UUID, before relabelling.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If row count, UUID or format version disagrees.
        sqlite3.Error: If the identity query fails; callers wrap copy failures.
    """
    rows = connection.execute(
        f"SELECT project_uuid, format_version FROM {IDENTITY_TABLE}"
    ).fetchall()
    if rows != [(str(expected), STORE_FORMAT_VERSION)]:
        raise ProjectStoreError(
            "snapshot original UUID/version disagrees with locked store")


@contextmanager
def _locked_snapshot(manifest: StoreManifest, handle: BinaryIO) -> Iterator[sqlite3.Connection]:
    """Pin a read transaction to the locked inode, not its logical member.

    Linux's proc-FD path resolves the held file, including a required external
    symlink target. The retained SQLite connection owns the entire snapshot;
    every package operation also refuses changes to the logical member.

    Args:
        manifest (StoreManifest): Expected member, physical identity and UUID.
        handle (BinaryIO): Caller-owned file handle holding the store lock.

    Kwargs:
        None

    Returns:
        contextlib.AbstractContextManager: Yield an inode-bound read-only
            sqlite3.Connection in a read transaction; close it on exit.

    Raises:
        ProjectStoreError: If the handle/member/UUID/schema differs or the
            read-only connection cannot bind to the held file.
    """
    try:
        file_stat = os.fstat(handle.fileno())
        if (file_stat.st_dev, file_stat.st_ino) != manifest.db_identity:
            raise ProjectStoreError(
                "snapshot handle does not own the expected inode")
        _check_member(manifest)
        uri = f"file:/proc/self/fd/{handle.fileno()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            if _schema(connection) != _trusted_schema():
                raise ProjectStoreError(
                    "locked snapshot contains unsupported schema")
            _require_snapshot_identity(connection, manifest.project_uuid)
            _check_member(manifest)
            yield connection
    except (OSError, sqlite3.Error) as exc:
        raise ProjectStoreError(
            f"cannot bind locked snapshot ({exc})") from exc


def _rename(source: Path, target: Path, *, exchange: bool = False) -> None:
    """Publish without clobbering, or atomically exchange two Linux paths.

    Args:
        source (Path): Staged file/directory to publish or exchange.
        target (Path): Destination; must be absent for no-clobber publication.

    Kwargs:
        exchange (bool): Swap existing paths rather than use NOREPLACE.
            Defaults to False; True leaves the previous target at source.

    Returns:
        None

    Raises:
        ProjectStoreError: If libc lacks renameat2; no unsafe fallback exists.
        OSError: If renameat2 refuses the publish/exchange.

    Side Effects:
        Change directory entries atomically; durability fsync is caller-owned.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise ProjectStoreError(
            "Linux renameat2 required; unsafe rename fallback refused") from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 2 if exchange else 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


# --- Stage materialised manifest + WAL-safe DB into a fresh directory ---
def _stage_package(
    manifest: StoreManifest, staging: Path, project_uuid: uuid.UUID, created_at: str,
    source_connection: sqlite3.Connection,
) -> None:
    """Build a verified sanitized package, retaining failed partial staging.

    Args:
        manifest (StoreManifest): Original locked store identity/member gate.
        staging (Path): Absent output directory with an existing parent.
        project_uuid (uuid.UUID): UUID for the staged manifest and identity.
        created_at (str): Creation timestamp to write to the manifest.
        source_connection (sqlite3.Connection): Caller-owned original snapshot.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If staging exists, snapshot/schema/identity/counts
            disagree, or writing, sanitizing, validation or flushing fails.

    Side Effects:
        Write manifest/database, remove auth/session/admin rows and relabel
        identity in the copy; vacuum private bytes, then fsync the package.
        Research row IDs remain unchanged; partial staging is not deleted.
    """
    try:
        staging.mkdir(mode=0o700, parents=False)
    except FileExistsError as exc:
        raise ProjectStoreError(
            f"{staging}: staging already exists; remove it explicitly after inspection"
        ) from exc
    except OSError as exc:
        raise ProjectStoreError(
            f"{staging}: cannot create staging ({exc})") from exc
    try:
        manifest_doc = {
            "format_version": STORE_FORMAT_VERSION,
            "project_uuid": str(project_uuid),
            "database": DATABASE_FILENAME,
            "created_at": created_at,
        }
        (staging / MANIFEST_FILENAME).write_text(
            json.dumps(manifest_doc, indent=2) + "\n", encoding="utf-8"
        )
        _fsync_file(staging / MANIFEST_FILENAME)
        _backup_db_file(source_connection, staging /
                        DATABASE_FILENAME, manifest=manifest)
        # Complete schema and ORIGINAL identity are checked on the same
        # connection/transaction that sanitises and (for Save Copy) relabels.
        with closing(sqlite3.connect(staging / DATABASE_FILENAME)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN IMMEDIATE")
            if _schema(connection) != _trusted_schema():
                raise ProjectStoreError("snapshot contains unsupported schema")
            _require_snapshot_identity(connection, manifest.project_uuid)
            _check_member(manifest)
            expected = _table_counts(connection)
            connection.execute("PRAGMA secure_delete=ON")
            private = {name for name in expected if name.startswith("auth_")} | {
                "django_session", "django_admin_log",
            }
            for table in private:
                connection.execute(f'DELETE FROM "{table}"')
                expected[table] = 0
            connection.execute(
                f"UPDATE {IDENTITY_TABLE} SET project_uuid=?", (str(
                    project_uuid),)
            )
            connection.commit()
            connection.execute("PRAGMA journal_mode=DELETE")
            # Remove deleted private bytes from freelists, not merely their rows.
            connection.execute("VACUUM")
            if _table_counts(connection) != expected:
                raise ProjectStoreError(
                    "sanitised snapshot row counts disagree")
        _fsync_file(staging / DATABASE_FILENAME)
        validate_package(staging)
        _fsync_dir(staging)
    except ProjectStoreError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ProjectStoreError(
            f"{staging}: cannot stage package ({exc})") from exc


# --- Sibling staging path; never inside the source or destination ---
def _staging_for(dest: Path) -> Path:
    """Choose a random recovery sibling without creating it.

    Args:
        dest (Path): Destination whose parent/name define the sibling path.

    Kwargs:
        None

    Returns:
        Path: .<destination-name>.recovery-<random UUID hex> beside dest.
    """
    return dest.parent / f".{dest.name}.recovery-{uuid.uuid4().hex}"


# --- Refuse anything but an absent (or empty real-dir) destination ---
def _refuse_unless_creatable(dest: Path) -> None:
    """Refuse symlink, non-directory and populated output destinations.

    Args:
        dest (Path): Candidate absent path or empty real directory.

    Kwargs:
        None

    Returns:
        None

    Raises:
        ProjectStoreError: If the destination is unsuitable or listing fails.
    """
    if dest.is_symlink():
        raise ProjectStoreError(
            f"{dest}: destination is a symlink; refusing to write through it"
        )
    if dest.exists():
        if not dest.is_dir():
            raise ProjectStoreError(
                f"{dest}: target exists and is not a directory"
            )
        try:
            if any(dest.iterdir()):
                raise ProjectStoreError(
                    f"{dest}: refusing to overwrite a populated directory"
                )
        except OSError as exc:
            raise ProjectStoreError(
                f"{dest}: cannot list directory ({exc})") from exc


# --- Save Copy: same research rows under a NEW project UUID ---
def _copy_store(source: Path | str, dest: Path | str, *, new_identity: bool) -> StoreManifest:
    """Copy a store to a new directory; only Save Copy mints a fresh UUID.

    The source is only read (WAL-safe backup API, read-only opens); its
    manifest, identity row and row IDs are never mutated. The destination
    gains the chosen UUID in both manifest and identity row while local
    research row IDs stay stable. Populated destinations and symlink
    destinations are refused. A failed copy leaves the staging directory
    for inspection and never a half-visible destination.

    Args:
        source (Path | str): Store to validate and read without modification.
        dest (Path | str): Absent/empty, non-symlink output outside the source.

    Kwargs:
        new_identity (bool): Required; True mints UUID/time for Save Copy and
            permits a shared source lock. False preserves both for Backup
            and requires an exclusive source lock.

    Returns:
        StoreManifest: Published copy's validated manifest and physical inode.

    Raises:
        ProjectStoreError: If source, destination, ownership, staging or
            publication checks fail; retained recovery staging is not removed.

    Side Effects:
        Create a sanitized snapshot in sibling staging, remove an empty
        destination if present, atomically publish, and fsync the parent.
    """
    try:
        manifest = validate_package(source, strict_members=False)
        dest_path = Path(dest).expanduser().absolute()
        _refuse_unless_creatable(dest_path)
        if dest_path.resolve() == manifest.location:
            raise ProjectStoreError("destination is the source store")
        staging = _staging_for(dest_path)
        with (
            store_file_lock(manifest, closed=not new_identity) as handle,
            _locked_snapshot(manifest, handle) as source_connection,
        ):
            _stage_package(
                manifest, staging, uuid.uuid4() if new_identity else manifest.project_uuid,
                datetime.now(UTC).isoformat(
                ) if new_identity else manifest.created_at,
                source_connection,
            )
            _check_member(manifest)
            # rmdir only removes an empty directory; NOREPLACE closes the
            # external race after this check. Never overwrite a populated target.
            if dest_path.exists():
                dest_path.rmdir()
            _rename(staging, dest_path)
            _fsync_dir(dest_path.parent)
        return open_project_store(dest_path)
    except (OSError, RuntimeError) as exc:
        raise ProjectStoreError(
            f"copy refused ({exc}); any recovery staging is retained") from exc


def save_copy_store(source: Path | str, dest: Path | str) -> StoreManifest:
    """Save a sanitized committed copy under a new UUID and creation timestamp.

    Args:
        source (Path | str): Source store; workers may remain open.
        dest (Path | str): Fresh absent/empty non-symlink destination.

    Kwargs:
        None

    Returns:
        StoreManifest: Published new project's manifest and database inode.

    Raises:
        ProjectStoreError: If validation, shared lock, copy or publish fails.

    Side Effects:
        Write the new package; preserve source and failed recovery staging.
    """
    return _copy_store(source, dest, new_identity=True)


# --- Backup: same-UUID package at an explicitly chosen fresh destination ---
def backup_project_store(source: Path | str, dest: Path | str) -> StoreManifest:
    """Back up a closed store as a sanitized package preserving UUID and time.

    Args:
        source (Path | str): Source requiring an exclusive ownership lock.
        dest (Path | str): Fresh absent/empty non-symlink destination.

    Kwargs:
        None

    Returns:
        StoreManifest: Published backup manifest with its own database inode.

    Raises:
        ProjectStoreError: If validation, locking, copy or publication fails.

    Side Effects:
        Write the backup only; retain the source and any failed staging.
    """
    return _copy_store(source, dest, new_identity=False)


# --- Restore: atomically swap a backup over its store, keeping recoverable prev ---
def restore_project_store(backup_dir: Path | str, target: Path | str) -> StoreManifest:
    """Replace a store with a same-UUID backup; the prior store is preserved.

    The backup must open cleanly and carry the same project UUID as the
    target store; mismatched UUIDs refuse (never a merge). The target's
    current content moves to a ``.<name>.recovery-<uuid>`` sibling, so an
    interrupted restore leaves either the prior valid store or the complete
    new one. This operation takes exclusive locks on the target and backup.

    Args:
        backup_dir (Path | str): Strict, valid same-UUID backup package.
        target (Path | str): Distinct store to replace; both stores must close
            their participating workers before exclusive locking succeeds.

    Kwargs:
        None

    Returns:
        StoreManifest: Restored target manifest with recovery_location pointing
            to the entire prior target directory now at a recovery sibling.

    Raises:
        ProjectStoreError: If identities differ, physical files coincide,
            locking/validation/staging fails, or exchange/publication fails.

    Side Effects:
        Stage a sanitized backup copy, atomically exchange directories, and
        retain the previous target. A changed prior member triggers exchange
        rollback; failures retain recovery material for inspection.
    """
    try:
        backup_manifest = validate_package(backup_dir)
        target_manifest = validate_package(target, strict_members=False)
        target_path = target_manifest.location
        if (backup_manifest.project_uuid != target_manifest.project_uuid
                or backup_manifest.db_identity == target_manifest.db_identity):
            raise ProjectStoreError(
                "restore needs a distinct same-UUID backup, never a merge")
        staging = _staging_for(target_path)
        with (
            store_file_lock(target_manifest, closed=True) as target_handle,
            store_file_lock(backup_manifest, closed=True) as backup_handle,
            _locked_snapshot(target_manifest, target_handle),
            _locked_snapshot(backup_manifest, backup_handle) as source_connection,
        ):
            _stage_package(
                backup_manifest, staging, backup_manifest.project_uuid,
                backup_manifest.created_at, source_connection,
            )
            # Keep the new inode closed too until prior-store verification
            # completes, including a rollback if the exchange raced a change.
            staged_manifest = open_project_store(staging)
            with store_file_lock(staged_manifest, closed=True):
                _check_member(target_manifest)
                _check_member(backup_manifest)
                _fsync_dir(target_path.parent)
                _rename(staging, target_path, exchange=True)
                try:
                    _check_member(replace(target_manifest, location=staging))
                except ProjectStoreError:
                    _rename(staging, target_path, exchange=True)
                    _fsync_dir(target_path.parent)
                    raise
                # One atomic exchange: no missing-target interruption window.
                # The ENTIRE prior folder (including backups/links) survives here.
                _fsync_dir(target_path.parent)
        return replace(open_project_store(target_path), recovery_location=staging)
    except (OSError, RuntimeError) as exc:
        raise ProjectStoreError(
            f"restore refused ({exc}); prior store/recovery retained") from exc


def backup_store_db(database_path: Path | str, backups_dir: Path | str) -> Path:
    """Write an integrity-checked WAL-safe snapshot, not a full package.

    Args:
        database_path (Path | str): Existing source database; never created.
        backups_dir (Path | str): Output directory; create parents if needed.

    Kwargs:
        None

    Returns:
        Path: Published <source-stem>-<UTC timestamp>.sqlite3 backup path.

    Raises:
        ProjectStoreError: If the source is missing/not a file, a same-second
            target exists, or copy, integrity, flush or publication fails.

    Side Effects:
        Write a sibling-staged snapshot and atomically publish without clobber.
        The source remains unchanged; failed staging is retained.
    """
    source = Path(database_path)
    if not source.exists():
        raise ProjectStoreError(
            f"{source}: backup source missing; refusing to create one")
    try:
        if not source.is_file():
            raise ProjectStoreError(f"{source}: backup source is not a file")
    except OSError as exc:
        raise ProjectStoreError(
            f"{source}: cannot stat backup source ({exc})") from exc
    target_dir = Path(backups_dir)
    try:
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectStoreError(
            f"{target_dir}: cannot create backups dir ({exc})") from exc
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"{source.stem}-{stamp}.sqlite3"
    if target.exists():
        raise ProjectStoreError(f"{target}: backup target already exists")
    staging = target.with_name(f".{target.name}-{uuid.uuid4().hex}")
    _backup_db_file(source, staging)
    _check_integrity(staging)
    try:
        _rename(staging, target)
        _fsync_dir(target_dir)
    except OSError as exc:
        raise ProjectStoreError(
            f"{source}: backup publication failed ({exc})") from exc
    return target
