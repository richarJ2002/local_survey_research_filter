# Local Survey Research Filter
<!-- Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group. -->

Local-first tool to turn a messy literature search into a fast, auditable survey workflow.

Pick any topic, search OpenAlex, Semantic Scholar, Crossref and arXiv, then screen papers Yes / No — first by title, then by abstract — with an auditable citation graph.

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

## Quickstart

Requires Python 3.12.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

cp -n .env.example .env  # First setup only; do not overwrite an existing .env
# Edit .env and set OPENALEX_EMAIL=you@example.com
# (OpenAlex polite pool — faster, no key needed. Restart server after editing.)

.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver 8000
```

Open http://localhost:8000/:

1. `/settings/` — add your email on first run (saved to local `.env` only, never in the DB).
2. New project → New search (e.g. `"battery degradation" AND lithium`, years 2015–2026) → Run search.
3. Start screening → `←` No / `→` Yes.
4. Download Markdown from the project page, or:
   ```bash
    .venv/bin/python manage.py export_markdown <project_id>
   ```

Search is blocked until the email is set.

### Private storage and existing databases

All projects share **`projects/db.sqlite3`**, the canonical local SQLite database.
`projects/` (including backups, logs and default `projects/exports/` Markdown
exports) is gitignored. `--output /chosen/path.md` explicitly overrides the export
destination. Credentials stay in the ignored root `.env`; never upload either
file or your survey data. This is a local single-user app, not a public server.

For an older checkout with a root `db.sqlite3`, **stop its server before upgrading**.
Back up the database with SQLite's backup API (which includes committed WAL data),
place the verified copy at `projects/db.sqlite3`, run `migrate`, then restart.
Do not run against a new empty database and mistake it for your existing survey.
Keep the original and a recoverable backup until records and decisions are verified.
An optional `LOCAL_SURVEY_DB=/absolute/path/to/isolated.sqlite3` environment override
uses a separate verification database; its parent directory must already exist.

### Curated survey-writing graph

**Manage curated survey graph** on the project dashboard opens a separate graph.
The existing automatic **How your kept papers link together** chart remains the
draft, governed by discovery and screening. Curated entries are editable snapshots:
changing or removing one never overwrites canonical paper metadata, discovery
history, keyword assignments or screening decisions.

1. Choose **Add survey entry / look up DOI**. DOI lookup is optional; it prefills
   cached/library or provider metadata, and every visible detail can be overwritten.
   When lookup is unavailable, start a blank manual entry. Saving does not repeat
   provider requests.
2. Choose a positive manuscript reference number, unique within this project,
   and the discovery origin. Circles display the year and **[x]**, or **?** for an
   unknown year. Duplicate numbers/DOIs are rejected; stale edits must be reloaded.
3. Select a citing entry and its cited entry to add a directed manual link. Remove
   manual links independently, or remove the curated entry. Cycles and multiple
   parents are allowed: this is a citation network, not a forced tree.

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
remain historical and are labelled **Unspecified origin (legacy)** rather than
being silently assigned to a guessed provider.

The save is atomic: a validation error saves no paper, keyword, assignment or
status changes. The metadata preview expires after 30 minutes; use **Retry
lookup** if it expires. A title is sufficient for Title screening. Missing
abstracts and references are completed at their later screening steps.
Provider references without a resolvable identifier cannot produce a citation
link, and provider outages cannot be repaired by editing metadata in this panel.
The separate manual-entry fallback is available when no provider record exists.

Keyword assignments and screening decisions belong to the current project.
Changing an assignment preserves original discovery history; a shared paper's
metadata and other projects' decisions are not changed.

Pasted bibliographies are retained in full. DOI, OpenAlex, S2 and modern arXiv
identifiers are extracted where possible; unresolved text does not invent
citation edges. Completing a shared paper's missing metadata supplies that same
paper's bibliography to its other projects, without changing their decisions.

The running-total chart uses adaptive 1-, 2-, 5- or 10-year axis intervals (maximum
10). The underlying yearly observations are not grouped into decade bins. The
main funnel no longer has an Incomplete category; reference-related rejects are
Abstract rejects, and reference-pending papers are included in the pending lane.
Pending counts come from actual screening decisions. Stored records with no
title/queue entry are identified in a separate note, not labelled as pending.

After updating the code, always re-run `.venv/bin/python manage.py migrate`:
new database tables are added over time, and pages will fail with
`OperationalError: no such table` until the migration is applied.

### Running from an existing checkout

After moving the project directory, an already-active shell may still point at
the old virtual environment, even though the prompt says `(.venv)`. Run
`deactivate` in that shell if it is defined, then from this project directory:

```bash
source .venv/bin/activate
hash -r
python -m django --version
python manage.py runserver 8000
```

Use `python manage.py -h` for local command examples, or
`python manage.py help` for Django's full command list. A port number alone
is not a command: use `runserver 8000`, not `python manage.py 8000`. The
`activate` command by itself does not activate this virtual environment.

## Project layout

```
config/              Django settings/urls (single settings.py, reads .env)
literature/
  models.py          Projects, papers, discovery/audit records, screening and project keyword assignments
  providers/         OpenAlex, Semantic Scholar, Crossref and arXiv clients
  services/          search/dedup/screening/export/citation logic
  views.py / urls.py Project, search, screening, settings, export pages
  templates/         Server-rendered HTML + vanilla JS (no React/npm)
  tests/             Mapping, dedup, screening, view smoke tests
projects/           Private, gitignored data, backups and default exports
  db.sqlite3        One shared local truth; Markdown is generated from it
.env                Your email/keys (gitignored). Never stored in the DB.
```

## Testing

```bash
.venv/bin/python manage.py test literature -v 2
node literature/tests/chart_popup.test.cjs  # Optional Node stdlib UI-state check; no npm
```

The Django runner covers provider mapping, deduplication, screening, enrichment, citations, plots and views.

Note: plain `pytest` autodiscovery can pick up a ROS `launch_testing` plugin — use the Django runner above.

## Limits (MVP)

- Citation paths currently join papers already discovered in the project. The graph starts from selected papers, newest first, and follows their references backwards by the chosen number of citation steps. Use the project page's OpenCitations button to fetch the next 20 DOI citation lookups (incoming and outgoing); results are cached for 30 days. No network calls are made when viewing charts.
- Metadata-provider outages do not exclude titled papers from screening. Missing abstracts/references can be supplied manually. OpenCitations improves graph edges but does not supply abstracts or replace bibliography review.
- arXiv's public Atom API needs no API key; use the polite 3-second request pacing already built into the provider.
- No login (local single user), no Maybe button, no PDFs, no BibTeX/CSV yet.
- Automatic dedup matches DOI → OpenAlex ID → normalized title; same-title groups can also be reviewed manually.

## License

Source code is licensed under the [MIT License](LICENSE), copyright 2026
Aerospace Engineering Research & Operation Systems (AERO-Sys) group. Private
projects, credentials, local instructions and conversation exports are not part
of the published source.
