"""!
@File: test_paper_entry.py
@Brief: End-to-end tests for read-only DOI previews and atomic entry changes.
@Date: 30/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
"""

from __future__ import annotations

from unittest.mock import patch

from django.core import signing
from django.test import TestCase
from django.urls import reverse

from literature.models import (
    Discovery,
    Paper,
    PaperKeywordAssignment,
    Project,
    ScreeningDecision,
    ScreeningRevision,
    Search,
    SearchRun,
)
from literature.services.paper_lookup_service import save_entry
from literature.services.screening_service import InvalidTransition


class PaperEntryTests(TestCase):
    """! @brief Exercise preview, validation, persistence and project isolation together."""

    def setUp(self) -> None:
        """! @brief Create an isolated project and provider result without live API calls."""
        self.project = Project.objects.create(name="Research")
        self.project.ensure_default_stages()
        self.search = Search.objects.create(
            project=self.project, name="Semantic Navigation", keywords=["navigation", "robotics"]
        )
        self.url = reverse("paper_lookup", args=[self.project.pk]) + "?doi=10.1000/entry"
        self.metadata = {
            "doi": "10.1000/entry",
            "title": "A provider paper",
            "abstract": "Provider summary",
            "authors": [{"name": "Jane Researcher"}],
            "publication_year": 2025,
            "venue": "Research Journal",
            "references": ["https://doi.org/10.1000/ref", "https://openalex.org/W123", "S2:abc123"],
        }
        self.values = {
            "action": "save",
            "origin_source": "arxiv",
            "doi": "10.1000/entry",
            "search_keyword": f"{self.search.pk}|navigation",
            "title_decision": "ACCEPT",
            "abstract_decision": "ACCEPT",
        }

    def preview(self):
        """! @brief Fetch an isolated signed metadata preview without touching SQLite content."""
        with patch("literature.services.reference_service.lookup_doi", return_value=self.metadata):
            return self.client.get(self.url)

    def submit(self, **overrides):
        """!
        @brief Submit a real preview and assert that saving never calls the providers.
        @param overrides Form values overriding the valid entry defaults.
        @return Django test response.
        """
        preview = self.preview()
        data = {**self.values, "preview_token": preview.context["preview_token"], **overrides}
        with patch(
            "literature.services.reference_service.lookup_doi",
            side_effect=AssertionError("Save repeated provider lookup"),
        ):
            return self.client.post(self.url, data)

    def test_preview_is_read_only_complete_and_unselected(self) -> None:
        """! @brief Show all metadata and links without default keyword/status or database writes."""
        page = self.preview()
        self.assertContains(page, "Jane Researcher")
        self.assertContains(page, "Research Journal")
        self.assertContains(page, "2025")
        self.assertContains(page, 'href="https://openalex.org/W123"')
        self.assertContains(page, 'href="https://www.semanticscholar.org/paper/abc123"')
        self.assertContains(page, '<optgroup label="Semantic Navigation">')
        self.assertNotContains(page, 'name="abstract"')
        self.assertNotContains(page, 'name="references"')
        self.assertNotContains(page, 'name="reason"')
        self.assertFalse(Paper.objects.exists())
        self.assertEqual(page.context["form"]["search_keyword"].value(), None)
        self.assertEqual(page.context["form"]["title_decision"].value(), None)
        self.assertContains(page, 'name="origin_source"')
        self.assertContains(page, "Where was this paper found?")

    def test_add_passes_both_stages_and_keeps_provider_metadata(self) -> None:
        """! @brief Persist displayed metadata and both choices, ignoring forged detail fields."""
        response = self.submit(title="Forged title", abstract="Forged abstract", references="junk")
        self.assertEqual(response.status_code, 302)
        paper = Paper.objects.get(doi=self.metadata["doi"])
        self.assertEqual(paper.title, self.metadata["title"])
        self.assertEqual(paper.abstract, self.metadata["abstract"])
        self.assertEqual(paper.authors[0]["name"], "Jane Researcher")
        self.assertEqual(paper.publication_year, 2025)
        self.assertEqual(paper.venue, "Research Journal")
        self.assertEqual(paper.references, self.metadata["references"])
        self.assertEqual(
            dict(paper.decisions.values_list("stage__name", "decision")),
            {"Title": "ACCEPT", "Abstract": "ACCEPT"},
        )
        self.assertEqual(PaperKeywordAssignment.objects.get(paper=paper).keyword, "navigation")

    def test_added_paper_uses_selected_origin_not_manual_category(self) -> None:
        self.assertEqual(self.submit(origin_source="publisher").status_code, 302)
        paper = Paper.objects.get()
        self.assertEqual(paper.sources, ["publisher"])
        discovery = paper.discoveries.get()
        self.assertEqual(discovery.source, "publisher")
        self.assertEqual(discovery.raw_payload["entry_method"], "user_added")
        self.assertEqual(SearchRun.objects.get().source, "publisher")
        self.assertFalse(Discovery.objects.filter(source="manual").exists())

    def test_new_entry_requires_explicit_discovery_origin(self) -> None:
        response = self.submit(origin_source="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("origin_source", response.context["form"].errors)
        self.assertContains(response, "This field is required.")
        self.assertFalse(Paper.objects.exists())

    def test_legacy_origin_can_be_selected_by_user_without_rewriting_history(self) -> None:
        """Only a synthetic user POST records a known origin; a preview changes nothing."""
        paper = Paper.objects.create(**self.metadata, sources=["manual"], keywords=["navigation"])
        old_run = SearchRun.objects.create(
            search=self.search,
            source="manual",
            status="completed",
            query_used="navigation",
            results_count=1,
            new_papers_count=1,
        )
        old_discovery = Discovery.objects.create(
            paper=paper,
            search=self.search,
            search_run=old_run,
            source="manual",
            query="navigation",
        )
        PaperKeywordAssignment.objects.create(
            paper=paper,
            project=self.project,
            search=self.search,
            keyword="navigation",
        )
        for name, decision in (("Title", "ACCEPT"), ("Abstract", "PENDING")):
            ScreeningDecision.objects.create(
                paper=paper,
                stage=self.project.stages.get(name=name),
                decision=decision,
            )
        models = (Paper, Discovery, SearchRun, ScreeningDecision, PaperKeywordAssignment)
        before = [list(model.objects.values()) for model in models]
        page = self.preview()
        self.assertEqual([list(model.objects.values()) for model in models], before)
        self.assertContains(page, 'name="origin_source"')
        self.assertContains(page, reverse("paper_manage", args=[self.project.pk, paper.pk]))
        self.assertContains(page, "Edit paper details manually")
        self.assertEqual(
            self.submit(origin_source="publisher", abstract_decision="PENDING").status_code, 302
        )
        old_discovery.refresh_from_db()
        old_run.refresh_from_db()
        self.assertEqual(old_discovery.source, "manual")
        self.assertEqual(
            (old_run.source, old_run.results_count, old_run.new_papers_count), ("manual", 1, 1)
        )
        self.assertTrue(paper.discoveries.filter(source="publisher").exists())
        self.assertEqual(list(ScreeningDecision.objects.values()), before[3])
        paper.refresh_from_db()
        for name in ("title", "authors", "abstract", "publication_year", "venue", "references"):
            self.assertEqual(getattr(paper, name), self.metadata[name])
        table = self.client.get(reverse("papers_view", args=[self.project.pk]))
        self.assertEqual(table.context["rows"][0]["display_sources"], ["publisher"])

    def test_existing_origin_is_not_changed_by_status_update(self) -> None:
        self.submit(origin_source="crossref")
        page = self.preview()
        self.assertNotContains(page, 'name="origin_source"')
        self.assertEqual(self.submit(origin_source="arxiv").status_code, 302)
        self.assertEqual(Paper.objects.get().sources, ["crossref"])
        self.assertEqual(Discovery.objects.get().source, "crossref")

    def test_provider_ids_survive_creation_for_citation_matching(self) -> None:
        """! @brief Keep the paper's own OpenAlex and S2 identifiers for incoming graph edges."""
        self.metadata["openalex_id"] = "W1234567"
        self.metadata["source_ids"] = {"openalex": "W1234567", "semantic_scholar": "abcdef"}
        self.assertEqual(self.submit().status_code, 302)
        paper = Paper.objects.get()
        self.assertEqual(paper.openalex_id, "W1234567")
        self.assertEqual(paper.source_ids, self.metadata["source_ids"])

    def test_missing_status_and_keyword_produce_visible_field_errors(self) -> None:
        """! @brief Require explicit assignment and screening choices without side effects."""
        response = self.submit(search_keyword="", title_decision="", abstract_decision="")
        self.assertContains(response, "Entry not saved")
        self.assertEqual(
            set(response.context["form"].errors),
            {"search_keyword", "title_decision", "abstract_decision"},
        )
        self.assertFalse(Paper.objects.exists())
        self.assertFalse(SearchRun.objects.exists())

    def test_new_keyword_requires_name_and_destination(self) -> None:
        """! @brief Require both new-keyword fields only for the explicit creation option."""
        response = self.submit(search_keyword="NEW", new_keyword="", new_keyword_search="")
        self.assertEqual(
            set(response.context["form"].errors), {"new_keyword", "new_keyword_search"}
        )
        self.assertFalse(Paper.objects.exists())

    def test_existing_selection_ignores_stale_new_keyword_fields(self) -> None:
        """! @brief Previously typed creation fields cannot override a selected existing keyword."""
        self.assertEqual(
            self.submit(new_keyword="unwanted", new_keyword_search=str(self.search.pk)).status_code,
            302,
        )
        self.search.refresh_from_db()
        self.assertNotIn("unwanted", self.search.keywords)

    def test_impossible_abstract_choice_does_not_create_keyword(self) -> None:
        """! @brief Validate Title/Abstract dependency before saving any keyword or paper."""
        response = self.submit(
            search_keyword="NEW",
            new_keyword="new",
            new_keyword_search=str(self.search.pk),
            title_decision="REJECT",
        )
        self.assertIn("abstract_decision", response.context["form"].errors)
        self.search.refresh_from_db()
        self.assertNotIn("new", self.search.keywords)
        self.assertFalse(Paper.objects.exists())

    def test_mid_save_failure_rolls_back_everything(self) -> None:
        """! @brief A failing second correction rolls back the first and all entry writes."""
        from literature.services.screening_service import correct_decision

        calls = 0

        def fail_second(**kwargs):
            """! @brief Allow Title then inject a failure in Abstract correction."""
            nonlocal calls
            calls += 1
            if calls == 2:
                raise InvalidTransition("Injected abstract failure")
            return correct_decision(**kwargs)

        with patch(
            "literature.services.paper_lookup_service.correct_decision", side_effect=fail_second
        ):
            response = self.submit(
                search_keyword="NEW", new_keyword="rollback", new_keyword_search=str(self.search.pk)
            )
        self.assertContains(response, "Injected abstract failure")
        self.search.refresh_from_db()
        self.assertNotIn("rollback", self.search.keywords)
        for model in (
            Paper,
            Discovery,
            SearchRun,
            PaperKeywordAssignment,
            ScreeningDecision,
            ScreeningRevision,
        ):
            self.assertFalse(model.objects.exists(), model.__name__)

    def test_preview_tampering_expiry_and_project_replay_are_rejected(self) -> None:
        """! @brief Refuse unauthenticated, expired and wrong-project preview data."""
        token = self.preview().context["preview_token"]
        response = self.submit(preview_token=token + "broken")
        self.assertContains(response, "preview has expired or is invalid")
        with patch("django.core.signing.time.time", return_value=0):
            expired = signing.dumps(
                {
                    "project": self.project.pk,
                    "doi": self.metadata["doi"],
                    "metadata": self.metadata,
                    "paper_id": None,
                },
                salt="paper-entry",
            )
        self.assertContains(self.submit(preview_token=expired), "preview has expired or is invalid")
        wrong_project = signing.dumps(
            {
                "project": 123456,
                "doi": self.metadata["doi"],
                "metadata": self.metadata,
                "paper_id": None,
            },
            salt="paper-entry",
        )
        self.assertContains(
            self.submit(preview_token=wrong_project), "preview has expired or is invalid"
        )
        self.assertFalse(Paper.objects.exists())

    def test_retrying_same_new_preview_does_not_duplicate_paper_or_audit(self) -> None:
        """! @brief Double submission returns a stale-preview error without additional writes."""
        token = self.preview().context["preview_token"]
        data = {**self.values, "preview_token": token}
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        before = (SearchRun.objects.count(), ScreeningRevision.objects.count())
        self.assertContains(self.client.post(self.url, data), "changed since this preview")
        self.assertEqual(Paper.objects.count(), 1)
        self.assertEqual((SearchRun.objects.count(), ScreeningRevision.objects.count()), before)

    def test_reassignment_is_current_and_does_not_rewrite_discovery(self) -> None:
        """! @brief Change the assignment on update while retaining original discovery history."""
        self.submit()
        paper = Paper.objects.get(doi=self.metadata["doi"])
        discovery = paper.discoveries.get()
        response = self.submit(search_keyword=f"{self.search.pk}|robotics")
        self.assertEqual(response.status_code, 302)
        paper.refresh_from_db()
        discovery.refresh_from_db()
        self.assertEqual(discovery.query, "navigation")
        self.assertEqual(paper.keyword_assignments.get().keyword, "robotics")
        self.assertEqual(paper.keywords, ["robotics"])
        self.assertEqual(
            self.preview().context["form"]["search_keyword"].value(), f"{self.search.pk}|robotics"
        )
        self.assertEqual(paper.title, self.metadata["title"])

    def test_unchanged_update_is_success_without_duplicate_audit(self) -> None:
        """! @brief Treat an unchanged update as a no-op, not an invalid transition."""
        self.submit()
        before = (SearchRun.objects.count(), ScreeningRevision.objects.count())
        self.assertEqual(self.submit().status_code, 302)
        self.assertEqual((SearchRun.objects.count(), ScreeningRevision.objects.count()), before)

    def test_title_rejection_removes_previous_abstract_status(self) -> None:
        """! @brief Apply Title dependency to an already accepted Abstract without stale skips."""
        self.submit()
        self.assertEqual(
            self.submit(title_decision="REJECT", abstract_decision="NOT_QUEUED").status_code, 302
        )
        self.assertFalse(ScreeningDecision.objects.filter(stage__name="Abstract").exists())
        self.assertTrue(ScreeningRevision.objects.filter(new_decision="REMOVED").exists())

    def test_global_existing_paper_does_not_fetch_or_modify_metadata(self) -> None:
        """! @brief Link a shared record without overwriting metadata or other-project statuses."""
        other = Project.objects.create(name="Other")
        other.ensure_default_stages()
        other_search = Search.objects.create(
            project=other, name="Other search", keywords=["robotics"]
        )
        paper = Paper.objects.create(**self.metadata)
        Discovery.objects.create(
            paper=paper, search=other_search, source="openalex", query="robotics"
        )
        ScreeningDecision.objects.create(
            paper=paper, stage=other.stages.get(name="Title"), decision="REJECT"
        )
        with patch(
            "literature.services.reference_service.lookup_doi",
            side_effect=AssertionError("Fetched stored paper"),
        ):
            page = self.client.get(self.url)
            response = self.client.post(
                self.url,
                {
                    **self.values,
                    "preview_token": page.context["preview_token"],
                    "abstract": "tampered",
                },
            )
        self.assertContains(page, "Update entry")
        self.assertNotContains(page, "Edit paper details manually")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Paper.objects.count(), 1)
        paper.refresh_from_db()
        self.assertEqual(paper.abstract, self.metadata["abstract"])
        self.assertEqual(paper.decisions.get(stage__project=other).decision, "REJECT")
        self.assertContains(self.preview(), "Edit paper details manually")

    def test_invalid_doi_never_calls_providers(self) -> None:
        """! @brief Validate directly visited lookup URLs as well as the DOI search form."""
        with patch("literature.services.reference_service.lookup_doi") as provider:
            response = self.client.get(
                reverse("paper_lookup", args=[self.project.pk]) + "?doi=garbage"
            )
        provider.assert_not_called()
        self.assertEqual(response.status_code, 302)

    def test_missing_references_does_not_block_content_screening(self) -> None:
        """Missing references defer final selection, not Title or Abstract screening."""
        from literature.services.screening_service import pending_references

        self.metadata["references"] = []
        self.assertEqual(self.submit().status_code, 302)
        self.assertEqual(pending_references(self.project).count(), 1)
        Paper.objects.all().delete()
        self.metadata["title"] = ""
        self.assertContains(
            self.submit(title_decision="PENDING", abstract_decision="NOT_QUEUED"),
            "No paper title was found",
        )
        self.assertFalse(Paper.objects.exists())

    def test_stale_keyword_is_revalidated_at_transaction_boundary(self) -> None:
        """! @brief A keyword removed after form validation cannot be saved."""
        self.search.keywords = ["robotics"]
        self.search.save()
        with self.assertRaisesMessage(ValueError, "no longer in the search"):
            save_entry(self.project, self.metadata["doi"], self.metadata, self.values, None)
        self.assertFalse(Paper.objects.exists())

    def test_assignment_is_visible_in_table_and_keyword_counts(self) -> None:
        """! @brief Explicit reassignment takes precedence over stale text-based keyword matches."""
        from literature.services.metrics_service import keyword_stage_counts

        self.metadata["abstract"] = "The navigation abstract mentions navigation many times."
        self.submit()
        self.submit(search_keyword=f"{self.search.pk}|robotics")
        counts = keyword_stage_counts(self.project)
        self.assertEqual(counts["navigation"]["found"], 0)
        self.assertEqual(counts["robotics"]["abstract"], 1)
        page = self.client.get(reverse("papers_view", args=[self.project.pk]))
        self.assertContains(page, "Linked keyword: robotics")

    def test_duplicate_merge_retains_assignment_for_each_project(self) -> None:
        """! @brief Assignment rows survive normal keep/drop deduplication."""
        from literature.services.dedup_service import merge_papers

        self.submit()
        drop = Paper.objects.get(doi=self.metadata["doi"])
        keep = Paper.objects.create(title="Retained paper")
        merge_papers(keep=keep, drop=drop)
        assignment = PaperKeywordAssignment.objects.get(project=self.project)
        self.assertEqual(assignment.paper_id, keep.pk)
        self.assertEqual(assignment.keyword, "navigation")

    def test_old_tab_cannot_overwrite_newer_status_or_assignment(self) -> None:
        """! @brief Reject stale updates even when the DOI still resolves to the same paper."""
        self.submit()
        token = self.preview().context["preview_token"]
        self.submit(search_keyword=f"{self.search.pk}|robotics")
        response = self.client.post(self.url, {**self.values, "preview_token": token})
        self.assertContains(response, "updated after you opened it")
        self.assertEqual(PaperKeywordAssignment.objects.get().keyword, "robotics")

    def test_database_lock_returns_retry_message(self) -> None:
        """! @brief A contending SQLite writer produces a retry message, not an HTTP 500."""
        from django.db import OperationalError

        with patch(
            "literature.services.paper_lookup_service.save_entry",
            side_effect=OperationalError("database is locked"),
        ):
            self.assertContains(self.submit(), "Another database save is in progress")
        self.assertFalse(Paper.objects.exists())

    def test_existing_incomplete_metadata_cannot_gain_screening_decisions(self) -> None:
        """! @brief Stored metadata gaps cannot be bypassed by an update or forged preview."""
        paper = Paper.objects.create(title="Incomplete", doi=self.metadata["doi"])
        Discovery.objects.create(
            paper=paper, search=self.search, source="openalex", query="navigation"
        )
        self.assertContains(self.submit(), "No abstract found")
        self.assertFalse(ScreeningDecision.objects.exists())
        self.assertFalse(PaperKeywordAssignment.objects.exists())

    def test_reassignment_retains_provider_attributed_keyword_index(self) -> None:
        """! @brief Preserve provider keywords while changing this project's explicit assignment."""
        self.submit()
        paper = Paper.objects.get(doi=self.metadata["doi"])
        Discovery.objects.create(
            paper=paper, search=self.search, source="openalex", query="navigation"
        )
        self.submit(search_keyword=f"{self.search.pk}|robotics")
        paper.refresh_from_db()
        self.assertEqual(paper.keywords, ["navigation", "robotics"])
        self.assertEqual(paper.keyword_assignments.get().keyword, "robotics")
