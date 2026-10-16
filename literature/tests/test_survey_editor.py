"""
Graph-first survey editor: origins, project prefill, links, popup.

@File: test_survey_editor.py
@Brief: Cited-entries multiselect, inclusion origins, scoped prefill, popup iframe.
@Date: 02/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

# Custom library Imports
import httpx
from django.core import signing
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from literature.models import Discovery, Paper, Project, Search, SurveyCitation, SurveyEntry
from literature.services.survey_graph_service import prefill, project_origin, save_survey_entry

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Fixtures ---


def make_project(name="E"):
    project = Project.objects.create(name=name)
    project.ensure_default_stages()
    return project


def base_values(number=1, title="T", **over):
    values = {
        "reference_number": number,
        "title": title,
        "doi": "",
        "authors": "A U Thor",
        "publication_year": None,
        "venue": "V",
        "publisher": "",
        "abstract": "",
        "reference_text": "",
        "origin_source": "openalex",
        "inclusion_origin": "reading",
        "version": "",
        "preview_token": "",
    }
    values.update(over)
    return values


def make_entry(project, number=1, title="T", **kw):
    return save_survey_entry(project, base_values(number=number, title=title, **kw))


def make_discovered(project, title="Found", source="openalex", entry_method=None, doi=None):
    search = Search.objects.create(project=project, name="S", keywords=["k"])
    paper = Paper.objects.create(
        title=title, doi=doi, venue="Venue X", authors=[{"name": "Jane Doe"}]
    )
    payload = {} if entry_method is None else {"entry_method": entry_method}
    Discovery.objects.create(
        paper=paper, search=search, source=source, query="k", raw_payload=payload
    )
    return paper


# --- Classes
class OriginTests(TestCase):
    def test_provider_history_counts_original(self):
        project = make_project()
        paper = make_discovered(project, entry_method=None, source="crossref")
        origin, source = project_origin(project, paper)
        self.assertEqual((origin, source), ("search", "crossref"))

    def test_user_added_counts_reading(self):
        project = make_project()
        paper = make_discovered(
            project, entry_method="user_added", source="openalex")
        self.assertEqual(project_origin(project, paper),
                         ("reading", "openalex"))

    def test_outside_project_is_reading(self):
        project = make_project()
        other = make_project(name="O")
        paper = make_discovered(other, entry_method=None, source="openalex")
        self.assertEqual(project_origin(project, paper), ("reading", ""))

    def test_invalid_source_leaves_blank(self):
        project = make_project()
        paper = make_discovered(
            project, entry_method=None, source="legacy-source")
        origin, source = project_origin(project, paper)
        self.assertEqual(origin, "search")
        self.assertEqual(source, "")

    def test_form_absent_origin_defaults_reading_keeps_legacy(self):
        from literature.forms import SurveyEntryForm

        project = make_project()
        form = SurveyEntryForm(
            {"reference_number": "1", "title": "T", "origin_source": "openalex"}, project=project
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["inclusion_origin"], "reading")
        entry = make_entry(project, number=9, title="Legacy")
        entry.inclusion_origin = "unspecified"
        entry.save()
        form2 = SurveyEntryForm(
            {
                "reference_number": "9",
                "title": "Legacy",
                "origin_source": "openalex",
                "inclusion_origin": "unspecified",
            },
            project=project,
            entry=entry,
        )
        self.assertTrue(form2.is_valid())
        self.assertEqual(form2.cleaned_data["inclusion_origin"], "unspecified")


class PrefillTests(TestCase):
    def test_project_library_prefill_keeps_number_blank_and_links_paper_without_doi(self):
        project = make_project()
        paper = make_discovered(
            project, title="No DOI Paper", doi=None, entry_method="user_added")
        out = prefill(project, "", {}, paper)
        self.assertEqual(out["title"], "No DOI Paper")
        self.assertEqual(out["inclusion_origin"], "reading")
        self.assertNotIn("reference_number", out)
        entry = save_survey_entry(
            project,
            {
                **base_values(number=1, title="x"),
                **out,
                "reference_number": 1,
                "origin_source": out["origin_source"] or "openalex",
            },
        )
        self.assertEqual(entry.paper_id, paper.pk)

    def test_doi_prefill_blank_number_and_no_library_mutation(self):
        project = make_project()
        before = list(Paper.objects.values("id", "title"))
        out = prefill(project, "10.1000/new",
                      {"title": "New", "authors": []}, None)
        self.assertEqual(out["inclusion_origin"], "reading")
        self.assertNotIn("reference_number", out)
        self.assertEqual(before, list(Paper.objects.values("id", "title")))

    def test_signed_snapshot_nonmutation(self):
        project = make_project()
        paper = make_discovered(project, title="Orig",
                                doi="10.1000/orig", entry_method=None)
        snap = (paper.title, Discovery.objects.count())
        data = prefill(project, "10.1000/orig", {}, paper)
        data.update(
            {
                "reference_number": 1,
                "origin_source": data["origin_source"] or "openalex",
                "title": "Edited",
                "version": "",
                "publication_year": None,
                "authors": "E",
                "venue": "",
                "publisher": "",
                "abstract": "",
                "reference_text": "",
            }
        )
        entry = save_survey_entry(project, data)
        self.assertEqual(entry.title, "Edited")
        paper.refresh_from_db()
        self.assertEqual((paper.title, Discovery.objects.count()), snap)


class CitedEntriesTests(TestCase):
    def test_create_edit_replace_preserve_incoming(self):
        project = make_project()
        a = make_entry(project, number=1, title="A")
        b = make_entry(project, number=2, title="B")
        c = make_entry(project, number=3, title="C")
        SurveyCitation.objects.create(citing=a, cited=c)
        # create D citing A,B
        d = save_survey_entry(project, base_values(
            number=4, title="D", cited_entries=[a.pk, b.pk]))
        SurveyCitation.objects.create(citing=a, cited=d)
        self.assertEqual(set(d.manual_outgoing.values_list(
            "cited_id", flat=True)), {a.pk, b.pk})
        # edit D to cite B,C (replace outgoing, keep incoming untouched)
        version = signing.dumps(
            [d.pk, d.updated_at.isoformat()], salt="survey-version")
        d2 = save_survey_entry(
            project,
            base_values(number=4, title="D2", cited_entries=[
                        b.pk, c.pk], version=version),
            entry_id=d.pk,
        )
        self.assertEqual(set(d2.manual_outgoing.values_list(
            "cited_id", flat=True)), {b.pk, c.pk})
        # incoming preserved: B cited by D still
        self.assertTrue(SurveyCitation.objects.filter(
            citing=d2, cited=b).exists())
        self.assertTrue(SurveyCitation.objects.filter(
            citing=a, cited=d2).exists())
        version_a = signing.dumps(
            [a.pk, a.updated_at.isoformat()], salt="survey-version")
        save_survey_entry(
            project,
            base_values(number=1, title="A",
                        version=version_a, cited_entries=[]),
            entry_id=a.pk,
        )
        self.assertFalse(a.manual_outgoing.exists())

    def test_cycles_allowed_self_crossproject_rejected(self):
        project = make_project()
        other = make_project(name="O")
        a = make_entry(project, number=1, title="A")
        b = make_entry(project, number=2, title="B")
        foreign = make_entry(other, number=1, title="F")
        save_survey_entry(
            project,
            base_values(
                number=1,
                title="A",
                cited_entries=[b.pk],
                version=signing.dumps(
                    [a.pk, a.updated_at.isoformat()], salt="survey-version"),
            ),
            entry_id=a.pk,
        )
        version_b = signing.dumps(
            [b.pk, b.updated_at.isoformat()], salt="survey-version")
        save_survey_entry(
            project,
            base_values(number=2, title="B", cited_entries=[
                        a.pk], version=version_b),
            entry_id=b.pk,
        )
        self.assertEqual(SurveyCitation.objects.filter(
            citing__project=project).count(), 2)
        with self.assertRaises(ValueError):
            save_survey_entry(project, base_values(
                number=3, title="C", cited_entries=[foreign.pk]))
        self.assertFalse(SurveyEntry.objects.filter(title="C").exists())
        with self.assertRaises(ValueError):
            save_survey_entry(project, base_values(
                number=4, title="S", cited_entries=[999999]))
        with self.assertRaises(ValueError):
            v = signing.dumps(
                [a.pk, SurveyEntry.objects.get(
                    pk=a.pk).updated_at.isoformat()],
                salt="survey-version",
            )
            save_survey_entry(
                project,
                base_values(number=1, title="A",
                            cited_entries=[a.pk], version=v),
                entry_id=a.pk,
            )

    def test_old_callers_without_key_preserve_links(self):
        project = make_project()
        a = make_entry(project, number=1, title="A")
        b = make_entry(project, number=2, title="B", cited_entries=[a.pk])
        self.assertTrue(SurveyCitation.objects.filter(
            citing=b, cited=a).exists())
        version = signing.dumps(
            [b.pk, b.updated_at.isoformat()], salt="survey-version")
        vals = base_values(number=2, title="B2", version=version)
        vals.pop("cited_entries", None)
        vals.pop("inclusion_origin", None)
        kept = save_survey_entry(project, vals, entry_id=b.pk)
        self.assertEqual(kept.title, "B2")
        self.assertTrue(SurveyCitation.objects.filter(
            citing=b, cited=a).exists())

    def test_rollback_on_bad_links(self):
        project = make_project()
        a = make_entry(project, number=1, title="A")
        with self.assertRaises(ValueError):
            save_survey_entry(
                project, base_values(number=2, title="Bad",
                                     cited_entries=[a.pk, 424242])
            )
        self.assertFalse(SurveyEntry.objects.filter(title="Bad").exists())

    def test_graph_link_bump_invalidates_stale_editor(self):
        project = make_project()
        a = make_entry(project, number=1, title="A")
        b = make_entry(project, number=2, title="B")
        stale = signing.dumps(
            [a.pk, a.updated_at.isoformat()], salt="survey-version")
        self.client.post(
            f"/project/{project.pk}/survey/",
            {"action": "link", "citing": str(a.pk), "cited": str(b.pk)},
        )
        self.assertTrue(SurveyCitation.objects.filter(
            citing=a, cited=b).exists())
        with self.assertRaises(ValueError):
            save_survey_entry(
                project, base_values(number=1, title="Stale", version=stale), entry_id=a.pk
            )
        link = SurveyCitation.objects.get(citing=a, cited=b)
        fresh = signing.dumps(
            [a.pk, SurveyEntry.objects.get(pk=a.pk).updated_at.isoformat()], salt="survey-version"
        )
        self.client.post(
            f"/project/{project.pk}/survey/", {"action": "remove_link",
                                               "link_id": str(link.pk)}
        )
        with self.assertRaises(ValueError):
            save_survey_entry(
                project, base_values(number=1, title="Stale2", version=fresh), entry_id=a.pk
            )


class EditorViewTests(TestCase):
    def test_graph_then_add_then_visible_table_on_both_pages(self):
        project = make_project()
        entry = make_entry(
            project,
            number=12,
            title="Curated title",
            authors="One Author\nSecond Author",
            publication_year=2025,
            venue="Research Journal",
            inclusion_origin="search",
        )
        for route in ("survey_graph", "plots_view", "project_detail"):
            response = self.client.get(reverse(route, args=[project.pk]))
            self.assertContains(response, "Curated title")
            self.assertContains(
                response, "One Author<br>Second Author", html=False)
            self.assertContains(response, "Research Journal")
            self.assertContains(response, "Original project search")
            self.assertContains(response, "Are you sure")
            self.assertContains(
                response, f"Yes, delete entry [{entry.reference_number}]")
            self.assertContains(response, "survey_entry_dialog.js")
            html = response.content.decode()
            self.assertLess(html.index('id="curated-survey-plot"'),
                            html.index(">+ Add entry</a>"))
            self.assertLess(
                html.index(
                    ">+ Add entry</a>"), html.index('id="curated-survey-entries"')
            )
            for column in ("Title", "Authors", "Year", "Journal", "Ref ID", "Added from"):
                self.assertContains(response, f'<th scope="col">{column}</th>')

    def test_library_and_doi_controls_stay_inside_popup(self):
        project = make_project()
        paper = make_discovered(project, title="Search hit")
        response = self.client.get(
            reverse("survey_entry_add", args=[project.pk]), {
                "q": "Search", "popup": "1"}
        )
        self.assertContains(response, f"paper_id={paper.pk}&amp;popup=1")
        self.assertContains(response, 'name="popup" value="1"')
        self.assertContains(response, "Save entered details")
        self.assertContains(response, "set it later under the graph")
        self.assertContains(response, "Links to (this entry cites)")
        self.assertContains(response, "survey-lookup-doi")
        self.assertEqual(
            response.context["form"]["reference_number"].value(), None)
        redirect = self.client.post(
            reverse("survey_entry_add", args=[project.pk]),
            {"action": "lookup", "doi": "10.1000/example", "popup": "1"},
        )
        self.assertIn("popup=1", redirect.url)

    def test_save_multiple_links_through_popup_and_edit(self):
        project = make_project()
        a = make_entry(project, number=1, title="A")
        b = make_entry(project, number=2, title="B")
        values = {
            "action": "save",
            "title": "C",
            "authors": "Curator",
            "reference_number": "3",
            "origin_source": "reference_list",
            "inclusion_origin": "reading",
            "cited_entries": [a.pk, b.pk],
            "popup": "1",
        }
        response = self.client.post(
            reverse("survey_entry_add", args=[project.pk]), values)
        self.assertContains(
            response, "window.parent.location.replace(window.parent.location.href)")
        entry = SurveyEntry.objects.get(title="C")
        self.assertEqual(
            set(entry.manual_outgoing.values_list(
                "cited_id", flat=True)), {a.pk, b.pk}
        )
        edit_url = reverse("survey_entry_edit", args=[project.pk, entry.pk])
        editor = self.client.get(edit_url)
        self.assertEqual(
            set(editor.context["form"]["cited_entries"].value()), {a.pk, b.pk})
        values.update(
            cited_entries=[b.pk], version=editor.context["form"].initial["version"])
        self.client.post(edit_url, values)
        self.assertEqual(
            set(entry.manual_outgoing.values_list("cited_id", flat=True)), {b.pk})

    def test_empty_new_link_controls_remove_links_but_old_forms_preserve_them(self):
        project = make_project()
        target = make_entry(project, number=1)
        entry = make_entry(
            project,
            number=2,
            title="Original",
            inclusion_origin="search",
            cited_entries=[target.pk],
        )
        url = reverse("survey_entry_edit", args=[project.pk, entry.pk])
        values = {
            "action": "save",
            "reference_number": "2",
            "title": "Edited",
            "origin_source": "openalex",
            "version": signing.dumps(
                [entry.pk, entry.updated_at.isoformat()], salt="survey-version"
            ),
        }
        self.assertEqual(self.client.post(url, values).status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.inclusion_origin, "search")
        self.assertTrue(entry.manual_outgoing.exists())
        values.update(
            cited_entries_present="1",
            version=signing.dumps(
                [entry.pk, entry.updated_at.isoformat()], salt="survey-version"),
        )
        self.assertEqual(self.client.post(url, values).status_code, 302)
        self.assertFalse(entry.manual_outgoing.exists())

    def test_global_cached_doi_prefills_reading_without_project_discovery(self):
        project = make_project()
        other = make_project("Other")
        paper = make_discovered(
            other, title="Cached elsewhere", doi="10.1000/shared")
        with patch("literature.survey_views.lookup_doi") as lookup:
            response = self.client.get(
                reverse("survey_entry_add", args=[project.pk]), {
                    "doi": paper.doi}
            )
        lookup.assert_not_called()
        self.assertEqual(
            response.context["form"].initial["title"], "Cached elsewhere")
        self.assertEqual(
            response.context["form"].initial["inclusion_origin"], "reading")
        self.assertFalse(paper.discoveries.filter(
            search__project=project).exists())

    def test_search_fields_limit_and_foreign_exclusion(self):
        project = make_project()
        other = make_project("Foreign")
        foreign = make_discovered(other, title="Needle")
        for index in range(30):
            make_discovered(
                project, title=f"Needle {index:02}", doi=f"10.1000/needle-{index}")
        for query in ("Needle", "Jane Doe", "Venue X", "10.1000/needle-"):
            response = self.client.get(
                reverse("survey_entry_add", args=[project.pk]), {"q": query})
            papers = response.context["project_papers"]
            self.assertEqual(len(papers), 25)
            self.assertNotIn(foreign.pk, [paper.pk for paper in papers])

    def test_q_search_and_scoping(self):
        project = make_project()
        other = make_project(name="O")
        make_discovered(project, title="Robot Navigation", doi="10.1000/nav")
        make_discovered(other, title="Robot Navigation", doi="10.1000/nav2")
        resp = self.client.get(f"/project/{project.pk}/survey/add/?q=robot")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("project_papers", resp.context)
        self.assertIn("paper_query", resp.context)
        titles = [p.title for p in resp.context["project_papers"]]
        self.assertTrue(titles)
        self.assertLessEqual(len(resp.context["project_papers"]), 25)
        blank = self.client.get(f"/project/{project.pk}/survey/add/?q=")
        self.assertEqual(blank.context["project_papers"], [])

    def test_paper_id_prefill_and_guards(self):
        project = make_project()
        other = make_project(name="O")
        paper = make_discovered(project, title="Scoped Paper")
        foreign = make_discovered(other, title="Foreign")
        with patch("literature.survey_views.lookup_doi") as lookup:
            resp = self.client.get(
                f"/project/{project.pk}/survey/add/?paper_id={paper.pk}")
            lookup.assert_not_called()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Scoped Paper",
                      resp.context["form"].initial.get("title", ""))
        self.assertEqual(
            self.client.get(
                f"/project/{project.pk}/survey/add/?paper_id=abc").status_code, 400
        )
        self.assertEqual(
            self.client.get(
                f"/project/{project.pk}/survey/add/?paper_id=999999").status_code, 404
        )
        self.assertEqual(
            self.client.get(
                f"/project/{project.pk}/survey/add/?paper_id={foreign.pk}").status_code,
            404,
        )

    def test_doi_outage_manual_fallback(self):
        project = make_project()
        with patch("literature.survey_views.lookup_doi", side_effect=httpx.RequestError("down")):
            resp = self.client.get(
                f"/project/{project.pk}/survey/add/?doi=10.1000%2Fgone")
        self.assertEqual(resp.status_code, 200)

    def test_popup_success_error_frame(self):
        project = make_project()
        get = self.client.get(f"/project/{project.pk}/survey/add/?popup=1")
        self.assertTrue(get.context["popup"])
        self.assertIn("SAMEORIGIN", get.get("X-Frame-Options", ""))
        for text in ('id="project-save-status"', 'class="site-header"', "Legacy shared-DB"):
            self.assertNotContains(get, text)
        bad = self.client.post(
            f"/project/{project.pk}/survey/add/",
            {
                "action": "save",
                "title": "",
                "reference_number": "",
                "origin_source": "",
                "popup": "1",
            },
        )
        self.assertEqual(bad.status_code, 200)
        self.assertTrue(bad.context["popup"])
        ok = self.client.post(
            f"/project/{project.pk}/survey/add/",
            {
                "action": "save",
                "title": "Pop",
                "reference_number": "1",
                "origin_source": "openalex",
                "inclusion_origin": "reading",
                "popup": "1",
            },
        )
        self.assertEqual(ok.status_code, 200)
        self.assertIn("graph_url", ok.context)
        self.assertEqual(ok.context["graph_url"], reverse(
            "survey_graph", args=[project.pk]))
        normal = self.client.post(
            f"/project/{other_pk(project)}/survey/add/",
            {
                "action": "save",
                "title": "N",
                "reference_number": "1",
                "origin_source": "openalex",
                "inclusion_origin": "reading",
            },
        )
        self.assertEqual(normal.status_code, 302)

    def test_graph_aliases(self):
        project = make_project()
        make_entry(project, number=1, title="A")
        resp = self.client.get(f"/project/{project.pk}/survey/")
        for key in (
            "entries",
            "survey_entries",
            "graph_plot",
            "survey_plot",
            "automatic_count",
            "survey_automatic_count",
        ):
            self.assertIn(key, resp.context)


# --- Functions
def other_pk(project):
    other = Project.objects.create(name="N2")
    other.ensure_default_stages()
    return other.pk


# --- Real fixed worker, genuine CSRF and parent lease; no live store access ---
class WorkerPopupTests(SimpleTestCase):
    def test_entry_only_responses_commit_atomically_without_project_save(self):
        from literature import workspace as ws
        from literature.project_store import create_project_store, open_project_store
        from literature.tests.test_wp0105_lifecycle import _Client

        with tempfile.TemporaryDirectory(prefix="survey-popup-") as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "XDG_DATA_HOME": str(root / "state"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "PYTHON_DOTENV_DISABLED": "1",
                    "WP_MANAGER_BASE": "http://127.0.0.1:8001",
                },
            ):
                store = create_project_store(
                    root / "project", "Synthetic popup")
                tab = ws.acquire_handle(store.location)
                canonical = str(store.location)
                try:
                    base = f"{tab['base']}{tab['prefix']}/project/{tab['project_id']}"
                    client = _Client(tab["handle_id"])
                    add = base + "/survey/add/"
                    db = store.location / "project.sqlite3"

                    def rows(table):
                        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
                            return connection.execute(
                                f"SELECT * FROM literature_{table} ORDER BY id"
                            ).fetchall()

                    def no_save_panel(html):
                        for forbidden in (
                            canonical,
                            "project.sqlite3",
                            'id="project-save-status"',
                            "Submitted edits are saved immediately",
                            "saves validated entry changes automatically",
                            "No extra Save Project",
                            "Backup / Restore",
                            "Legacy shared-DB",
                            "New / Load manager",
                        ):
                            self.assertNotIn(forbidden, html)

                    def entry_only(response, text, popup=True):
                        status, body, headers = response
                        html = body.decode()
                        self.assertEqual(status, 200, html)
                        self.assertEqual(
                            headers["X-Frame-Options"], "SAMEORIGIN")
                        self.assertIn(text, html)
                        no_save_panel(html)
                        if popup:
                            for forbidden in (
                                "Save project copy",
                                'class="site-header"',
                                'class="site-footer"',
                                'class="site-nav"',
                            ):
                                self.assertNotIn(forbidden, html)
                        else:
                            self.assertIn('class="site-nav"', html)
                            self.assertIn("Save project copy", html)
                        return html

                    def save(number, title, **extra):
                        return client.post(
                            add,
                            {
                                "action": "save",
                                "popup": "1",
                                "reference_number": str(number),
                                "title": title,
                                "origin_source": "crossref",
                                "inclusion_origin": "reading",
                                "cited_entries_present": "1",
                                **extra,
                            },
                        )

                    entry_only(client.get(add + "?popup=1"),
                               'name="reference_number"')
                    entry_only(client.get(add),
                               'name="reference_number"', popup=False)
                    self.assertTrue(client._csrf())
                    # Missing CSRF or parent lease must still fail closed.
                    self.assertEqual(
                        _Client(tab["handle_id"]).post(add, {})[0], 403)
                    no_lease = _Client()
                    no_lease.get(add + "?popup=1")
                    self.assertEqual(no_lease.post(
                        add, {"action": "save"})[0], 410)
                    client.get(base + "/search/new/")
                    self.assertEqual(
                        client.post(
                            base + "/search/new/",
                            {"name": "Synthetic",
                                "keywords_json": '["synthetic"]'},
                        )[0],
                        200,
                    )
                    client.get(base + "/papers/add/")
                    self.assertEqual(
                        client.post(
                            base + "/papers/add/",
                            {
                                "search_keyword": "1|synthetic",
                                "origin_source": "crossref",
                                "doi": "10.1000/popup",
                                "title": "Synthetic paper",
                                "authors": "Synthetic author",
                                "publication_year": "2025",
                                "venue": "Synthetic journal",
                            },
                        )[0],
                        200,
                    )
                    self.assertEqual(len(rows("paper")), 1)
                    research = {
                        table: rows(table)
                        for table in (
                            "paper",
                            "discovery",
                            "search",
                            "searchrun",
                            "stage",
                            "screeningdecision",
                            "screeningrevision",
                            "paperkeywordassignment",
                        )
                    }
                    for query in (
                        "q=Synthetic",
                        "paper_id=1",
                        "doi=10.1000/popup",
                    ):
                        entry_only(client.get(add + "?popup=1&" +
                                   query), "Synthetic paper")
                        entry_only(client.get(add + "?" + query),
                                   "Synthetic paper", popup=False)
                    entry_only(
                        client.post(
                            add, {"popup": "1", "action": "lookup", "doi": "bad"}),
                        "Enter a DOI",
                    )
                    entry_only(
                        client.post(
                            add, {"popup": "1", "action": "lookup",
                                  "doi": "10.1000/popup"}
                        ),
                        "Synthetic paper",
                    )
                    entry_only(
                        client.post(add, {"action": "lookup", "doi": "bad"}),
                        "Enter a DOI",
                        popup=False,
                    )
                    entry_only(
                        client.post(add, {"action": "lookup",
                                    "doi": "10.1000/popup"}),
                        "Synthetic paper",
                        popup=False,
                    )
                    entry_only(save(1, ""), "This field is required")
                    entry_only(save(1, "", popup=""),
                               "This field is required", popup=False)
                    self.assertEqual(rows("surveyentry"), [])
                    success = entry_only(
                        save(1, "First"), "Survey entry saved.")
                    self.assertIn(
                        "window.parent.location.replace(window.parent.location.href)", success
                    )
                    status, body, _ = save(
                        2, "Second", cited_entries=[1], popup="")
                    self.assertEqual(status, 200)
                    self.assertIn("Second", body.decode())
                    no_save_panel(body.decode())
                    self.assertEqual(len(rows("surveyentry")), 2)
                    self.assertEqual(len(rows("surveycitation")), 1)
                    entry_only(save(2, "Duplicate"), "already used")
                    entry_only(save(3, "Bad link", cited_entries=[
                               999999]), "Select a valid choice")
                    self.assertEqual(len(rows("surveyentry")), 2)
                    self.assertEqual(len(rows("surveycitation")), 1)
                    edit = base + "/survey/2/edit/"
                    entry_only(client.get(edit + "?popup=1"),
                               "Save entered details")
                    entry_only(client.get(edit),
                               "Save entered details", popup=False)
                    entry_only(
                        client.post(
                            edit, {"popup": "1", "action": "save", "title": ""}),
                        "This field is required",
                    )
                    entry_only(
                        client.post(edit, {"action": "save", "title": ""}),
                        "This field is required",
                        popup=False,
                    )
                    for popup in ("1", ""):
                        editor = entry_only(
                            client.get(edit + "?popup=" + popup),
                            "Save entered details",
                            popup=bool(popup),
                        )
                        response = client.post(
                            edit,
                            {
                                "popup": popup,
                                "action": "save",
                                "reference_number": "2",
                                "title": "Second edited",
                                "origin_source": "crossref",
                                "inclusion_origin": "reading",
                                "cited_entries": [1],
                                "cited_entries_present": "1",
                                "version": re.search(r'name="version" value="([^"]+)"', editor)[1],
                            },
                        )
                        if popup:
                            entry_only(response, "Survey entry saved.")
                        else:
                            self.assertEqual(response[0], 200)
                            self.assertIn("Second edited",
                                          response[1].decode())
                            no_save_panel(response[1].decode())
                    status, body, _ = client.get(base + "/survey/")
                    self.assertEqual(status, 200)
                    no_save_panel(body.decode())
                    self.assertIn("Save project copy", body.decode())
                    self.assertIn('class="site-nav"', body.decode())
                    for route in ("/", "/papers/"):
                        status, body, _ = client.get(base + route)
                        self.assertEqual(status, 200)
                        html = body.decode()
                        for expected in (
                            canonical,
                            'id="project-save-status"',
                            "Save project copy",
                            "Backup / Restore",
                            'class="site-nav"',
                        ):
                            self.assertIn(expected, html)
                    self.assertEqual(ws.live_handle_count(canonical), 1)
                    self.assertEqual({table: rows(table)
                                     for table in research}, research)
                    entry_only(client.get(edit + "?popup=1"), "Second edited")
                    ws.release_handle(tab["handle_id"])
                    self.assertTrue(ws.drain_and_stop(canonical)["stopped"])
                    reopened = open_project_store(store.location)
                    self.assertEqual(reopened.location, store.location)
                    self.assertEqual(len(rows("surveyentry")), 2)
                    self.assertEqual(len(rows("surveycitation")), 1)
                finally:
                    ws.release_handle(tab["handle_id"])
                    ws.drain_and_stop(canonical)
