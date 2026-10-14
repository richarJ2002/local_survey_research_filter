# Local Survey Research Filter
<!-- Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group. -->

A local-first survey tool: find or enter papers, screen titles and abstracts,
complete references, then explore and export your selected literature.
Python 3.12, Django 5.x, one disk SQLite store per project and local Plotly.
No login, React/npm, PDF processing or external graph-layout service.

## Start

With Python **3.12** and its venv support, run from this checkout:

```bash
python3.12 launch.py
```

Open the printed loopback URL, normally **http://127.0.0.1:8000/**, in Brave.
First launch installs existing `requirements.txt` into a private checkout-specific
XDG-cache environment; internet or available wheels/cache is needed once.
Successful repeats skip pip. Local work then runs offline; no preparatory
`migrate` is needed. `--port 8001` selects another port if 8000 is busy.
`launch.py` parses options; `config.launcher` owns runtime and worker setup.
JavaScript and native Web Locks are required for editable project tabs.

## New or Load

- **New:** enter the project name and an absolute existing parent folder.
  Leave destination override blank for a lowercase name-derived child
  (spaces become `_`; literal `&` and Unicode remain), or choose an override.
  **Preview destination**, inspect its resolved path, confirm, then create/open.
  Populated destinations are never overwritten. The DB stays `project.sqlite3`.
- **Load:** choose the verified project **folder**, not its SQLite filename.
  Load never creates, converts, repairs or upgrades a missing/incompatible store.
- **Remember this folder** selects the parent listing project folders, not a
  global active project. Symlinked/external folders show resolved locations.
- Projects have separate stores/workers. Multiple tabs have independent handles.
  **Projects (Exit)** releases only your tab; dirty drafts prompt before discard.
  Submitted saves survive tab/worker closure.

## Configure, then find papers—or stay local

1. Open **Configure search criteria**.
   Enter keywords, optional years/venues/authors and providers, then choose
   **Save search settings**. Only one current configuration is editable/runnable.
2. For old settings, explicitly **Use these settings** or confirm the editable
   **Use combined settings** preview. Historical settings, runs and decisions stay.
   Query-only syntax needs deliberate translation before combining; combined
   settings are not a union-of-historical-query-results engine.
3. For online search, open the owning manager's **Settings**. Supply OpenAlex
   email for the original providers, or Elsevier credentials for Scopus-only
   search, then **restart the launcher to apply**.
   Ordinary startup does not load source `.env`; deliberate compatibility uses
   `launch.py --legacy-env /absolute/path/.env`. Credentials never enter project DBs.
4. **Find papers now** requests selected providers and adds per-source run history.
   Failed providers are reported separately, not disguised as an empty success.
5. Offline, **Add papers by hand** saves entered details locally;
   choose a current keyword and the real discovery origin. DOI opens a local
   preview; **Fetch DOI metadata — online** is a separate POST action.
   A fetched preview is saved only by **Save entered details**. Saves never auto-fetch.

### Optional Scopus search (Elsevier)

In manager **Settings**, enter your **Elsevier Scopus API key** and, if supplied
by Elsevier, an **institutional token**. Blank credential fields keep saved
values; Elsevier secrets are never redisplayed or stored in project databases.
Restart the launcher, then explicitly tick **Scopus (Elsevier)** in
**Configure search criteria**.
Scopus stays off for existing/default searches. Scopus-only search does not
need an OpenAlex email; mixed searches retain the existing email requirement.

