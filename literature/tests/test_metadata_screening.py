"""Regression tests for staged metadata completion, final selection and migration.

@File: test_metadata_screening.py
@Brief: Verify missing metadata never silently excludes a relevant titled paper.
@Date: 01/10/2026
"""

from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from literature.models import (
    Discovery,
    Paper,
    Project,
    ScreeningDecision,
    ScreeningRevision,
    Search,
)
from literature.services.citation_service import build_graph
from literature.services.export_service import build_markdown
from literature.services.metrics_service import (
    build_metrics,
    keyword_stage_counts,
    stage_year_cumulative,
)
from literature.services.reference_review_service import (
    complete_references,
    extract_reference_ids,
    save_missing_abstract,
)
from literature.services.screening_service import (
    InvalidTransition,
    abstract_available,
    decide,
    final_decisions,
    pending_references,
)
from literature.services.search_service import gate_papers


class MetadataScreeningTests(TestCase):
    """Exercise the full workflow with isolated databases and no provider requests."""

    def setUp(self):
        self.project = Project.objects.create(name="Screening project")
        self.project.ensure_default_stages()
        self.search = Search.objects.create(
            project=self.project, name="Search", keywords=["navigation"]
        )
        self.paper = Paper.objects.create(
            title="Navigation with scene graphs", publication_year=2025
        )
        Discovery.objects.create(
            paper=self.paper, search=self.search, source="manual", query="navigation"
        )
        self.title = self.project.stages.get(name="Title")
        self.abstract = self.project.stages.get(name="Abstract")
        gate_papers([self.paper], self.title)
        self.abstract_url = reverse("screening_view", args=[self.project.pk, "abstract"])
        self.reference_url = reverse("reference_review", args=[self.project.pk])

    def pass_title(self):
        decide(paper=self.paper, stage=self.title, decision="ACCEPT")

    def pass_abstract(self):
        self.pass_title()
        save_missing_abstract(
            self.project, self.paper, "A manually supplied abstract about navigation."
        )
        self.paper.refresh_from_db()
        decide(paper=self.paper, stage=self.abstract, decision="ACCEPT")

    def test_missing_fields_enter_title_and_then_abstract(self):
        self.assertEqual(self.paper.decisions.get(stage=self.title).decision, "PENDING")
        self.pass_title()
        page = self.client.get(self.abstract_url)
        self.assertContains(page, "No abstract found")
        self.assertContains(page, 'name="abstract"')
        self.assertContains(page, 'data-decision="ACCEPT" disabled')
        self.assertEqual(page.context["paper"].pk, self.paper.pk)

    def test_manual_abstract_save_keeps_same_paper_and_enables_acceptance(self):
        self.pass_title()
        response = self.client.post(
            self.abstract_url, {"paper_id": self.paper.pk, "abstract": "  The pasted abstract.  "}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"?paper={self.paper.pk}", response["Location"])
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.abstract, "The pasted abstract.")
        self.assertEqual(self.paper.raw["manual_metadata"]["abstract"]["source"], "manual")
        page = self.client.get(response["Location"])
        self.assertContains(page, "The pasted abstract.")
        self.assertNotContains(page, 'data-decision="ACCEPT" disabled')

    def test_blank_abstract_returns_field_error_without_changes(self):
        self.pass_title()
        response = self.client.post(
            self.abstract_url, {"paper_id": self.paper.pk, "abstract": "  "}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("abstract", response.context["abstract_form"].errors)
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.abstract, "")

    def test_existing_abstract_cannot_be_overwritten(self):
        self.pass_title()
        save_missing_abstract(self.project, self.paper, "Original pasted abstract")
        with self.assertRaisesMessage(InvalidTransition, "already present"):
            save_missing_abstract(self.project, self.paper, "Replacement")
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.abstract, "Original pasted abstract")

    def test_unknown_metadata_can_be_filled_without_entering_abstract(self):
        self.pass_title()
        response = self.client.post(
            self.abstract_url,
            {
                "paper_id": self.paper.pk,
                "authors": "Jane Researcher\nAnother Author",
                "venue": "Research Journal",
                "publisher": "Publisher",
                "doi": "10.1000/details",
                "keywords": "navigation\nrobotics",
                "citation_count": "7",
                "reference_text": "Smith (2020). An unindexed bibliography entry.",
                "title": "Forged replacement title",
                "publication_year": "2000",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.title, "Navigation with scene graphs")
        self.assertEqual(self.paper.publication_year, 2025)
        self.assertEqual(self.paper.abstract, "")
        self.assertEqual(self.paper.author_display(), "Jane Researcher, Another Author")
        self.assertEqual(self.paper.venue, "Research Journal")
        self.assertEqual(self.paper.publisher, "Publisher")
        self.assertEqual(self.paper.doi, "10.1000/details")
        self.assertEqual(self.paper.keywords, ["navigation", "robotics"])
        self.assertEqual(self.paper.citation_count, 7)
        self.assertIn("unindexed", self.paper.reference_text)
        self.assertEqual(self.paper.decisions.get(stage=self.abstract).decision, "PENDING")
        self.assertContains(self.client.get(response["Location"]), "No abstract found")

    def test_known_metadata_is_not_exposed_in_completion_form(self):
        self.pass_abstract()
        # Return to a pending content-review fixture without overwriting its abstract.
        self.paper.decisions.filter(stage=self.abstract).update(decision="PENDING")
        page = self.client.get(self.abstract_url)
        self.assertNotContains(page, 'name="title"')
        self.assertNotContains(page, 'name="publication_year"')
        self.assertNotContains(page, 'name="abstract"')
        self.assertContains(page, 'name="authors"')
        self.assertContains(page, "Complete other unknown details")

    def test_metadata_date_conflict_rolls_back_all_fields(self):
        self.pass_title()
        response = self.client.post(
            self.abstract_url,
            {"paper_id": self.paper.pk, "authors": "Jane", "publication_date": "2024-01-01"},
        )
        self.assertContains(response, "must match the publication year")
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.authors, [])
        self.assertIsNone(self.paper.publication_date)

    def test_duplicate_doi_cannot_partially_save_metadata(self):
        self.pass_title()
        Paper.objects.create(title="Other", doi="10.1000/other")
        response = self.client.post(
            self.abstract_url,
            {"paper_id": self.paper.pk, "authors": "Jane", "doi": "10.1000/other"},
        )
        self.assertContains(response, "belongs to another stored paper")
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.authors, [])
        self.assertIsNone(self.paper.doi)

    def test_metadata_identifier_race_returns_a_form_error(self):
        from django.db import IntegrityError

        self.pass_title()
        with patch(
            "literature.services.reference_review_service.save_missing_metadata",
            side_effect=IntegrityError("identifier collision"),
        ):
            response = self.client.post(
                self.abstract_url, {"paper_id": self.paper.pk, "authors": "Jane"}
            )
        self.assertContains(response, "no details were saved")
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.authors, [])

    def test_reported_zero_citation_count_stays_read_only(self):
        self.paper.raw = {"cited_by_count": 0}
        self.paper.save(update_fields=["raw"])
        self.pass_title()
        self.assertNotContains(self.client.get(self.abstract_url), 'name="citation_count"')

    def test_new_publication_date_supplies_missing_year(self):
        self.paper.publication_year = None
        self.paper.save(update_fields=["publication_year"])
        self.pass_title()
        response = self.client.post(
            self.abstract_url, {"paper_id": self.paper.pk, "publication_date": "2026-02-03"}
        )
        self.assertEqual(response.status_code, 302)
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.publication_year, 2026)
        self.assertEqual(self.paper.publication_date.isoformat(), "2026-02-03")

    def test_audience_tag_shows_paste_box_and_can_be_replaced(self):
        """SMaNa's stored provider tag must not masquerade as an abstract."""
        self.paper.title = (
            "SMaNa: Semantic Mapping and Navigation Architecture for Autonomous Robots"
        )
        self.paper.abstract = "International audience"
        self.paper.save(update_fields=["title", "abstract"])
        self.pass_title()
        with self.assertRaisesMessage(InvalidTransition, "No abstract found"):
            decide(paper=self.paper, stage=self.abstract, decision="ACCEPT")
        page = self.client.get(self.abstract_url)
        self.assertContains(page, "No abstract found")
        self.assertContains(page, 'name="abstract"')
        self.assertContains(page, 'data-decision="ACCEPT" disabled')
        response = self.client.post(
            self.abstract_url, {"paper_id": self.paper.pk, "abstract": "The real SMaNa abstract."}
        )
        self.assertEqual(response.status_code, 302)
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.abstract, "The real SMaNa abstract.")
        self.assertEqual(
            self.paper.raw["manual_metadata"]["abstract"]["replaced_placeholder"],
            "International audience",
        )
        self.assertNotContains(
            self.client.get(response["Location"]), 'data-decision="ACCEPT" disabled'
        )

    def test_abstract_detection_does_not_discard_short_or_prefixed_real_text(self):
        for text in (None, "", " \n ", "International audience", "  INTERNATIONAL AUDIENCE\n"):
            self.assertFalse(abstract_available(text), text)
        for text in (
            "A short abstract.",
            "International audience. We propose a navigation architecture.",
        ):
            self.assertTrue(abstract_available(text), text)

    def test_manually_pasting_the_provider_tag_is_rejected(self):
        self.pass_title()
        response = self.client.post(
            self.abstract_url, {"paper_id": self.paper.pk, "abstract": "International audience"}
        )
        self.assertContains(response, "Paste the actual abstract")
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.abstract, "")
        self.assertEqual(self.paper.decisions.get(stage=self.abstract).decision, "PENDING")

    def test_missing_abstract_can_be_rejected_but_not_accepted(self):
        self.pass_title()
        with self.assertRaisesMessage(InvalidTransition, "No abstract found"):
            decide(paper=self.paper, stage=self.abstract, decision="ACCEPT")
        self.assertEqual(self.paper.decisions.get(stage=self.abstract).decision, "PENDING")
        decide(paper=self.paper, stage=self.abstract, decision="REJECT")
        self.assertEqual(build_metrics(self.project)["stages"]["Abstract"]["reject"], 1)
        self.assertFalse(pending_references(self.project).exists())

    def test_abstract_requires_title_acceptance_and_project_membership(self):
        with self.assertRaisesMessage(InvalidTransition, "Accept the title"):
            decide(paper=self.paper, stage=self.abstract, decision="REJECT")
        other = Project.objects.create(name="Other")
        other.ensure_default_stages()
        with self.assertRaisesMessage(InvalidTransition, "does not belong"):
            decide(paper=self.paper, stage=other.stages.get(name="Title"), decision="ACCEPT")
        self.assertFalse(self.paper.decisions.filter(stage__project=other).exists())

    def test_content_pass_is_pending_references_not_final_selection(self):
        self.pass_abstract()
        metrics = build_metrics(self.project)
        self.assertEqual(metrics["reference_pending"], 1)
        self.assertEqual(metrics["final_included"], 0)
        self.assertEqual(metrics["stages"]["Abstract"], {"pending": 1, "accept": 0, "reject": 0})
        self.assertEqual(keyword_stage_counts(self.project)["navigation"]["abstract"], 0)
        self.assertEqual(stage_year_cumulative(self.project)["series"]["abstract"], [0])
        self.assertEqual(build_graph(self.project, seed_stage="final")["nodes"], [])
        exported = self.client.get(reverse("references_download", args=[self.project.pk, "final"]))
        self.assertNotContains(exported, self.paper.title)
        self.assertIn("awaiting references, not finally selected", build_markdown(self.project))
        self.assertContains(self.client.get(self.reference_url), self.paper.title)

    def test_manually_pasted_bibliography_is_retained_and_finalizes(self):
        self.pass_abstract()
        text = "Smith (2020). A paper without a DOI.\nhttps://doi.org/10.1000/example\nhttps://arxiv.org/abs/2307.01613"
        response = self.client.post(
            self.reference_url,
            {"paper_id": self.paper.pk, "action": "save", "reference_text": text},
        )
        self.assertEqual(response.status_code, 302)
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.reference_text, text)
        self.assertEqual(
            self.paper.references,
            ["https://doi.org/10.1000/example", "https://doi.org/10.48550/arxiv.2307.01613"],
        )
        self.assertEqual(self.paper.raw["manual_metadata"]["references"]["source"], "manual")
        self.assertFalse(pending_references(self.project).exists())
        self.assertEqual(final_decisions(self.project).count(), 1)
        self.assertEqual(build_metrics(self.project)["final_included"], 1)
        self.assertEqual(stage_year_cumulative(self.project)["series"]["abstract"], [1])
        self.assertIn("Smith (2020)", build_markdown(self.project))
        self.assertEqual(build_graph(self.project)["edges"], [])

    def test_unresolved_plain_bibliography_counts_as_available_not_graph_edges(self):
        self.pass_abstract()
        complete_references(self.project, self.paper, "Smith, A. (2020). An unindexed reference.")
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.references, [])
        self.assertEqual(final_decisions(self.project).count(), 1)
        self.assertEqual(build_graph(self.project)["edges"], [])

    def test_blank_bibliography_returns_visible_error_and_stays_pending(self):
        self.pass_abstract()
        response = self.client.post(
            self.reference_url,
            {"paper_id": self.paper.pk, "action": "save", "reference_text": "\n "},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("reference_text", response.context["form"].errors)
        self.assertEqual(pending_references(self.project).count(), 1)
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.reference_text, "")

    def test_repeat_reference_actions_never_overwrite_completed_bibliography(self):
        self.pass_abstract()
        complete_references(
            self.project, self.paper, "The original manually supplied bibliography."
        )
        for action in ("save", "reject", "retry"):
            with patch("literature.services.reference_service.enrich_papers") as provider:
                response = self.client.post(
                    self.reference_url,
                    {"paper_id": self.paper.pk, "action": action, "reference_text": "Replacement"},
                    follow=True,
                )
            self.assertContains(response, "already available")
            provider.assert_not_called()
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.reference_text, "The original manually supplied bibliography.")
        self.assertEqual(self.paper.decisions.get(stage=self.abstract).decision, "ACCEPT")

    def test_reference_rejection_is_audited_as_abstract_reject(self):
        self.pass_abstract()
        response = self.client.post(
            self.reference_url, {"paper_id": self.paper.pk, "action": "reject"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.paper.decisions.get(stage=self.abstract).decision, "REJECT")
        self.assertEqual(self.paper.decisions.get(stage=self.title).decision, "ACCEPT")
        revision = ScreeningRevision.objects.get(paper=self.paper, stage=self.abstract)
        self.assertEqual((revision.previous_decision, revision.new_decision), ("ACCEPT", "REJECT"))
        self.assertIn("reference completion", revision.reason)
        self.assertEqual(build_metrics(self.project)["stages"]["Abstract"]["reject"], 1)
        self.assertEqual(build_metrics(self.project)["reference_pending"], 0)
        self.assertEqual(final_decisions(self.project).count(), 0)

    def test_reference_actions_are_project_scoped_and_after_content_pass_only(self):
        self.assertEqual(
            self.client.post(
                self.reference_url, {"paper_id": self.paper.pk, "action": "reject"}
            ).status_code,
            404,
        )
        self.pass_abstract()
        other = Project.objects.create(name="Other")
        self.assertEqual(
            self.client.post(
                reverse("reference_review", args=[other.pk]),
                {"paper_id": self.paper.pk, "action": "reject"},
            ).status_code,
            404,
        )
        self.assertEqual(self.paper.decisions.get(stage=self.abstract).decision, "ACCEPT")

    def test_provider_retry_is_explicit_post_and_can_finalize(self):
        self.pass_abstract()
        with patch("literature.services.reference_service.enrich_papers") as provider:
            self.client.get(self.reference_url)
            provider.assert_not_called()

        def fill(papers):
            papers[0].references = ["https://doi.org/10.1000/retrieved"]
            papers[0].save(update_fields=["references"])

        with patch("literature.services.reference_service.enrich_papers", side_effect=fill):
            self.assertEqual(
                self.client.post(
                    self.reference_url, {"paper_id": self.paper.pk, "action": "retry"}
                ).status_code,
                302,
            )
        self.assertEqual(final_decisions(self.project).count(), 1)

    def test_finished_abstract_round_points_to_reference_queue(self):
        self.pass_title()
        save_missing_abstract(self.project, self.paper, "An abstract")
        response = self.client.post(
            reverse("screening_decide"),
            {"paper_id": self.paper.pk, "stage_id": self.abstract.pk, "decision": "ACCEPT"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["next_paper_url"], self.reference_url)

    def test_existing_reference_list_skips_reference_completion_queue(self):
        self.paper.references = ["https://doi.org/10.1000/known"]
        self.paper.save(update_fields=["references"])
        self.pass_abstract()
        self.assertFalse(pending_references(self.project).exists())
        self.assertEqual(final_decisions(self.project).count(), 1)

    def test_graphs_have_no_incomplete_category(self):
        self.pass_abstract()
        for name in ("project_detail", "plots_view", "plots_download"):
            page = self.client.get(reverse(name, args=[self.project.pk]))
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, "Incomplete papers by source")
            self.assertNotContains(page, "Where incomplete papers were excluded")

    def test_unqueued_untitled_records_are_not_reported_as_pending(self):
        from literature.services.plots_service import build_funnel_figure

        decide(paper=self.paper, stage=self.title, decision="REJECT")
        for _ in range(6):
            draft = Paper.objects.create(title="")
            Discovery.objects.create(
                paper=draft, search=self.search, source="crossref", query="navigation"
            )
        metrics = build_metrics(self.project)
        self.assertEqual(metrics["stages"]["Title"]["pending"], 0)
        self.assertEqual(metrics["title_not_queued"], 6)
        with patch(
            "plotly.graph_objects.Figure.to_html", autospec=True, return_value="chart"
        ) as render:
            build_funnel_figure(metrics)
        figure = render.call_args.args[0]
        links = figure.data[0].link
        pending_index = list(figure.data[0].node.label).index("Title: pending")
        self.assertEqual(
            [value for target, value in zip(links.target, links.value) if target == pending_index],
            [0],
        )
        self.assertIn("6 stored papers are not queued", figure.layout.annotations[0].text)

    def test_doi_punctuation_and_duplicate_identifiers(self):
        self.assertEqual(
            extract_reference_ids("Citation (10.1000/example).\n10.1000/example\n10.1000/foo(bar)"),
            ["https://doi.org/10.1000/example", "https://doi.org/10.1000/foo(bar)"],
        )

    def test_arxiv_ids_in_bibliography_prose_match_cached_arxiv_papers(self):
        self.pass_abstract()
        cited = Paper.objects.create(
            title="Cited arXiv paper", source_ids={"arxiv": "2307.01613v1"}
        )
        Discovery.objects.create(
            paper=cited, search=self.search, source="arxiv", query="navigation"
        )
        complete_references(
            self.project, self.paper, "Kremer et al. (2023). S-Nav. arXiv:2307.01613."
        )
        self.assertEqual(build_graph(self.project, depth=1)["edges"], [(self.paper.pk, cited.pk)])

    def test_resolved_manual_reference_matches_only_discovered_targets(self):
        self.pass_abstract()
        cited = Paper.objects.create(title="Cited paper", doi="10.1000/example")
        outsider = Paper.objects.create(title="Outside this project", doi="10.1000/outside")
        Discovery.objects.create(
            paper=cited, search=self.search, source="crossref", query="navigation"
        )
        complete_references(self.project, self.paper, "10.1000/example\n10.1000/outside")
        self.assertEqual(build_graph(self.project, depth=1)["edges"], [(self.paper.pk, cited.pk)])
        self.assertNotIn(outsider.pk, {node["id"] for node in build_graph(self.project)["nodes"]})

    def test_migration_admits_existing_excluded_papers_preserving_decisions(self):
        self.paper.decisions.all().delete()
        rejected = Paper.objects.create(title="Already rejected")
        Discovery.objects.create(
            paper=rejected, search=self.search, source="manual", query="navigation"
        )
        ScreeningDecision.objects.create(paper=rejected, stage=self.title, decision="REJECT")
        migrate = import_module(
            "literature.migrations.0010_reference_text_and_screening_admission"
        ).admit_titled_papers
        migrate(apps, connection.schema_editor())
        migrate(apps, connection.schema_editor())
        self.assertEqual(self.paper.decisions.get(stage=self.title).decision, "PENDING")
        self.assertEqual(rejected.decisions.get(stage=self.title).decision, "REJECT")
        self.assertEqual(self.paper.decisions.count(), 1)

    def test_backfill_admits_a_paper_after_its_title_becomes_available(self):
        self.paper.decisions.all().delete()
        self.paper.title = ""
        self.paper.save(update_fields=["title"])

        def fill(project):
            Paper.objects.filter(pk=self.paper.pk).update(title="Now titled")
            return {"checked": 1, "filled": 1, "batches": 1}

        with patch(
            "literature.services.reference_service.backfill_missing_references", side_effect=fill
        ):
            response = self.client.post(reverse("refs_backfill", args=[self.project.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.paper.decisions.get(stage=self.title).decision, "PENDING")

    def test_details_edit_cannot_silently_remove_final_reference_list(self):
        from literature.services.manual_paper_service import update_paper_details

        self.paper.references = ["https://doi.org/10.1000/provider-reference"]
        self.paper.save(update_fields=["references"])
        self.pass_abstract()
        with self.assertRaisesMessage(ValueError, "finally selected paper"):
            update_paper_details(
                self.project,
                self.paper,
                {"title": self.paper.title, "abstract": self.paper.abstract, "references": []},
            )
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.references, ["https://doi.org/10.1000/provider-reference"])
        self.assertEqual(final_decisions(self.project).count(), 1)

    def test_manual_bibliography_does_not_trigger_repeated_enrichment(self):
        from literature.services.reference_service import enrich_papers

        self.pass_abstract()
        complete_references(self.project, self.paper, "Smith (2020). An unindexed reference.")
        self.paper.refresh_from_db()
        self.paper.doi = "10.1000/manual"
        with patch("literature.services.reference_service.httpx.Client") as provider:
            self.assertEqual(enrich_papers([self.paper]), 0)
        provider.assert_not_called()

    def test_manual_reference_formats_are_consistent(self):
        from literature.forms import PaperDetailsForm

        form = PaperDetailsForm(
            {"title": "Paper", "references": "S2:s2-paper\nhttps://arxiv.org/pdf/2508.06283"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["references"],
            ["S2:s2-paper", "https://doi.org/10.48550/arxiv.2508.06283"],
        )

    def test_reference_retry_outage_keeps_paper_pending(self):
        import httpx

        self.pass_abstract()
        with patch(
            "literature.services.reference_service.enrich_papers",
            side_effect=httpx.RequestError("Provider unavailable"),
        ):
            response = self.client.post(
                self.reference_url, {"paper_id": self.paper.pk, "action": "retry"}, follow=True
            )
        self.assertContains(response, "Reference lookup is unavailable")
        self.assertEqual(pending_references(self.project).count(), 1)

    def test_json_payload_shape_is_checked_before_screening(self):
        for payload in ([], None, {"paper_id": [], "stage_id": 1, "decision": "ACCEPT"}):
            self.assertEqual(
                self.client.post(
                    reverse("screening_decide"), payload, content_type="application/json"
                ).status_code,
                400,
            )
