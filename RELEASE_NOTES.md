# Startup cutover — 2026-10-04

Ordinary `python manage.py runserver [8000]` now enters the existing foreground
New / Load manager, with the same private XDG state and worker ownership as
`python3.12 launch.py`. Port-only and IPv4 loopback `localhost:port` /
`127.0.0.1:port` syntax and help are supported; old Django flags fail explicitly.
No automatic conversion, project migration or last-project fallback is introduced.

The old Django server requires `--legacy-shared-db`. Administrative writes need
an explicit `LOCAL_SURVEY_DB` or that deliberate legacy opt-in; ordinary checks,
tests and migration-file generation default to memory rather than legacy truth.
The developer banner no longer asserts that conversion is pending. See
[README.md](README.md) and [LEGACY_CONVERSION.md](LEGACY_CONVERSION.md) for startup
and retained-original rollback commands. The new entry-point dispatch takes effect
on the next startup; an already-running `launch.py` manager does not need a restart
for this alias change alone. This cutover does not stop a listener.

This source change does not inspect or modify live stores and is not a new claim
about their conversion status. The source-preparation evidence below is historical.

Startup-cutover verification used a temporary source checkout, synthetic DBs and
private temporary XDG state with dotenv disabled: **20/20** focused tests
(93.697s), **408/408** full Django tests (204.157s, no failures/errors/skips),
Django check/no migration drift and all four Node checks passed. Actual
`manage.py runserver` started the manager for the offline Load/edit/Save Copy
roundtrip and occupied-port refusal; explicit developer startup and fixed-worker
dispatch remained usable. A synthetic legacy-file sentinel survived refused
implicit `migrate` unchanged. Scoped Ruff lint and entry-point/test formatting
passed; whole-file settings formatting remains outside scope because of existing
style differences. The code-only graph update completed; semantic docs were not
regenerated. No live database inspection, conversion, backup or process control
was performed by this startup change.

## Source-only preparation — 2026-10-03

**Local synthetic/code qualification only; not a published Git release.**
At that source-preparation stage, live legacy adoption was **BLOCKED — NOT
AUTHORISED / NOT RUN**. No live inventory,
backup, conversion, migration or research edits were performed. Source preparation
does not authorise any such operation or a commit/push/tag/GitHub release.

## Delivered in the current source tree

- Linux `python3.12 launch.py`: foreground loopback manager, first-run isolated
  dependency installation, offline repeat startup, native New/Load and remembered
  workspace root. One immutable-DB worker per open physical project store.
- UUID/epoch-scoped navigation, independent tab leases and Web Locks ownership,
  submitted SQLite saves, own-tab Exit/Back, draft retention on cancellation or
  failed backend release, owned graceful/crash drains and stale-handle recovery.
- Current distinct-paper/multi-source bars separated from historical import
  totals; same-project content revalidation; local installed Plotly and standalone
  offline chart exports; project-scoped Markdown provenance/notes/assignments.
- Atomic two-file Save Copy (new UUID), closed-store Backup/Restore (same UUID),
  materialised required symlinks, retained recovery siblings; separately approved
  legacy inventory/mapping/dry-run/conversion utility, never automatic migration.
- Preserved predecessor UI work: sortable/paginated papers table, graph node and
  IN/THROUGH chart-point paper lists, editable curated snapshots with provenance,
  positive manuscript labels, directed manual links/cycles and shared graph panel.
  Citation geometry changes only presentation, not membership or stored edges.
- Task-oriented README/storage diagrams, MIT/third-party notices and runnable
  synthetic example. No real survey database/sample binary is shipped.

## Evidence and support boundary

Predecessor qualification after the owning Exit error refix: **395/395** Django
tests, **55/55** focused tests, Django check/no migration drift and four Node
checks passed. Independent functional review reran 17 predecessor tests and
accepted six fixes before that narrow refix; the refix has its own actual
registry DELETE/HTTP503/draft-retention regression and offline Firefox evidence.
That predecessor evidence was not a new independent phase12 review or a claim of
live cutover; final source-only acceptance is recorded separately below.

Source-preparation rerun on 2026-10-03: **396/396** full Django tests (149.989s,
no failures/errors/skips), **47/47** focused launcher/qualification/copy tests
(67.983s), check/no migration drift, four Node checks and scoped Ruff lint/format
passed. The new regression generates the documented sample in a clean source-only
temporary checkout containing current dirty/new source, opens/edits/shares/reopens
it via the actual launcher with synthetic XDG state and outbound sockets disabled.
All research tables roundtrip unchanged except the deliberately submitted search
edit; the copy gets a new UUID. Repeated sample creation refuses without overwrite.
Existing Backup/Restore, WAL and recovery tests remain green.

Offline isolated Firefox was rerun successfully, including actual backend Exit
DELETE failure/draft retention, A/A and A/B isolation, graphs/cycles, sharing,
Backup/Restore, restart and native no-JS New/Load. It uses installed runtime modules
in temporary state, not the user's browser or processes. The synthetic example
command also ran directly; no binary database was created in source.

Qualified host: Linux, Python 3.12.3, Django 5.2.17, SQLite 3.45.1, Plotly Python
7.1.0 and isolated Firefox 156.0.1. Dependency ranges remain unpinned. Editable
workers need JavaScript + Web Locks; no-JS native New/Load reaches read-only pages.
Linux proc-FD/flock/renameat2 and a local writable filesystem are required.

Not qualified: Windows/macOS, Chromium/user Chrome, network filesystems, physical
power loss/full disks/failing storage, concurrent distinct-XDG/user cold starts,
screen readers/touch/mobile and every advanced history/BFCache combination.
Injected write/interruption failures do not prove physical storage durability.
Large-library content fingerprint cost is not benchmarked; shutdown waits for
accepted work rather than killing it. Unmanaged/old SQLite clients must be closed
by their operator. There is no general old-store upgrade or automatic recovery merge.

## Final source-only acceptance — recorded 2026-10-03

The manager's final synthetic rerun passed **396/396** Django tests (`-v1`,
148.660s, zero skips), with dotenv disabled and an explicit temporary DB override.
Django check/no migration drift, all four Node checks (paper-entry six subchecks),
handle/refresh JavaScript syntax, whitespace and empty-staged-diff checks passed.
Independent final docs/source-only review **PASS**: complete MapLibre and ieee754
notices resolve the licence-documentation hold; no remaining material finding in
scope. This accepts local synthetic/code-qualified preparation, **not** live
research adoption or Git publication. Earlier evidence and limitations remain.

## Publication disposition at source preparation — 2026-10-03

The intended package is source/config/migrations/tests/docs/licences only,
including the then-untracked predecessor files and dirty UI changes. The
index at that stage was not that prepared package until explicitly reviewed/staged
under separate publication authority. Read-only validation found **102 actual
indexed text files**, an empty staged diff and **138 current public source files**,
including **36 not yet indexed** files. Forbidden private paths/binaries were absent
from both sets; the indexed environment template contains placeholders only.
Independent phase12 docs/source-only acceptance is complete through the manager's
final review, not a nested review by the implementing engineer. Private
projects/DBs/sidecars, credentials,
local instructions/plans/sessions, exports/backups/artifacts and graph outputs
must remain excluded. No source was staged, committed, pushed or released here.
