"""
Focused tests for the curated survey graph (no draft mutation).

@File: test_survey_graph.py
@Brief: DOI prefill snapshots, ownership, manual links, duplicates, stale saves.
@Date: 01/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from datetime import timedelta
from unittest.mock import patch

# Custom library Imports
import httpx
from django.core import signing
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from literature.models import (
    Discovery,
    OpenCitationsLookup,
    Paper,
    PaperKeywordAssignment,
    Project,
    ScreeningDecision,
    Search,
    SurveyCitation,
    SurveyEntry,
)
from literature.services.plots_service import build_citation_figure
from literature.services.survey_graph_service import (
    build_survey_graph,
    citation_key,
    prefill,
    save_survey_entry,
)

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Shared fixtures ---


def make_project(name="P"):
    project = Project.objects.create(name=name)
    project.ensure_default_stages()
    return project


def make_entry(project, number=1, doi="", title="T", year=None, text=""):
    return save_survey_entry(
        project,
        {
            "reference_number": number,
            "title": title,
            "doi": doi,
            "authors": "A U Thor",
            "publication_year": year,
            "venue": "V",
            "publisher": "",
            "abstract": "",
            "reference_text": text,
            "origin_source": "openalex",
            "version": "",
            "preview_token": "",
        },
    )


# --- Classes
class SurveyKeyTests(TestCase):
    """
    Actual IDs only; OpenAlex slash trim; DOI suffix slash kept.
    """

    def test_titles_rejected(self):
        self.assertEqual(citation_key("Some Interesting Title"), "")
        self.assertEqual(citation_key(""), "")

    def test_openalex_digits_only_and_slash(self):
        self.assertEqual(citation_key("https://openalex.org/W123/"), "W123")
        self.assertEqual(citation_key("https://openalex.org/W123"), "W123")
        self.assertEqual(citation_key("W123"), "W123")
        self.assertEqual(citation_key("https://openalex.org/W12a"), "")
        self.assertEqual(citation_key("W12a"), "")

    def test_doi_pattern_and_suffix_slash_kept(self):
        self.assertEqual(citation_key("10.1000/Example"), "10.1000/example")
        self.assertEqual(citation_key(
            "https://doi.org/10.1000/foo/"), "10.1000/foo/")
        self.assertEqual(citation_key("10.1/x"), "")


class SurveyPrefillTests(TestCase):
    """
    Editable snapshots never mutate the draft library.
    """

    def test_string_authors_prefill(self):
        project = make_project()
        out = prefill(project, "10.1000/a",
                      {"title": "T", "authors": "A, B"}, None)
        self.assertEqual(out["authors"], "A, B")
        out2 = prefill(project, "10.1000/a",
                       {"title": "T", "authors": [{"name": "X"}, "Y"]}, None)
        self.assertIn("X", out2["authors"])
        self.assertIn("Y", out2["authors"])

    def test_cached_identifiers_connect_only_curated_entries(self):
        project = make_project()
        target = prefill(
            project,
            "10.1000/target",
            {
                "title": "Target",
                "source_ids": {
                    "openalex": "W42",
                    "arxiv": "2508.06283v2",
                    "semantic_scholar": "abc123",
                },
            },
            None,
        )
        target.update(reference_number=1, origin_source="crossref")
        entry = save_survey_entry(project, target)
        source = make_entry(
            project,
            number=2,
            title="Source",
            text="https://openalex.org/W42\narxiv:2508.06283\nS2:abc123\nUnresolved Paper Title",
        )
        make_entry(project, number=3, title="Unresolved Paper Title")
        self.assertEqual(build_survey_graph(project)[
                         "edges"], [(source.pk, entry.pk)])
        cached = OpenCitationsLookup.objects.create(
            doi="10.48550/arxiv.2508.06283",
            direction="outgoing",
            edges=[["10.48550/arxiv.2508.06283", "10.1000/source"]],
        )
        source.doi = "10.1000/source"
        source.save()
        self.assertIn((entry.pk, source.pk),
                      build_survey_graph(project)["edges"])
        cached.delete()

    def test_prefill_snapshot_does_not_mutate_library(self):
        project = make_project()
        search = Search.objects.create(
            project=project, name="S", keywords=["robotics"])
        paper = Paper.objects.create(
            title="Orig", doi="10.1000/orig", authors=[{"name": "P A"}])
        Discovery.objects.create(
            paper=paper, search=search, source="openalex", query="robotics")
        PaperKeywordAssignment.objects.create(
            paper=paper, project=project, search=search, keyword="robotics"
        )
        stage = project.stages.get(name="Title")
        ScreeningDecision.objects.create(
            paper=paper, stage=stage, decision="ACCEPT")
        snap = (
            paper.title,
            paper.doi,
            Discovery.objects.count(),
            PaperKeywordAssignment.objects.get().keyword,
            ScreeningDecision.objects.count(),
        )
        data = prefill(project, "10.1000/orig", {}, paper)
        self.assertEqual(data["title"], "Orig")
        data.update(
            {
                "reference_number": 1,
                "origin_source": "openalex",
                "title": "Edited snapshot",
                "version": "",
                "publication_year": None,
                "venue": "",
                "publisher": "",
                "abstract": "",
                "authors": "E",
                "reference_text": "",
            }
        )
        entry = save_survey_entry(project, data)
        self.assertEqual(entry.title, "Edited snapshot")
        paper.refresh_from_db()
        self.assertEqual(
            (
                paper.title,
                paper.doi,
                Discovery.objects.count(),
                PaperKeywordAssignment.objects.get().keyword,
                ScreeningDecision.objects.count(),
            ),
            snap,
        )


class SurveySaveTests(TestCase):
    """
    Number/DOI duplicates, required fields, alias retain/drop, stale saves.
    """

    def test_number_and_doi_duplicates(self):
        project = make_project()
        make_entry(project, number=1, doi="10.1000/a", title="A")
        with self.assertRaises(ValueError):
            make_entry(project, number=1, doi="10.1000/b", title="B")
        with self.assertRaises(ValueError):
            make_entry(project, number=2,
                       doi="HTTPS://DOI.ORG/10.1000/A ", title="Dup")
        # Blank DOI may repeat.
        make_entry(project, number=2, title="No DOI")
        make_entry(project, number=3, title="No DOI 2")

    def test_required_title_and_number(self):
        project = make_project()
        with self.assertRaises(ValueError):
            make_entry(project, number=0, title="T")
        with self.assertRaises(ValueError):
            make_entry(project, number=1, title="  ")

    def test_aliases_retained_on_unchanged_doi_dropped_on_edit(self):
        project = make_project()
        paper = Paper.objects.create(
            title="P", doi="10.1000/k", openalex_id="https://openalex.org/W111"
        )
        token = prefill(project, "10.1000/k", {}, paper)["preview_token"]
        entry = save_survey_entry(
            project,
            {
                "reference_number": 1,
                "title": "P",
                "doi": "10.1000/k",
                "authors": "",
                "publication_year": None,
                "venue": "",
                "publisher": "",
                "abstract": "",
                "reference_text": "",
                "origin_source": "openalex",
                "version": "",
                "preview_token": token,
            },
        )
        self.assertIn("W111", entry.identity_aliases)
        self.assertEqual(entry.paper_id, paper.pk)
        version = signing.dumps(
            [entry.pk, entry.updated_at.isoformat()], salt="survey-version")
        kept = save_survey_entry(
            project,
            {
                "reference_number": 1,
                "title": "P2",
                "doi": "10.1000/k",
                "authors": "",
                "publication_year": None,
                "venue": "",
                "publisher": "",
                "abstract": "",
                "reference_text": "",
                "origin_source": "openalex",
                "version": version,
                "preview_token": "",
            },
            entry_id=entry.pk,
        )
        self.assertIn("W111", kept.identity_aliases)
        version2 = signing.dumps(
            [kept.pk, kept.updated_at.isoformat()], salt="survey-version")
        changed = save_survey_entry(
            project,
            {
                "reference_number": 1,
                "title": "P3",
                "doi": "10.1000/other",
                "authors": "",
                "publication_year": None,
                "venue": "",
                "publisher": "",
                "abstract": "",
                "reference_text": "",
                "origin_source": "openalex",
                "version": version2,
                "preview_token": "",
            },
            entry_id=entry.pk,
        )
        self.assertEqual(changed.identity_aliases, [])
        self.assertIsNone(changed.paper)

    def test_stale_save_rejected(self):
        project = make_project()
        entry = make_entry(project, number=1, title="A")
        version = signing.dumps(
            [entry.pk, entry.updated_at.isoformat()], salt="survey-version")
        entry.title = "Elsewhere"
        entry.save()
        with self.assertRaises(ValueError):
            save_survey_entry(
                project,
                {
                    "reference_number": 1,
                    "title": "Stale",
                    "doi": "",
                    "authors": "",
                    "publication_year": None,
                    "venue": "",
                    "publisher": "",
                    "abstract": "",
                    "reference_text": "",
                    "origin_source": "openalex",
                    "version": version,
                    "preview_token": "",
                },
                entry_id=entry.pk,
            )
        with self.assertRaises(ValueError):
            save_survey_entry(
                project,
                {
                    "reference_number": 1,
                    "title": "Bad",
                    "doi": "",
                    "authors": "",
                    "publication_year": None,
                    "venue": "",
                    "publisher": "",
                    "abstract": "",
                    "reference_text": "",
                    "origin_source": "openalex",
                    "version": "not-a-token",
                    "preview_token": "",
                },
                entry_id=entry.pk,
            )

    def test_malformed_preview_rejected(self):
        project = make_project()
        bad = signing.dumps({"bad": 1}, salt="survey-prefill")
        with self.assertRaises(ValueError):
            save_survey_entry(
                project,
                {
                    "reference_number": 1,
                    "title": "T",
                    "doi": "10.1000/a",
                    "authors": "",
                    "publication_year": None,
                    "venue": "",
                    "publisher": "",
                    "abstract": "",
                    "reference_text": "",
                    "origin_source": "openalex",
                    "version": "",
                    "preview_token": bad,
                },
            )
        self.assertFalse(SurveyEntry.objects.exists())
        other = make_project(name="Q")
        foreign = prefill(other, "10.1000/a",
                          {"title": "T"}, None)["preview_token"]
        with self.assertRaises(ValueError):
            save_survey_entry(
                project,
                {
                    "reference_number": 1,
                    "title": "T",
                    "doi": "10.1000/a",
                    "authors": "",
                    "publication_year": None,
                    "venue": "",
                    "publisher": "",
                    "abstract": "",
                    "reference_text": "",
                    "origin_source": "openalex",
                    "version": "",
                    "preview_token": foreign,
                },
            )
        tampered = prefill(
            project, "10.1000/a", {"title": "T",
                                   "openalex_id": "https://openalex.org/W5"}, None
        )
        tampered["preview_token"] = signing.dumps(
            {"project": project.pk, "doi": "10.1000/a",
                "aliases": ["not an id!!"], "paper": None},
            salt="survey-prefill",
        )
        with self.assertRaises(ValueError):
            save_survey_entry(
                project,
                {
                    "reference_number": 1,
                    "title": "T",
                    "doi": "10.1000/a",
                    "authors": "",
                    "publication_year": None,
                    "venue": "",
                    "publisher": "",
                    "abstract": "",
                    "reference_text": "",
                    "origin_source": "openalex",
                    "version": "",
                    "preview_token": tampered["preview_token"],
                },
            )


class SurveyGraphViewTests(TestCase):
    """
    Ownership, manual links, rendering, malformed IDs, outages.
    """

    def test_cross_project_citation_rejected(self):
        a = make_project(name="A")
        b = make_project(name="B")
        ea = make_entry(a, number=1, title="A1")
        eb = make_entry(b, number=1, title="B1")
        # Entry edit scoped to project.
        self.assertEqual(self.client.get(
            f"/project/{a.pk}/survey/{eb.pk}/edit/").status_code, 404)
        # Foreign entry cannot be linked inside project A.
        resp = self.client.post(
            f"/project/{a.pk}/survey/",
            {"action": "link", "citing": str(ea.pk), "cited": str(eb.pk)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(SurveyCitation.objects.exists())

    def test_form_limits_and_invalid_doi_are_controlled(self):
        project = make_project()
        base = {
            "action": "save",
            "title": "T",
            "reference_number": "1",
            "origin_source": "crossref",
        }
        for field, value in (
            ("reference_number", "0"),
            ("reference_number", "99999999999999999999999999"),
            ("publication_year", "1499"),
            ("publication_year", "10000"),
            ("publication_year", "abc"),
            ("title", ""),
            ("origin_source", ""),
            ("doi", "not a DOI"),
            ("venue", "x" * 501),
            ("publisher", "x" * 301),
        ):
            with self.subTest(field=field, value=value):
                response = self.client.post(
                    f"/project/{project.pk}/survey/add/", {**
                                                           base, field: value}
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.assertFalse(SurveyEntry.objects.exists())
        with patch("literature.survey_views.lookup_doi") as lookup:
            self.assertEqual(
                self.client.get(
                    f"/project/{project.pk}/survey/add/?doi=not-a-doi").status_code, 200
            )
        lookup.assert_not_called()

    def test_blank_number_saved_then_assigned_inline_under_graph(self):
        project = make_project()
        response = self.client.post(
            f"/project/{project.pk}/survey/add/",
            {
                "action": "save",
                "title": "Unnumbered",
                "reference_number": "",
                "origin_source": "crossref",
            },
        )
        self.assertEqual(response.status_code, 302)
        entry = project.survey_entries.get()
        self.assertIsNone(entry.reference_number)
        graph = self.client.get(f"/project/{project.pk}/survey/")
        self.assertContains(graph, 'name="reference_number"')
        renumbered = self.client.post(
            f"/project/{project.pk}/survey/",
            {"action": "renumber", "entry_id": entry.pk,
             "reference_number": "4"},
        )
        self.assertEqual(renumbered.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.reference_number, 4)
        other = project.survey_entries.create(
            title="Other", reference_number=None, origin_source="")
        clash = self.client.post(
            f"/project/{project.pk}/survey/",
            {"action": "renumber", "entry_id": other.pk,
             "reference_number": "4"},
        )
        self.assertEqual(clash.status_code, 302)
        other.refresh_from_db()
        self.assertIsNone(other.reference_number)
        cleared = self.client.post(
            f"/project/{project.pk}/survey/",
            {"action": "renumber", "entry_id": entry.pk,
             "reference_number": ""},
        )
        self.assertEqual(cleared.status_code, 302)
        entry.refresh_from_db()
        self.assertIsNone(entry.reference_number)

    def test_citation_snapshot_scales_nodes_without_showing_counts(self):
        project = make_project()
        paper = Paper.objects.create(
            title="Heavily cited", doi="10.1000/cited",
            citation_count=120,
        )
        search = Search.objects.create(
            project=project, name="S", keywords=["k"])
        Discovery.objects.create(
            search=search, paper=paper, source="openalex", query="k")
        preview = prefill(project, "10.1000/cited", {}, paper)
        self.assertEqual(preview["citation_count"], 120)
        entry = save_survey_entry(
            project,
            {
                "reference_number": 1, "title": "Heavily cited",
                "doi": "10.1000/cited", "authors": "",
                "publication_year": None, "venue": "",
                "publisher": "", "abstract": "",
                "reference_text": "",
                "origin_source": "openalex",
                "inclusion_origin": "search",
                "citation_count": preview["citation_count"],
                "version": "", "preview_token": "",
            },
        )
        self.assertEqual(entry.citation_count, 120)
        quiet = save_survey_entry(
            project,
            {
                "reference_number": 2, "title": "Quiet",
                "doi": "", "authors": "",
                "publication_year": None, "venue": "",
                "publisher": "", "abstract": "",
                "reference_text": "",
                "origin_source": "openalex",
                "inclusion_origin": "reading",
                "version": "", "preview_token": "",
            },
        )
        self.assertEqual(quiet.citation_count, 0)
        graph = build_survey_graph(project)
        counts = {node["id"]: node["citations"] for node in graph["nodes"]}
        self.assertEqual(counts, {entry.pk: 120, quiet.pk: 0})
        with patch(
            "plotly.graph_objects.Figure.to_html", autospec=True,
            return_value="plot",
        ) as render:
            build_citation_figure(graph, hide_citation_counts=True)
        figure = render.call_args.args[0]
        trace = next(t for t in figure.data if t.meta)
        sizes = dict(zip(
            [d["title"] for d in trace.meta["paper_details"]],
            list(trace.marker.size),
        ))
        self.assertGreater(sizes["Heavily cited"], sizes["Quiet"])
        self.assertTrue(
            all("citations" not in d for d in trace.meta["paper_details"]))
        hidden = build_citation_figure(graph, hide_citation_counts=True)
        self.assertNotIn("Cited by", hidden)
        shown = build_citation_figure(graph)
        self.assertIn("Cited by", shown)

    def test_legacy_entry_without_snapshot_falls_back_to_paper_count(self):
        project = make_project()
        paper = Paper.objects.create(
            title="Counted", doi="10.1000/counted", citation_count=45)
        entry = SurveyEntry.objects.create(
            project=project, paper=paper, title="Counted",
            reference_number=None, citation_count=0, origin_source="")
        graph = build_survey_graph(project)
        self.assertEqual(graph["nodes"][0]["citations"], 45)
        self.assertEqual(entry.citation_count, 0)

    def test_expired_tokens_and_snapshot_edit_delete_preserve_draft(self):
        project = make_project()
        paper = Paper.objects.create(
            title="Canonical",
            doi="10.1000/canonical",
            abstract="Known abstract",
            references=["https://openalex.org/W8"],
        )
        search = Search.objects.create(
            project=project, name="S", keywords=["original"])
        Discovery.objects.create(
            search=search, paper=paper, source="crossref", query="original")
        PaperKeywordAssignment.objects.create(
            project=project, paper=paper, search=search, keyword="original"
        )
        ScreeningDecision.objects.create(
            stage=project.stages.get(name="Title"), paper=paper, decision="ACCEPT"
        )
        models = (Paper, Discovery, PaperKeywordAssignment,
                  ScreeningDecision, Search)
        before = [list(model.objects.values()) for model in models]
        with patch("literature.survey_views.lookup_doi") as lookup:
            response = self.client.get(
                f"/project/{project.pk}/survey/add/?doi=10.1000%2Fcanonical")
        lookup.assert_not_called()
        values = {
            **{
                key: value if value is not None else ""
                for key, value in response.context["form"].initial.items()
            },
            "action": "save",
            "title": "Curated",
            "reference_number": "12",
            "origin_source": "crossref",
        }
        self.assertEqual(
            self.client.post(
                f"/project/{project.pk}/survey/add/", values).status_code, 302
        )
        entry = project.survey_entries.get()
        values.pop("preview_token")
        values["version"] = signing.dumps(
            [entry.pk, entry.updated_at.isoformat()], salt="survey-version"
        )
        values["abstract"] = "Editable curated abstract"
        edit_url = f"/project/{project.pk}/survey/{entry.pk}/edit/"
        self.assertEqual(self.client.post(edit_url, values).status_code, 302)
        with patch(
            "django.core.signing.TimestampSigner.unsign",
            side_effect=signing.SignatureExpired("expired"),
        ):
            self.assertContains(self.client.post(edit_url, values), "expired")
            values["preview_token"] = "expired-preview"
            values["reference_number"] = "13"
            values["doi"] = "10.1000/other"
            self.assertContains(
                self.client.post(
                    f"/project/{project.pk}/survey/add/", values), "expired"
            )
        self.assertEqual(
            self.client.post(
                f"/project/{project.pk}/survey/", {"action": "remove_entry",
                                                   "entry_id": entry.pk}
            ).status_code,
            302,
        )
        self.assertEqual([list(model.objects.values())
                         for model in models], before)

    def test_manual_add_cycle_and_removal(self):
        project = make_project()
        a = make_entry(project, number=1, title="A")
        b = make_entry(project, number=2, title="B")
        resp = self.client.post(
            f"/project/{project.pk}/survey/",
            {"action": "link", "citing": str(a.pk), "cited": str(b.pk)},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(SurveyCitation.objects.filter(
            citing=a, cited=b).exists())
        # Self link rejected.
        resp = self.client.post(
            f"/project/{project.pk}/survey/",
            {"action": "link", "citing": str(a.pk), "cited": str(a.pk)},
        )
        self.assertEqual(resp.status_code, 200)
        # Two-node cycle allowed (directed, only self banned).
        resp = self.client.post(
            f"/project/{project.pk}/survey/",
            {"action": "link", "citing": str(b.pk), "cited": str(a.pk)},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(SurveyCitation.objects.count(), 2)
        link = SurveyCitation.objects.get(citing=a, cited=b)
        resp = self.client.post(
            f"/project/{project.pk}/survey/", {"action": "remove_link",
                                               "link_id": str(link.pk)}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(SurveyCitation.objects.filter(pk=link.pk).exists())
        resp = self.client.post(
            f"/project/{project.pk}/survey/", {"action": "remove_entry",
                                               "entry_id": str(b.pk)}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(SurveyEntry.objects.filter(pk=b.pk).exists())

    def test_missing_year_rendering_keeps_legend(self):
        project = make_project()
        make_entry(project, number=1, title="Dated", year=2020)
        make_entry(project, number=2, title="Undated", year=None)
        graph = build_survey_graph(project)
        html = build_citation_figure(graph)
        self.assertIn("?", html)
        self.assertIn("[1]", html)
        self.assertIn("[2]", html)
        self.assertIn("Dot colour", html)

    def test_malformed_ids_controlled(self):
        project = make_project()
        self.assertEqual(
            self.client.post(
                f"/project/{project.pk}/survey/", {"action": "remove_link",
                                                   "link_id": "abc"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                f"/project/{project.pk}/survey/", {"action": "remove_entry",
                                                   "entry_id": ""}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                f"/project/{project.pk}/survey/", {"action": "nope"}).status_code, 400
        )

    def test_cached_edges_used_regardless_age_and_doi_only(self):
        project = make_project()
        a = make_entry(project, number=1, doi="10.1000/a", title="A")
        b = make_entry(project, number=2, doi="10.1000/b", title="B")
        lookup = OpenCitationsLookup.objects.create(
            doi="10.1000/a", direction="outgoing", edges=[["10.1000/a", "10.1000/b"]]
        )
        OpenCitationsLookup.objects.filter(pk=lookup.pk).update(
            checked_at=timezone.now() - timedelta(days=60)
        )
        OpenCitationsLookup.objects.create(
            doi="W999", direction="outgoing", edges=[["W999", "10.1000/b"]]
        )
        graph = build_survey_graph(project)
        self.assertIn((a.pk, b.pk), graph["automatic_edges"])

    def test_ambiguous_cached_alias_does_not_guess_target(self):
        project = make_project()
        first = make_entry(project, number=1, doi="10.1000/first")
        second = make_entry(project, number=2, doi="10.1000/second")
        for entry in (first, second):
            entry.identity_aliases = ["W123"]
            entry.save()
        source = make_entry(project, number=3,
                            text="https://openalex.org/W123\n10.1000/first")
        self.assertEqual(build_survey_graph(project)[
                         "edges"], [(source.pk, first.pk)])

    def test_provider_outage_falls_back_to_manual(self):
        project = make_project()
        with patch("literature.survey_views.lookup_doi", side_effect=httpx.RequestError("down")):
            resp = self.client.get(
                f"/project/{project.pk}/survey/add/?doi=10.1000%2Fgone")
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            f"/project/{project.pk}/survey/add/",
            {
                "action": "save",
                "reference_number": "1",
                "doi": "",
                "title": "Manual",
                "authors": "",
                "publication_year": "",
                "venue": "",
                "publisher": "",
                "abstract": "",
                "reference_text": "",
                "origin_source": "openalex",
                "version": "",
                "preview_token": "",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(SurveyEntry.objects.filter(title="Manual").exists())

    def test_db_contention_mapped_to_form_error(self):
        project = make_project()
        with patch("literature.models.SurveyEntry.save", side_effect=OperationalError("locked")):
            resp = self.client.post(
                f"/project/{project.pk}/survey/add/",
                {
                    "action": "save",
                    "reference_number": "1",
                    "doi": "",
                    "title": "T",
                    "authors": "",
                    "publication_year": "",
                    "venue": "",
                    "publisher": "",
                    "abstract": "",
                    "reference_text": "",
                    "origin_source": "openalex",
                    "version": "",
                    "preview_token": "",
                },
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("database busy", resp.content.decode().lower())
