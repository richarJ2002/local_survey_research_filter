"""Explicit, mapping-approved shared-database conversion; never a startup hook.

@File: legacy_conversion.py
@Brief: Read-only inventory and retained SQLite snapshots to isolated project stores.
@Date: 03/10/2026
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from literature import project_store as ps

# --- Explicit ownership rules; no discovery-origin or metadata inference ---
TABLES = (
    "project",
    "search",
    "searchrun",
    "stage",
    "paper",
    "discovery",
    "screeningdecision",
    "screeningrevision",
    "paperkeywordassignment",
    "surveyentry",
    "surveycitation",
    "opencitationslookup",
)
GLOBAL_FIELDS = {"sources": list, "keywords": list, "raw": dict, "source_ids": dict}
NAMESPACE = uuid.UUID("78096620-a790-4392-a0b6-97386360a7ea")
ROOT = Path(__file__).resolve().parent.parent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _bootstrap(database: Path) -> None:
    """Migrate only a fresh, fixed target in a separate process, without .env."""
    if database.exists() or database.is_symlink():
        raise ps.ProjectStoreError("bootstrap target already exists; refusing migration")
    ps._run_migrate(database)


@lru_cache(maxsize=1)
def _reference() -> tuple:
    with tempfile.TemporaryDirectory(prefix="wp09-schema-") as tmp:
        db = Path(tmp) / ps.DATABASE_FILENAME
        _bootstrap(db)
        with closing(ps._connect_ro(db)) as connection:
            return ps._schema(connection)


@contextmanager
def _source(path: Path) -> Iterator[sqlite3.Connection]:
    """Pin committed reads to the selected physical file, including its WAL."""
    with path.open("rb") as handle:
        identity = os.fstat(handle.fileno())
        with closing(
            sqlite3.connect(f"file:/proc/self/fd/{handle.fileno()}?mode=ro", uri=True, timeout=30)
        ) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            if ps._schema(connection) != _reference():
                raise ps.ProjectStoreError("unsupported legacy schema; no automatic upgrade")
            yield connection
            current = path.stat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ps.ProjectStoreError("legacy source member changed; refusing conversion")


def _read(connection: sqlite3.Connection) -> dict[str, list[dict]]:
    """Read research fields only, never auth/session/private application records."""
    # ponytail: materialise the MVP inventory; stream rows if a legacy library exceeds RAM.
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ps.ProjectStoreError("legacy database integrity failure")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ps.ProjectStoreError("legacy database foreign-key failure")
    expected = {p.stem for p in (ROOT / "literature" / "migrations").glob("[0-9]*.py")}
    applied = {
        r[0]
        for r in connection.execute("SELECT name FROM django_migrations WHERE app='literature'")
    }
    if applied != expected:
        raise ps.ProjectStoreError("unknown/partial legacy migration history; upgrade separately")
    rows = {}
    for table in TABLES:
        cursor = connection.execute(f'SELECT * FROM "literature_{table}" ORDER BY id')
        columns = [c[0] for c in cursor.description]
        rows[table] = [dict(zip(columns, row, strict=True)) for row in cursor]
    return rows


def _ownership(rows: dict) -> tuple[dict, dict]:
    """Follow stored project FKs and refuse inconsistent relational ownership."""
    owners = {table: {} for table in TABLES}
    owners["project"] = {r["id"]: r["id"] for r in rows["project"]}
    for table in ("search", "stage", "paperkeywordassignment", "surveyentry"):
        owners[table] = {r["id"]: r["project_id"] for r in rows[table]}
    for table, parent, key in (
        ("searchrun", "search", "search_id"),
        ("discovery", "search", "search_id"),
        ("screeningdecision", "stage", "stage_id"),
        ("screeningrevision", "stage", "stage_id"),
        ("surveycitation", "surveyentry", "citing_id"),
    ):
        owners[table] = {r["id"]: owners[parent][r[key]] for r in rows[table]}
    runs = {r["id"]: r["search_id"] for r in rows["searchrun"]}
    for r in rows["discovery"]:
        if r["search_run_id"] is not None and runs[r["search_run_id"]] != r["search_id"]:
            raise ps.ProjectStoreError(f"discovery {r['id']}: run belongs to another search")
    for r in rows["paperkeywordassignment"]:
        if owners["search"][r["search_id"]] != r["project_id"]:
            raise ps.ProjectStoreError(f"assignment {r['id']}: cross-project search")
    for r in rows["surveycitation"]:
        if owners["surveyentry"][r["cited_id"]] != owners["surveyentry"][r["citing_id"]]:
            raise ps.ProjectStoreError(f"citation {r['id']}: cross-project endpoints")
    for r in rows["screeningdecision"]:
        if r["decision"] not in {"PENDING", "ACCEPT", "REJECT"}:
            raise ps.ProjectStoreError(f"decision {r['id']}: unsupported choice")
    papers = {r["id"]: set() for r in rows["paper"]}
    for table in (
        "discovery",
        "screeningdecision",
        "screeningrevision",
        "paperkeywordassignment",
        "surveyentry",
    ):
        for r in rows[table]:
            if r["paper_id"] is not None:
                papers[r["paper_id"]].add(owners[table][r["id"]])
    return owners, papers


def _inventory(rows: dict) -> dict:
    owners, papers = _ownership(rows)
    for paper in rows["paper"]:
        for field, kind in GLOBAL_FIELDS.items():
            if type(json.loads(paper[field])) is not kind:
                raise ps.ProjectStoreError(f"paper {paper['id']}: unsupported {field} value")
    source_digest = _digest(rows)
    projects = {}
    for project in rows["project"]:
        pid = project["id"]
        fields = {}
        for paper in rows["paper"]:
            if pid not in papers[paper["id"]]:
                continue
            fields[str(paper["id"])] = {
                field: {"action": "review" if json.loads(paper[field]) else "exclude"}
                for field in GLOBAL_FIELDS
            }
        projects[str(pid)] = {
            "destination": None,
            "project_uuid": str(uuid.uuid5(NAMESPACE, f"{source_digest}:{pid}")),
            "papers": fields,
        }
    return {
        "format_version": 1,
        "source_digest": source_digest,
        "counts": {t: len(r) for t, r in rows.items()},
        "ownership": {
            t: {str(pk): pid for pk, pid in mapping.items()}
            for t, mapping in owners.items()
            if mapping
        },
        "global_fields": {
            str(p["id"]): {
                "owners": sorted(papers[p["id"]]),
                "fields": {
                    f: {"nonempty": bool(json.loads(p[f])), "sha256": _digest(p[f])}
                    for f in GLOBAL_FIELDS
                },
            }
            for p in rows["paper"]
        },
        "warnings": [
            "Canonical bibliographic values are current globals, not historical project versions.",
            "sources/keywords/raw/source_ids need explicit approval; no origin inference.",
            "Unowned papers require explicit exclusion; original and private snapshot retain them.",
            "Cache rows are limited to local DOI keys; edges to nonlocal DOIs are excluded.",
        ],
        "mapping_template": {
            "format_version": 1,
            "source_digest": source_digest,
            "approved": False,
            "canonical_bibliography": "copy-current-globals",
            "cache_policy": "local-doi-keys-and-edges",
            "projects": projects,
            "orphan_papers": {str(pk): "review" for pk, own in papers.items() if not own},
        },
    }


def inventory_legacy(source: Path | str) -> dict:
    """Inventory an explicitly supplied source without modifying research records."""
    try:
        with _source(Path(source)) as connection:
            return _inventory(_read(connection))
    except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
        raise ps.ProjectStoreError("legacy inventory failed; source not modified") from exc


def _approved(rows: dict, mapping: dict) -> dict[str, Path]:
    template = _inventory(rows)["mapping_template"]
    if (
        not isinstance(mapping, dict)
        or set(mapping) != set(template)
        or type(mapping["format_version"]) is not int
        or mapping["format_version"] != 1
        or mapping["approved"] is not True
        or mapping["canonical_bibliography"] != template["canonical_bibliography"]
        or mapping["cache_policy"] != template["cache_policy"]
        or mapping["source_digest"] != template["source_digest"]
    ):
        raise ps.ProjectStoreError(
            "mapping must explicitly approve this exact source digest/version"
        )
    if (
        not isinstance(mapping["orphan_papers"], dict)
        or set(mapping["orphan_papers"]) != set(template["orphan_papers"])
        or any(v != "exclude" for v in mapping["orphan_papers"].values())
    ):
        raise ps.ProjectStoreError("unowned papers require explicit exclude mapping")
    if not isinstance(mapping["projects"], dict) or set(mapping["projects"]) != set(
        template["projects"]
    ):
        raise ps.ProjectStoreError("mapping must name every project exactly once")
    destinations = {}
    for pid, expected in template["projects"].items():
        project = mapping["projects"][pid]
        if (
            not isinstance(project, dict)
            or set(project) != set(expected)
            or project["project_uuid"] != expected["project_uuid"]
            or not isinstance(project["papers"], dict)
            or set(project["papers"]) != set(expected["papers"])
        ):
            raise ps.ProjectStoreError(f"project {pid}: wrong stable identity/paper mapping")
        destination = project["destination"]
        if not isinstance(destination, str) or not Path(destination).is_absolute():
            raise ps.ProjectStoreError(f"project {pid}: explicit absolute destination required")
        path = Path(destination)
        if path.exists() or path.is_symlink():
            raise ps.ProjectStoreError(f"project {pid}: nonempty/partial/repeated output refused")
        if not path.parent.is_dir():
            raise ps.ProjectStoreError(f"project {pid}: destination parent must exist")
        destinations[pid] = path.resolve()
        for paper_id, fields in project["papers"].items():
            if not isinstance(fields, dict) or set(fields) != set(GLOBAL_FIELDS):
                raise ps.ProjectStoreError(f"paper {paper_id}: all global fields require a policy")
            for field, policy in fields.items():
                if not isinstance(policy, dict):
                    raise ps.ProjectStoreError(f"paper {paper_id}: invalid {field} policy")
                action = policy.get("action")
                if action not in {"copy", "exclude", "replace"}:
                    raise ps.ProjectStoreError(f"paper {paper_id}: unresolved {field} ownership")
                keys = {"action", "value"} if action == "replace" else {"action"}
                if set(policy) != keys or (
                    action == "replace" and type(policy["value"]) is not GLOBAL_FIELDS[field]
                ):
                    raise ps.ProjectStoreError(f"paper {paper_id}: invalid {field} replacement")
    paths = list(destinations.values())
    if len(paths) != len(set(paths)) or any(
        a.is_relative_to(b) for a in paths for b in paths if a != b
    ):
        raise ps.ProjectStoreError("project destinations overlap")
    return destinations


def _doi(raw: str) -> str:
    return (
        (raw or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "")
    )


def _local_dois(projection: dict) -> set[str]:
    """Match runtime-supported DOI identities, never references or title guesses."""
    dois = {_doi(r["doi"]) for t in ("paper", "surveyentry") for r in projection[t]} - {""}
    for paper in projection["paper"]:
        ids = json.loads(paper["source_ids"])
        arxiv = re.fullmatch(r"(\d{4}\.\d{4,5})(?:v\d+)?", str(ids.get("arxiv") or ""))
        if arxiv:
            dois.add("10.48550/arxiv." + arxiv.group(1))
    for entry in projection["surveyentry"]:
        aliases = json.loads(entry["identity_aliases"])
        if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
            raise ps.ProjectStoreError(f"survey entry {entry['id']}: unsupported identity aliases")
        for alias in aliases:
            doi = _doi(alias)
            if re.fullmatch(r"10\.\d{4,9}/\S+", doi):
                dois.add(doi)
    return dois


def _projection(rows: dict, mapping: dict, pid: str) -> dict:
    owners, papers = _ownership(rows)
    project_id = int(pid)
    result = {
        table: [dict(r) for r in records if owners[table].get(r["id"]) == project_id]
        for table, records in rows.items()
    }
    result["paper"] = [dict(r) for r in rows["paper"] if project_id in papers[r["id"]]]
    for paper in result["paper"]:
        for field, policy in mapping["projects"][pid]["papers"][str(paper["id"])].items():
            if policy["action"] != "copy":
                value = policy["value"] if policy["action"] == "replace" else GLOBAL_FIELDS[field]()
                paper[field] = json.dumps(value, ensure_ascii=False)
    dois = _local_dois(result)
    for r in rows["opencitationslookup"]:
        if _doi(r["doi"]) not in dois:
            continue
        edges = json.loads(r["edges"])
        if not isinstance(edges, list) or any(
            not isinstance(e, list) or len(e) != 2 or any(not isinstance(v, str) for v in e)
            for e in edges
        ):
            raise ps.ProjectStoreError(
                f"cache {r['id']}: unsupported edge shape; resolve separately"
            )
        result["opencitationslookup"].append(
            dict(
                r,
                edges=json.dumps(
                    [e for e in edges if all(_doi(v) in dois for v in e)], ensure_ascii=False
                ),
            )
        )
    return result


def _evidence(connection: sqlite3.Connection, rows: dict, projection: dict) -> dict:
    """Record all PK/FK mappings and exact field projections without printing notes."""
    result = {}
    ids = {t: {r["id"] for r in records} for t, records in projection.items()}
    for table in TABLES:
        fields = [c[1] for c in connection.execute(f'PRAGMA table_info("literature_{table}")')]
        fks = list(connection.execute(f'PRAGMA foreign_key_list("literature_{table}")'))
        links = []
        for r in projection[table]:
            for fk in fks:
                parent, field = fk[2].removeprefix("literature_"), fk[3]
                value = r[field]
                if value is not None and value not in ids[parent]:
                    raise ps.ProjectStoreError(f"{table} {r['id']}: unmapped {field}")
                links.append(
                    {
                        "row": r["id"],
                        "field": field,
                        "target_table": parent,
                        "source_fk": value,
                        "target_fk": value,
                    }
                )
        expected = [[r[f] for f in fields] for r in projection[table]]
        original = [[r[f] for f in fields] for r in rows[table] if r["id"] in ids[table]]
        result[table] = {
            "fields": fields,
            "count": len(expected),
            "source_total": len(rows[table]),
            "source_selected_sha256": _digest(original),
            "projected_sha256": _digest(expected),
            "pk_mapping": {str(pk): pk for pk in sorted(ids[table])},
            "fk_mapping": links,
            "transformed_fields": [
                f
                for f in fields
                if any(
                    a[f] != b[f]
                    for a, b in zip(
                        [r for r in rows[table] if r["id"] in ids[table]],
                        projection[table],
                        strict=True,
                    )
                )
            ],
        }
    return result


def dry_run_legacy(source: Path | str, mapping: dict) -> dict:
    """Validate mapping, scope and destinations; create no output or snapshot."""
    try:
        with _source(Path(source)) as connection:
            rows = _read(connection)
            destinations = _approved(rows, mapping)
            return {
                "source_digest": _digest(rows),
                "mapping_sha256": _digest(mapping),
                "projects": {
                    pid: {
                        "destination": str(path),
                        "evidence": _evidence(connection, rows, _projection(rows, mapping, pid)),
                    }
                    for pid, path in destinations.items()
                },
            }
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise ps.ProjectStoreError("dry run failed; source not modified") from exc


# --- Stage all outputs, verify every field and FK, only then publish ---
def _write_json(path: Path, document: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ps._fsync_file(tmp)
    os.replace(tmp, path)
    ps._fsync_dir(path.parent)


def _stage(staging: Path, projection: dict, project: dict, evidence: dict, created: str) -> None:
    staging.mkdir(mode=0o700)
    db = staging / ps.DATABASE_FILENAME
    _bootstrap(db)
    ps._write_identity(db, uuid.UUID(project["project_uuid"]))
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for table in TABLES:
            # Fresh migration bootstrap must not seed/reset any research row.
            if connection.execute(f'SELECT COUNT(*) FROM "literature_{table}"').fetchone()[0]:
                raise ps.ProjectStoreError("bootstrap contains research rows; refusing overwrite")
            fields = evidence[table]["fields"]
            names = ",".join(f'"{f}"' for f in fields)
            placeholders = ",".join("?" for _ in fields)
            connection.executemany(
                f'INSERT INTO "literature_{table}" ({names}) VALUES ({placeholders})',
                [[r[f] for f in fields] for r in projection[table]],
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ps.ProjectStoreError("target foreign-key verification failed")
        connection.commit()
        for table in TABLES:
            actual = connection.execute(
                f'SELECT * FROM "literature_{table}" ORDER BY id'
            ).fetchall()
            expected = [tuple(r[f] for f in evidence[table]["fields"]) for r in projection[table]]
            if actual != expected:
                raise ps.ProjectStoreError(f"{table}: source-to-target field verification failed")
            evidence[table]["target_sha256"] = _digest(actual)
            if evidence[table]["target_sha256"] != evidence[table]["projected_sha256"]:
                raise ps.ProjectStoreError(f"{table}: target hash disagrees")
    _write_json(
        staging / ps.MANIFEST_FILENAME,
        {
            "format_version": ps.STORE_FORMAT_VERSION,
            "project_uuid": project["project_uuid"],
            "database": ps.DATABASE_FILENAME,
            "created_at": created,
        },
    )
    ps._fsync_file(db)
    ps.validate_package(staging)
    ps._fsync_dir(staging)


def convert_legacy(source: Path | str, mapping: dict, recovery: Path | str) -> dict:
    """Convert only explicit approved mappings; retain original, snapshot and report.

    All destinations must be absent, including repeat runs. Each publication is
    atomic; a multi-directory batch is not atomic. Failed/interrupted batches
    retain verified outputs/staging, and cannot be silently resumed or overwritten.
    """
    source, recovery = Path(source), Path(recovery)
    report_path = recovery / "conversion-report.json"
    report = None
    try:
        with _source(source) as connection:
            rows = _read(connection)
            destinations = _approved(rows, mapping)
            if not destinations:
                raise ps.ProjectStoreError("legacy source contains no projects")
            if not recovery.is_absolute() or recovery.exists() or recovery.is_symlink():
                raise ps.ProjectStoreError("explicit absent absolute recovery directory required")
            recovery = recovery.resolve()
            if (
                not recovery.parent.is_dir()
                or any(
                    recovery.is_relative_to(p) or p.is_relative_to(recovery)
                    for p in destinations.values()
                )
                or source.resolve().is_relative_to(recovery)
            ):
                raise ps.ProjectStoreError(
                    "recovery/destination/source locations overlap or parent missing"
                )
            projections = {pid: _projection(rows, mapping, pid) for pid in destinations}
            evidence = {pid: _evidence(connection, rows, p) for pid, p in projections.items()}
            # Nothing persistent is created until every mapping/FK/output gate passes.
            recovery.mkdir(mode=0o700)
            report = {
                "status": "snapshotting",
                "source": str(source.absolute()),
                "source_digest": _digest(rows),
                "mapping_sha256": _digest(mapping),
                "canonical_bibliography": mapping["canonical_bibliography"],
                "cache_policy": mapping["cache_policy"],
                "excluded_orphan_papers": mapping["orphan_papers"],
                "snapshot": str(recovery / "source.sqlite3"),
                "published": [],
                "rollback": "Keep original unchanged. Close converted stores before moving them; inspect retained staging/report. No automatic rollback or deletion.",
                "projects": {
                    pid: {
                        "destination": str(path),
                        "project_uuid": mapping["projects"][pid]["project_uuid"],
                        "global_field_policy": {
                            paper: {
                                field: {"action": policy["action"]}
                                for field, policy in fields.items()
                            }
                            for paper, fields in mapping["projects"][pid]["papers"].items()
                        },
                        "staging": str(path.parent / f".{path.name}.conversion-{uuid.uuid4().hex}"),
                        "evidence": evidence[pid],
                    }
                    for pid, path in destinations.items()
                },
            }
            _write_json(report_path, report)
            ps._backup_db_file(connection, recovery / "source.sqlite3")
            ps._check_integrity(recovery / "source.sqlite3")
            with closing(ps._connect_ro(recovery / "source.sqlite3")) as snapshot:
                if _digest(_read(snapshot)) != report["source_digest"]:
                    raise ps.ProjectStoreError(
                        "retained snapshot disagrees with inventoried source"
                    )
            with (recovery / "source.sqlite3").open("rb") as handle:
                report["snapshot_sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
        report["status"] = "staging"
        _write_json(report_path, report)
        created = datetime.now(UTC).isoformat()
        for pid, item in report["projects"].items():
            _stage(
                Path(item["staging"]),
                projections[pid],
                mapping["projects"][pid],
                evidence[pid],
                created,
            )
        report["status"] = "verified; publishing"
        _write_json(report_path, report)
        # ponytail: atomic per directory, not across filesystems/projects;
        # report + retained staging make a partial batch explicitly recoverable.
        for pid, item in report["projects"].items():
            ps._rename(Path(item["staging"]), Path(item["destination"]))
            ps._fsync_dir(Path(item["destination"]).parent)
            report["published"].append(pid)
            _write_json(report_path, report)
        report["status"] = "complete"
        _write_json(report_path, report)
        return report
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        KeyError,
        TypeError,
        ps.ProjectStoreError,
    ) as exc:
        if report is not None:
            report["status"] = (
                "failed; retained snapshot/staging/verified outputs require inspection"
            )
            try:
                _write_json(report_path, report)
            except (OSError, ps.ProjectStoreError):
                pass
        raise ps.ProjectStoreError(
            f"conversion refused ({exc}); original untouched; recovery: {recovery}"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    """Standalone CLI: no Django startup, default DB selection or credential reads."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("operation", choices=("inventory", "dry-run", "convert"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--mapping", type=Path)
    parser.add_argument("--recovery", type=Path)
    args = parser.parse_args(argv)
    # The shared fixed-target migration primitive disables dotenv in children;
    # the parent does not import config.settings or select a default database.
    if not settings.configured:
        settings.configure(BASE_DIR=ROOT)
    try:
        if args.operation == "inventory":
            if args.mapping or args.recovery:
                parser.error("inventory takes only --source")
            result = inventory_legacy(args.source)
        else:
            if not args.mapping:
                parser.error("--mapping required")
            mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
            if args.operation == "dry-run":
                if args.recovery:
                    parser.error("dry-run does not create recovery files")
                result = dry_run_legacy(args.source, mapping)
            else:
                if not args.recovery:
                    parser.error("convert requires --recovery")
                result = convert_legacy(args.source, mapping, args.recovery)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, ps.ProjectStoreError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
