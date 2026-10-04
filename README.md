# Local Survey Research Filter
<!-- Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group. -->

Local-first tool to turn a messy literature search into a fast, auditable survey workflow.

Pick any topic, search OpenAlex, Semantic Scholar, Crossref and arXiv, then screen papers Yes / No — first by title, then by abstract — with an auditable citation graph.

**Per-project Linux application; synthetic/code-qualified.** Ordinary startup
opens the New / Load manager, never the original shared database. Existing
converted stores are opened with Load; startup does not convert or synchronise
databases. Live adoption is a separate operator-controlled operation, not a
condition inferred from a developer page or these source qualification results.
See [RELEASE_NOTES.md](RELEASE_NOTES.md) for evidence and unqualified platforms.

## Workflow

```
Project → Search → Deduplicate/enrich → Title → Abstract → Complete references → Final selection/export
```

- **Title:** every paper with a title is admitted, even without an abstract or references.
- **Abstract:** if no abstract was found, paste it and select **Save missing details**, then screen the same paper. An existing abstract is not overwritten. A paper without an obtainable abstract can be explicitly rejected here.
- During Abstract screening, **Complete other unknown details** allows partial entry of missing authors, publication date/year, venue, publisher, DOI, keywords, citation count and bibliography. Known values stay read-only; only the confirmed audience-only provider tag is treated as a missing abstract. Saving details never automatically accepts a paper.
- **Reference completion:** papers passing Abstract content screening without a bibliography enter **Complete missing references**, linked from the project page. Paste the bibliography (including entries without DOIs), retry the providers, or reject the paper. Reference-related rejection is recorded as an audited **Abstract rejection**.
- **Final selection:** requires Abstract acceptance and a provider or manually supplied reference list. Papers awaiting references are pending, not rejected or finally selected. They do not seed the final citation graph or appear in the final reference export yet.
- Normal screening decisions are **Yes / No only** and locked once made. Explicit corrections, including reference-completion rejection, are audited. Manual abstract/reference provenance is recorded with timestamps.

## Linux one-command launch

With **Python 3.12**, its stdlib `venv`/`ensurepip` support, and a writable local
Linux filesystem already available, run from the checkout:

```bash
python3.12 launch.py
```

The familiar command also starts this same foreground manager:

```bash
python manage.py runserver 8000
```

Use Python 3.12 for either command. `runserver` accepts an omitted port (8000),
a port, `localhost:port` or `127.0.0.1:port`, and `--help`. It binds IPv4 loopback
only. Old Django flags such as `--noreload`, `--nostatic` and non-loopback binds
are rejected rather than dropped. Use `launch.py` for `--install` or
`--legacy-env`; explicit developer/rollback server options are described below.
There is no preparatory bare `manage.py migrate` step for ordinary launch.