An account/API key alone does not grant API access. Check your institutional
network/token and [Elsevier entitlement](https://dev.elsevier.com/support.html),
and confirm that your [API agreement](https://dev.elsevier.com/policy.html)
permits local retention/export for this research before selecting Scopus.
The adapter retrieves STANDARD bibliographic search metadata, including the
first author where available, DOI, year and Scopus identifiers/record links.
Scopus abstracts/references are not fetched; existing metadata completion and
screening rules remain. Queries, discoveries and failures are audited per
provider; a Scopus access/quota failure does not stop the other providers.

## Paper screening

The project page is ordered **Search → Paper screening → Charts**.
Navigation is **View Papers**, **Reference completion**, **Title round**,
**Abstract round**, **Final metrics**. This does not change decision chronology:

- **Title:** a title alone is enough. Choose **Yes / No**; missing metadata
  never automatically rejects a titled paper.
- **Abstract:** review after Title Yes. Paste a missing abstract or complete
  unknown fields, then **Save entered details**; saving never accepts it for you.
- **References, if needed:** after Abstract Yes, paste the bibliography, explicitly
  fetch missing details online, or reject with an audited Abstract rejection.
- **Final kept:** Abstract accepted **and** reference identifiers or nonblank
  bibliography available. Reference-pending papers are not final or rejected.
  Normal Yes/No decisions are immutable; explicit corrections are audited.

## Overview, charts and exports

- **Paper overview** is a searchable/status-filtered 25-row dialog with local
  Manage links. Rejected, undated, unqueued and reference-pending papers remain
  reachable. **View Papers** is the normal fallback. Clear filters for no matches.
- The results table distinguishes **Records found** (historical hits, including
  repeats), **Unique papers**, **Final kept**, and **Awaiting references**.
   Old settings and run audits remain stored; the dashboard has no history section.
- All six charts have inline **Update chart** controls retained in downloads.
   Updating stays at the current scroll position. Each chart, including the curated
   graph, has **PNG export settings**: labels, fonts in points, size in millimetres
   and DPI, with a low-resolution preview. **Download PNG** renders a fresh image
   with the selected pixel dimensions and PNG resolution metadata. IEEE Access
   and IEEE Robotics Society presets are editable starting styles, not compliance
   guarantees. Settings stay in the browser and do not change research records.
  Year points mean **IN** a year; running totals mean **THROUGH**, including earlier
  years outside a displayed window. Undated/reference-pending final entries are excluded.
- The final citation lens never falls back to Title/found. Stored identifiers/cache
  edges join only project papers; bibliography prose does not invent links.
  Green = selected, grey = joiners, blue = branches. Prune changes display only.
  Graphs initially fit all displayed nodes, just like **Reset view**; an empty
  curated graph has a blank, axis-free canvas.
  **Fetch citation links — online (up to 20 lookups)** is explicit and bounded.
  Ordinary figures, popups and updates make no provider requests.
- The final project section and Full Charts include the independent **curated survey graph**: editable snapshots,
  manuscript [x] labels and directed links/cycles, never canonical screening edits.
- Download Markdown, final/citation references or offline HTML charts. Plotly PNG,
  pan/zoom/reset remain available. Reports are not reloadable project saves.

## Saving, copy and recovery

Submitted edits commit immediately to disk SQLite; unsubmitted drafts do not.
**Save project copy…** opens the owning manager in another tab; preview/confirm
its destination. Copy produces two portable files and a **new UUID**, including
committed history, while the source may remain open—not a save of unsent drafts.
**Backup** preserves UUID. For Backup/Restore, **Exit all affected tabs, then
Close worker**. Restore needs a same-ID backup and retains the previous folder.
Never raw-copy an open DB, delete the original to fake one project, or Load
same-ID copies together. Research text/history are included when sharing;
exports/credentials/app state are not. Review privacy and provider reuse rights.

## Existing stores, reference and limits

The additive current-search migration does not adopt historical settings.
Existing stores need a verified backup and explicit closed-copy upgrade using
matching pre-upgrade tools; retain originals and rollback packages.
New tooling refuses unupgraded stores. There is no automatic Load migration.
Linux/local filesystems are the supported scope. Power-loss, network filesystems,
cross-platform and newcomer comprehension need separate qualification evidence.
Ctrl+C drains owned workers only; startup never stops an unrelated listener.

[Implementation and detailed operations](REFERENCE.md) · [Legacy conversion](LEGACY_CONVERSION.md) ·
[Historical release evidence](RELEASE_NOTES.md) · [MIT licence](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
