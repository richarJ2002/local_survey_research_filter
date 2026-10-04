# Explicit legacy shared-database conversion (Linux)

This utility converts explicitly approved shared databases. It is **not** a startup migration and does
not authorise inspecting or converting the user's live database. The examples
below are for synthetic fixtures; live inspection and conversion each require
separate operator approval. Synthetic qualification does not establish the current
conversion status of any user's database.

For the per-project launcher and copy/recovery UI, see [README.md](README.md).
Before any live step, obtain exact source/destination/ownership approval, establish
a no-write maintenance window and an independently verified WAL-aware private
backup, review dry-run projections/hashes and every PK/FK mapping, and retain an
original-launch rollback plan. Read-only inventory approval is not conversion,
backup or process-stop authority. An uninspected source's schema eligibility is unknown.

## Preconditions

- Use the installed Python/Django environment, Linux proc-FD support and a local
  filesystem supporting `renameat2`. No new dependencies are needed.
- The input must have the exact current shared-DB schema and all current
  literature migrations, **without** a project-store identity table. Unknown,
  older, partial or structurally different databases refuse. This command never
  upgrades the source. An older source needs a separately approved preparation
  procedure, not a guessed schema adaptation.
- Supply the source file explicitly. There is no default path, database fallback,
  provider call, Django application startup, registry update or worker launch.
- Every project destination and the private recovery directory must be explicit,
  absolute and absent, with existing parents. Existing empty directories also
  refuse, as do populated/partial/repeated outputs and overlapping destinations.
- For eventual operator use, close legacy writers to obtain the intended cutover
  point. The backup API includes committed WAL data from one pinned read snapshot;
  uncommitted writes are not copied, and the source is never checkpointed.

## Inventory, approve, dry run, convert

```bash
.venv/bin/python -m literature.legacy_conversion inventory \
  --source /absolute/synthetic/shared.sqlite3 > /private/inventory.json
```

Inventory reads research fields only. It prints project/row IDs, ownership,
counts, global-field presence/hashes and a `mapping_template`, not paper titles,
notes, raw payloads or credential values. It creates no research records or
destinations. Trusted schema construction migrates only a temporary synthetic DB.

Copy the `mapping_template` object into a private mapping JSON file. Review it:

1. Keep `source_digest`, `format_version` and deterministic `project_uuid` values
   unchanged. Set an absolute `destination` for **every** project.
2. For every owned paper, approve **all four** global fields: `sources`,
   `keywords`, `raw` and `source_ids`. Each field policy must be exactly one of:
   `{"action":"exclude"}`, `{"action":"copy"}` or
   `{"action":"replace","value":...}`. Replacements must be lists for
   sources/keywords and objects for raw/source_ids. `review` blocks conversion.
   Empty globals default to exclusion; nothing nonempty is approved automatically.
3. `copy` is an ownership attestation, not an ownership guess. For mixed raw
   provenance or private notes, use exclusion or an explicitly reviewed subset
   replacement. Project-owned discoveries, notes, revisions and assignments are
   copied only into their actual owning project. Historical `manual`/blank origins
   are preserved exactly; current origins are never inferred from them.
4. Unowned papers require an explicit `"exclude"` entry. The original and retained
   snapshot keep them; the utility does not invent discovery/ownership records.
5. Explicitly approve `canonical_bibliography: "copy-current-globals"` and
   `cache_policy: "local-doi-keys-and-edges"`. Canonical bibliography is the current
   stored value, not an invented earlier per-project version. Cache rows retain
   local DOI identities, including grounded cached arXiv/curated DOI aliases;
   only edges whose two endpoints are local remain. Unsupported edge shapes
   refuse rather than being guessed or silently dropped.
6. Set `approved` to JSON `true` only after reviewing those choices.

```bash
.venv/bin/python -m literature.legacy_conversion dry-run \
  --source /absolute/synthetic/shared.sqlite3 --mapping /private/mapping.json

.venv/bin/python -m literature.legacy_conversion convert \
  --source /absolute/synthetic/shared.sqlite3 --mapping /private/mapping.json \
  --recovery /absolute/private/new-recovery-directory
```

Dry run validates mapping, ownership, destinations and every projected FK, and
prints table counts, field names, source-selected/projected hashes and PK/FK
mappings. It creates neither stores nor snapshots. The source digest binds
approval to all inventoried research fields; a changed source requires new review.
UUID5 identities are stable for the same source digest/project ID. All research
PKs, including project IDs, stay unchanged; separate stores may reuse paper IDs.
No default stages, screening decisions or feature fields are silently reset.

Conversion creates a **private** recovery directory containing `source.sqlite3`
and `conversion-report.json`. That raw snapshot may contain legacy private
application records and is **not a shareable package**. Destinations are freshly
migrated with fixed-target child processes (dotenv disabled), populated with
approved research rows only, and checked against exact field projections, hashes,
FKs, schema and integrity before any destination is published. Auth/session data
are never projected. Each resulting package contains only `project.json` and
`project.sqlite3`; it can then be explicitly Loaded through the existing manager.

## Failure and rollback

Publication is atomic **per project**, not across a batch or filesystems. All
projects are staged and verified before publication begins. A failure retains the
original, snapshot, report, staging and any already published complete outputs.
An interruption between a rename and report update may leave a verified output
not yet listed in `published`; inspect both recorded staging and destination
locations. Repeat/partial runs refuse before creating another recovery directory.
Nothing is automatically deleted, overwritten, resumed, merged or rolled back.

Keep the original and recovery private. Inspect the report's UUIDs, destination
and staging paths, PK/FK maps and source-selected/projected/target hashes. Close
converted stores before an explicitly approved recovery/move operation. The
original needs no restoration because conversion never writes it. Genuine power
loss, full-filesystem and network-filesystem qualification remain unrun.

Ordinary `python manage.py runserver 8000` now opens the New / Load manager,
just like `python3.12 launch.py`. Load the verified converted folder; do not
rerun conversion or `migrate` as a startup step. The remembered workspace is a
location preference, not an automatically selected project.

To deliberately use the retained original for approved rollback, first close the
manager in its owning terminal and close any old writers yourself. Then use an
explicit absolute original DB target and the developer opt-in:

```bash
PYTHON_DOTENV_DISABLED=1 LOCAL_SURVEY_DB=/absolute/retained-original.sqlite3 \
  .venv/bin/python manage.py runserver 8000 --legacy-shared-db --noreload
```

This starts the old Django server without migrating/restoring/merging anything.
Omit `PYTHON_DOTENV_DISABLED=1` only when deliberately opting into the source
`.env` compatibility. The developer banner reports the selected mode, not whether
conversion was completed. Any new edits to the original and converted stores
diverge; they are never synchronised. Never delete either database to change
startup behavior. See README's developer section for explicit administrative
targets and startup flag limitations.