Open the printed loopback URL (default http://127.0.0.1:8000/). Choose **New** or
**Load**, then type an absolute project-folder path. JavaScript and native **Web
Locks** on the loopback secure context are required for editable tab handles;
without JavaScript, native New/Load work but project pages are read-only. New creates one
project database only when submitted; Load never creates or migrates a missing,
old or incompatible store. No research is seeded or legacy database opened at
launch. Legacy conversion is a separate approved operation; see
[LEGACY_CONVERSION.md](LEGACY_CONVERSION.md).

- First launch creates a checkout-specific isolated environment under
  `$XDG_CACHE_HOME/local-survey-filter/envs/` (default `~/.cache/`). It installs
  **only the existing `requirements.txt`**, without global pip, sudo or an upgrade
  flag. Internet or a populated pip cache is needed for this installation.
- Successful repeated launches do not run pip. Local browse/edit/screen/charts,
  New/Load and copy/recovery tools work offline; Plotly and static files are local.
  Email/keys are needed only for explicitly online provider actions. Configure
  them at the manager's Settings page, then restart the launcher to apply.
- Registry, workspace preferences, `credentials.env` and the persistent Django
  secret live under `$XDG_DATA_HOME/local-survey-filter/` (default
  `~/.local/share/`), **outside source/project packages**. The manager uses an
  explicit private database path and never migrates a shared research database.
  XDG paths must be absolute, outside the checkout and existing project packages.
  Keep one XDG profile per concurrently opened store.
- An existing source `.env` is neither loaded nor overwritten by default.
  Deliberate compatibility: `python3.12 launch.py --legacy-env /absolute/path/.env`.
  This opts into reading that file **and Settings saves to that selected file**;
  it does not copy credentials into a project. Environment variables retain their
  usual precedence over dotenv values.
- Ctrl+C/SIGTERM stops admission and waits for accepted operations/transactions,
  then reaps only this supervisor's child workers. It never signals registry PIDs
  or kills a busy-port listener. Lost browser tabs are swept periodically, without
  requiring a landing-page visit. A supervisor crash closes its liveness pipe;
  its workers stop admission and finish accepted edits before exiting themselves.
  Reopen after the drain completes; a still-owned/busy store refuses duplicate
  worker launch. **Close worker** can clear stale runtime rows after physical
  closure is proved. This is not power-loss durability for uncommitted work.

Use `--port 8001` for a busy port. Space-containing paths are supported; quote
them in shell commands. Missing Python/venv support, dependencies, unwritable
paths, busy ports and incompatible project schemas produce errors, never a
silent shared-store fallback. Installation failures can be retried. Changed
requirements or damaged dependencies require an explicit `--install` repair;
ordinary repeat launch never silently upgrades them. Do not delete a private
environment while its launcher is running. This is a **local single-user Linux
tool**, not a public/multi-user deployment. Network filesystems and abrupt
machine/power failure have not been qualified.

## New, Load, workspace and Exit

1. Optionally **Remember this folder** with an existing workspace path. Discovery
   lists recognised project folders one level deep, not a recursive disk scan.
   The remembered root is a preference, not a global active project; typed Load
   can select a folder outside it. There is no last-selected-project selector.
2. **New → Create and open** needs a name and an absent or empty folder with an
   existing writable parent. It creates only that store, never overwrites a
   populated folder, and does not add demo papers. **Load → Open project** needs
   the folder, not its SQLite filename. Missing, malformed, newer/older or
   schema-incompatible stores refuse without creating or upgrading them.
3. Submit each edit to commit it immediately to that project's disk SQLite.
   Unsubmitted form text is a browser draft, not an autosaved database. Different
   projects may reuse paper IDs/DOIs but do not share mutable research records.
4. Multiple tabs of one project reuse its worker but own independent handles.
   Reload, redirects and internal Back retain the tab's handle. **Exit**, the
   Projects/home links and an ordinary Back across the manager boundary release
   only that tab. A dirty draft prompts before discard; cancellation keeps it.
   Failed release keeps the draft/handle and page: retry Exit after the error is
   resolved. An already-released handle can be released again successfully.
5. A tab crash/close does not undo submitted saves; lease expiry/idle sweeping
   handles abandoned runtime use. Reopen through Load if a handle expires or an
   old worker URL refuses. Do not treat unload events as a save or close protocol.

Symlinked roots, folders, manifests and databases may point outside the workspace.
Preview the resolved location before an operation. Aliases of the same physical
store are deduplicated; a same UUID at another physical store is a collision,
not a merge. Use **Save Copy** to create an independent identity, not raw folder
duplication. Broken/looping links and unreadable/unwritable locations refuse.

## Storage and isolation

```text
python3.12 launch.py (foreground, loopback only)
  └─ Manager: New/Load, locations/preferences, copy/recovery, owned workers
       ├─ Worker A → fixed Survey A/project.sqlite3
       │    └─ tab A1 + tab A2: independent disposable handles
       └─ Worker B → fixed Survey B/project.sqlite3
            └─ tab B1
     URLs bind project UUID + worker epoch; edits also require a live tab handle
     and genuine CSRF. No request-time database switching or global active project.

Chosen workspace/                  # optional; external symlinks allowed
  Survey A/
    project.json                   # version 1, UUID, fixed DB member, creation time
    project.sqlite3                # one Project + owned research/history/cache/curation
    exports/                       # optional generated files, not reloadable truth
    backups/                       # optional private recovery, not shared by default

$XDG_DATA_HOME/local-survey-filter/ # default ~/.local/share/local-survey-filter/
  registry.sqlite3                 # locations, workers, leases; no research tables
  settings.json                    # workspace preferences
  credentials.env, django-secret   # private; never project-package members
$XDG_CACHE_HOME/local-survey-filter/envs/<checkout-key>/
                                   # installed runtime, not project data
```

SQLite on disk is authoritative: Open does not load an editable master copy into
RAM and Exit does not delete files or discard committed data. Research-adjacent
citation caches belong to the project DB; application state/credentials do not.

## Save Copy, share, Backup and Restore

From an open portable project, click **Save project copy…** in the top navigation
or saving card. It opens the owning manager in a **new tab**, with the current
project folder and Save Copy preselected. The original tab keeps its drafts and
handle; drafts are not included in the copy. Choose a destination and confirm.
**Backup / Restore / manage project…** opens the same management panel; select
the required action there. Submitted edits are already committed immediately—
Save Copy makes a separate portable folder, not a save of unsubmitted form text.

Explicit `manage.py runserver --legacy-shared-db` pages show a **Legacy shared-DB
mode** developer/rollback indicator and New/Load manager instructions. They do
not infer whether conversion has happened elsewhere. Load a previously converted
and verified folder in the manager for portable saving; developer edits do not
update that converted store. Opening either page does not convert or copy data.

Use the manager's **Save Copy / Backup / Restore** form. Supply absolute source
and destination folders and confirm the resolved paths. Destination parents must
already exist; choose an **absent or empty real directory** for Save Copy/Backup.
Populated directories and symlink destinations refuse. Legacy conversion and the
synthetic sample command are stricter: their destinations must be absent.

| Action | Source / destination | Identity and closure |
|---|---|---|
| Save Copy | Project → new copy folder | New UUID; source may stay open; committed saves only |
| Backup | Project → new backup folder | Same UUID; source must be closed |
| Restore | Backup folder → existing original project folder | Same UUID required; both closed; previous folder retained |
| Close worker | Project folder; no destination | Exit all its tabs first; never implicitly closes another tab/manager |

For Backup/Restore, **Exit all affected tabs, then Close worker in the owning
manager**. A missing registry row is not proof of closure. Unmanaged SQLite
editors and old developer servers must be closed separately by their operator;
the launcher cannot prove they are gone. Do not run two XDG profiles/users on the
same concurrently open store.

A successful copy/backup package contains exactly **two regular files**:
`project.json` and `project.sqlite3`. SQLite's backup API includes committed WAL
data from a pinned read snapshot, not uncommitted drafts/writes; it does not raw
copy an open database or force a source checkpoint. Required symlinks are
materialised. Exports, backups, unrelated files, manager registry/leases/settings,
credentials and authentication/session rows are excluded. Research notes, raw
provider payloads and history **are included**: review their contents and data
reuse rights before sharing; this is not research anonymisation.

Send only a completed Save Copy package; the recipient installs the source and
Loads that folder. Edits in the copy do not affect the sender. Markdown and HTML
charts are generated reports, **not** complete reloadable saves. Standalone charts
render offline with inline Plotly; app paper-list links/node popups still need the
running app. Include [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) when distributing
the chart bundle. Regenerate reports after loading a copy.

Publication of a package is staged, verified and atomically renamed on supported
Linux storage. Restore atomically exchanges folders and reports a sibling such as
`.Survey A.recovery-<uuid>` containing the entire prior folder. Keep it until the
restored records are verified. Interrupted/failed operations retain private staging
for explicit inspection, never automatically delete/merge/retry over user data.
A recovery sibling has the old identity: do not Load it beside the restored store
and expect an independent project. Use Save Copy if an independent ID is needed.
Do not share incomplete staging or a raw legacy backup.

## Offline work and current state versus history

After installation, browse/edit/screen/curate, local graphs, New/Load, copying and
recovery require no internet. Plotly comes from the installed Python dependency,
not a CDN; rendering, revision refresh and saving a cached preview do not fetch
providers. Explicit search, uncached DOI lookup, reference retry/enrichment and
OpenCitations requests need internet and may fail independently by source.
OpenAlex email gates online search, not local work. DOI/provider links are external.

**Current papers by source** uses distinct currently discovered papers and the
same displayed source values as the table. Each multi-source paper counts once
per source bar, so bars can sum above the unique-paper total. Blank means current
unknown source; a legacy literal `manual` displays blank without erasing its audit.
Default dashboards and chart downloads show current source bars, not historical
source pies. Select **Audit / History** on the charts page to view or download
the historical charts separately (`?lens=history`).
The audit/history charts **Raw records by source — historical imports** and
**Duplicate removals by source — historical imports** use historical SearchRun
totals, including repeat hits; they do not measure current missing-source papers.
Correcting present metadata does not rewrite original discovery or import audits.

Submitted saves return to updated views. On focus/visibility, another tab of the
same project revalidates content: a clean page can reload; a dirty form shows a
stale-data notice and keeps the draft until you choose to reload. This is polling,
not a merge/conflict-resolution service or constant-time large-library counter.

### Curated survey-writing graph

**Manage curated survey graph** on the project dashboard opens a separate graph.
The existing automatic **How your kept papers link together** chart remains the
draft, governed by discovery and screening. Curated entries are editable snapshots:
changing or removing one never overwrites canonical paper metadata, discovery
history, keyword assignments or screening decisions.

The plot uses the same citation-network renderer as the automatic draft. Layout
prioritises node separation, low-crossing links and compact groups, rather than
fixed publication-year rows. Years only orient each completed group loosely;
the year labels remain accurate. All genuine links remain, including cycles.
Zero crossings cannot be guaranteed for every possible graph. The responsive
plot uses the available page width, with a minimum readable canvas and scrolling
for crowded diagrams. No packages or external graph-layout dependencies are added.
Below it
is **Add entry**, then a visible table of title, authors, year, journal, ref ID and
entry provenance. The same panel appears on the full Charts page.
Hover or click a graph node for selectable/copyable paper details; edge-midpoint
hover gives citing → cited direction. Popups do not add papers or citation links.

1. Choose **Add entry** to open a native popup with the Django editor (or an
   ordinary page fallback; worker editing still requires JavaScript). Search the
   current project's existing papers by title, author, DOI or journal, then select a result to
   fill the fields. Alternatively look up a DOI, or start with manual fields.
   Both prefills leave the ref ID blank. Every bibliographic detail can be
   overwritten to correct typos. Saving does not repeat provider requests.
2. Choose a positive manuscript reference number, unique within this project,
   and the discovery origin. It is recommended to finalise ref IDs once you know
   the manuscript's reference numbers; temporary positive numbers can be used
   meanwhile. Circles display the year and **[x]**, or **?** for an unknown year.
   Duplicate numbers/DOIs are rejected; stale edits must be reloaded.
3. In **Links to (this entry cites)**, tick any number of entries already in the
   table. **Add paper to graph** saves the snapshot and links atomically, closes
   the popup and refreshes the graph/table. **Edit / links** lets you change details
   or untick manual links. Cycles and multiple parents are allowed.
4. Use the table's bin/Delete control, then confirm removal. This removes the
   snapshot and its manual links, not the draft record or screening decisions.

**Added from** distinguishes **Original project search** from **Added while reading
papers**, separately from provider/source. Project-paper prefill uses recorded
discovery provenance; manually added library papers remain reading additions.
DOI/manual additions default to reading unless project history says otherwise;
the curator can correct the label. Existing snapshots retain **Unspecified
(legacy)** until explicitly labelled; the migration does not guess their history.

Only curated entries appear. Automatic links match stored DOI/OpenAlex/S2 identifiers,
cached arXiv aliases and previously fetched OpenCitations edges. Unresolved bibliography
text or title similarity never creates a link. An automatic match can remain after
removing a manual link. Chart rendering makes no hidden provider requests.

### Explore yearly and running-total points

On both the dashboard and **Charts**, click/tap a yearly bar or running-total point
to open a persistent preview popup with the first ten matching papers, normal hover
counts unchanged. **Open a chart point's paper list** provides native keyboard/touch
series/year controls. Close the dialog with its button or Escape; the full-list link
opens a stable, paginated page (25 papers per page), without temporary files.

Yearly bars mean published **IN** that year; cumulative points mean published
**THROUGH** that year. Lists apply the chosen Found/Title/Abstract series filter.
Abstract/final excludes papers still awaiting reference completion; all series
exclude undated papers. Lists reflect current decisions, which may change after
the plot was loaded.

### Add or update a paper by DOI

**View Papers** searches stored titles/DOIs and pages 25 records at a time.
Header links sort all matches before paging; click again to reverse. Search/sort
survive page navigation and the native page-jump control. Ascending metadata sorts
put missing values first. Multi-value source/keyword ordering uses the first
listed source or linked/first stored keyword, not a separate sort per value.
Long cells wrap in fixed-width columns with horizontal scrolling, without clipping.

From **Papers → Add a missed paper**, look up a DOI or paste a DOI resolver link.
The entry panel displays read-only title, authors, year, venue, abstract and
clickable DOI/OpenAlex/Semantic Scholar references. Stored papers use their
existing metadata; updating their entry does not fetch or overwrite it.

Select **Linked keyword**, grouped by search. Choose **Add new keyword…** to
reveal the keyword name and destination search. Then choose Title screening;
Abstract screening applies only after Title passes. New entries require explicit
choices rather than silently assigning the first keyword or accepting a paper.
Use **Add entry** for a new DOI or **Update entry** for an existing record.
New entries require **Where was this paper found?** Choose the actual provider,
publisher site, reference list or another source. Human entry is recorded as an
entry method, not a discovery source. Existing unspecified-origin audit rows
remain historical and are labelled **Unspecified at import (historical)** rather
than being silently assigned to a guessed provider. Audit/History charts count
import records (including duplicate hits); the default source bars show current papers.
Supplying a source later does not rewrite those earlier audits; View Papers
shows current source metadata and puts blank values first when sorted ascending.

The save is atomic: a validation error saves no paper, keyword, assignment or
status changes. The metadata preview expires after 30 minutes; use **Retry
lookup** if it expires. A title is sufficient for Title screening. Missing
abstracts and references are completed at their later screening steps.
Provider references without a resolvable identifier cannot produce a citation
link, and provider outages cannot be repaired by editing metadata in this panel.
The separate manual-entry fallback is available when no provider record exists.

Keyword assignments and screening decisions belong to the current project.
Changing an assignment preserves original discovery history. Each loaded project
has its own paper metadata and decisions; another project's store is not changed.

Pasted bibliographies are retained in full. DOI, OpenAlex, S2 and modern arXiv
identifiers are extracted where possible; unresolved text does not invent
citation edges. Completing missing metadata affects only this loaded project's
paper. The old developer shared-DB mode did share canonical paper metadata.

The running-total chart uses adaptive 1-, 2-, 5- or 10-year axis intervals (maximum
10). The underlying yearly observations are not grouped into decade bins. The
main funnel no longer has an Incomplete category; reference-related rejects are
Abstract rejects, and reference-pending papers are included in the pending lane.
Pending counts come from actual screening decisions. Stored records with no
title/queue entry are identified in a separate note, not labelled as pending.

## Existing databases and developer mode

The launcher never opens/converts the old shared `projects/db.sqlite3`, and Load
never auto-upgrades schemas. See [LEGACY_CONVERSION.md](LEGACY_CONVERSION.md) for
explicit inventory → approved mapping → dry-run → conversion and recovery.
For a database not yet inspected, schema eligibility needs separately approved inspection.
Live adoption needs exact path/ownership approval, a verified WAL-aware private
backup, closed writers/maintenance window, reviewed field projections and PK/FK
hash checks, then read-only adoption validation and a rollback plan. Retain the
original and backup. Synthetic success does not authorise any live step.

Ordinary `manage.py runserver` is the launcher. The old Django server requires
**explicit developer/rollback opt-in**, even when `LOCAL_SURVEY_DB` is set:

```bash
# Synthetic development only; the parent directory must already exist.
PYTHON_DOTENV_DISABLED=1 LOCAL_SURVEY_DB=/absolute/synthetic/dev.sqlite3 \
  .venv/bin/python manage.py migrate
PYTHON_DOTENV_DISABLED=1 LOCAL_SURVEY_DB=/absolute/synthetic/dev.sqlite3 \
  .venv/bin/python manage.py runserver 8001 --legacy-shared-db --noreload
```

For deliberately approved rollback to the retained original (not a restore or
merge), stop the per-project manager through its owning terminal, then select the
original **explicitly** and use the opt-in command above. Keep the converted store
and original separate: subsequent edits in either are not synchronised. Omitting
`LOCAL_SURVEY_DB` with `--legacy-shared-db` deliberately selects
`projects/db.sqlite3`; that mode may load the source `.env` unless dotenv is
disabled. Prefer the absolute DB override so the intended target is unambiguous.
Do not migrate a retained original merely to run it, or run writers against it
while checking conversion equivalence. The operator, not the launcher, closes
any pre-existing developer server; startup never kills an occupied-port listener.

Administrative commands that might write (including `migrate`, `flush`, `shell`
and custom commands) refuse before Django loads unless `LOCAL_SURVEY_DB` or
`--legacy-shared-db` is explicit. Without either, `check`, `test`, `makemigrations`
and help use an in-memory DB and do not load source credentials. `makemigrations`
can still write source migration files unless `--dry-run` is supplied. Fixed
manager/worker roles retain their explicit startup DB requirement. Do not use
`migrate` as an upgrade instruction for a loaded project. Unknown/older stores
need a separately backed-up upgrade procedure; no general project-store upgrade
command is delivered.
Developer `export_markdown <project_id> --output /chosen/report.md` also uses the
selected developer DB, not whichever project a browser tab shows. Prefer the
loaded project's download controls.

## Project layout

```
launch.py            Foreground Linux manager + owned fixed-store workers
manage.py            Ordinary runserver → launcher; explicit developer/admin targets
config/              Django settings/URLs (role and DB selected before startup)
literature/
  models.py          Projects, papers, discovery/audit records, screening and project keyword assignments
  providers/         OpenAlex, Semantic Scholar, Crossref and arXiv clients
  services/          search/dedup/screening/export/citation logic
  project_store.py   Store validation, atomic copies and recovery
  workspace*.py     Manager discovery, registry, tab leases and operations
  worker.py          Immutable DB/UUID/epoch/handle request boundary
  legacy_conversion.py Explicit approved-mapping shared-DB conversion CLI
  views.py / urls.py Project, search, screening, settings, export pages
  templates/         Server-rendered HTML + vanilla JS (no React/npm)
  tests/             Synthetic regression/HTTP/browser fixtures; no real survey demo
requirements.txt     Existing dependency ranges, not a reproducible version lock
projects/            Ignored legacy developer data; not launcher project storage
.env                 Ignored legacy opt-in credentials; not loaded by launcher default
```

## Synthetic example and developer checks

For development/tests only, create a checkout `.venv` with the declared
requirements (the launcher uses its separate private environment):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m literature.tests.synthetic_example /tmp/local-survey-synthetic-example
```

The sample command requires that destination to be **absent**, outside source
and existing projects, with an existing parent; choose another absolute path if
it already exists. It does not read live data or `.env`. It creates a complete
two-file package with three fictional papers, accepted/rejected/pending decisions,
historical unspecified origins, a revision, cached citations and curated [7], [12],
[17] entries with a directed three-cycle. Load it, edit a search/paper, Save Copy,
then Exit/Close and Backup/Restore to practise safely. No binary DB is distributed
in source; the command reuses the qualified synthetic roundtrip fixture.

Use a synthetic override for checks, not the developer default or real credentials:

```bash
PYTHON_DOTENV_DISABLED=1 LOCAL_SURVEY_DB=/tmp/survey-check-unused.sqlite3 \
  .venv/bin/python manage.py test literature -v 2
PYTHON_DOTENV_DISABLED=1 LOCAL_SURVEY_DB=/tmp/survey-check-unused.sqlite3 \
  .venv/bin/python manage.py check
PYTHON_DOTENV_DISABLED=1 LOCAL_SURVEY_DB=/tmp/survey-check-unused.sqlite3 \
  .venv/bin/python manage.py makemigrations --check --dry-run
node literature/tests/graph_node_popup.test.cjs
node literature/tests/chart_popup.test.cjs
node literature/tests/survey_entry_dialog.test.cjs
node literature/tests/paper_entry_dom.test.cjs
```

The Django runner uses isolated synthetic databases and covers provider mapping,
dedup, screening, curation, current/history charts, worker/lifecycle, copies,
conversion and launcher paths. Launcher integration tests install declared
requirements only in a temporary runtime, then deny outbound connections for
repeat launch and sample Load/edit/share roundtrip. Optional Node checks use
stdlib only, no npm. The opt-in `literature.tests.wp0111_browser` harness needs
the existing snap Firefox/geckodriver paths on the qualification host; it is not
a general cross-browser installer.

Note: plain `pytest` autodiscovery can pick up a ROS `launch_testing` plugin — use the Django runner above.

## Supported configuration and limits

- Qualified: Linux local filesystem, Python **3.12.3**, Django **5.2.17**, SQLite
  **3.45.1**, Plotly Python **7.1.0** and isolated Firefox **156.0.1** on the test
  host. These are observed versions, not pins or guarantees for future installs.
  The launcher requires Python 3.12 exactly; dependencies retain declared ranges.
- Package snapshots require Linux `/proc/self/fd`, `flock` and filesystem
  `renameat2` NOREPLACE/EXCHANGE support; unavailable support fails closed.
  Windows/macOS, network filesystems, simultaneous distinct-XDG/user cold starts,
  Chromium/user Chrome, mobile/touch and assistive-technology runs are unqualified.
- Injected disk-full/write/interruption tests are not physical power-loss, failing
  disk or actually full-storage qualification. Patient shutdown may wait for stuck
  accepted work; it does not kill it. An unreachable cancelled drain needs deliberate
  recovery. Every history-menu multi-entry jump/BFCache combination was not tested.
- Citation paths currently join papers already discovered in the project. The graph starts from selected papers, newest first, and follows their references backwards by the chosen number of citation steps. Use the project page's OpenCitations button to fetch the next 20 DOI citation lookups (incoming and outgoing); results are cached for 30 days. No network calls are made when viewing charts.
- Metadata-provider outages do not exclude titled papers from screening. Missing abstracts/references can be supplied manually. OpenCitations improves graph edges but does not supply abstracts or replace bibliography review.
- arXiv's public Atom API needs no API key; use the polite 3-second request pacing already built into the provider.
- No login (local single user), no Maybe button, no PDFs, no BibTeX/CSV yet.
- Automatic dedup matches DOI → OpenAlex ID → normalized title; same-title groups can also be reviewed manually.

## License

Source code is licensed under the [MIT License](LICENSE), copyright 2026
Aerospace Engineering Research & Operation Systems (AERO-Sys) group. Private
projects, credentials, local instructions and conversation exports are not part
of the intended source package. Plotly and other installed dependencies keep
their own licences/notices; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
The source licence does **not** grant reuse rights to abstracts, bibliographies,
provider datasets or the user's research; review applicable data licences and
provider terms before redistributing them. This documentation does not assert
a published or tagged release.
