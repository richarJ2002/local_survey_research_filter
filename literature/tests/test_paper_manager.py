#!/usr/bin/env python3

'''
@File:   test_paper_manager.py

@Brief:  Project paper table, manual additions and audited status corrections.

@Date:   30/09/2026

@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
'''

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from literature.models import (
    Discovery, Paper, Project, ScreeningDecision, ScreeningRevision, Search, SearchRun,
)
from literature.services.screening_service import InvalidTransition, decide


class PaperManagerTests(TestCase):
    """Curated additions still pass through screening; corrections remain auditable."""

    def setUp(self) -> None:
        self.project = Project.objects.create(name="Project A")
        self.project.ensure_default_stages()
        self.search = Search.objects.create(
            project=self.project, name="Primary", keywords=["robotics"],
        )
        self.keyword = f"{self.search.pk}|robotics"
        self.title_stage = self.project.stages.get(name="Title")
        self.abstract_stage = self.project.stages.get(name="Abstract")

    def _add(self, **overrides):
        data = {
            "search_keyword": self.keyword,
            "origin_source": "crossref",
            "title": "A missed study",
            "doi": "",
            "authors": "A. Author\nB. Author",
            "publication_year": "2023",
            "venue": "Journal A",
            "abstract": "A complete summary",
            "references": "https://doi.org/10.1000/older",
        }
        data.update(overrides)
        return self.client.post(reverse("paper_add", args=[self.project.pk]), data)

    def _lookup_post(self, url: str, data: dict):
        """Submit the authenticated preview returned by the lookup page."""
        preview = self.client.get(url)
        return self.client.post(url, {"origin_source": "crossref", **data, "preview_token": preview.context["preview_token"]})

    def test_add_complete_paper_is_keyword_attributed_and_title_pending(self) -> None:
        response = self._add()
        self.assertEqual(response.status_code, 302)
        paper = Paper.objects.get(title="A missed study")
        self.assertEqual(paper.keywords, ["robotics"])
        self.assertEqual(paper.references, ["https://doi.org/10.1000/older"])
        self.assertEqual(paper.author_display(), "A. Author, B. Author")
        discovery = Discovery.objects.get(paper=paper, search=self.search, source="crossref")
        self.assertEqual(discovery.query, "robotics")
        run = SearchRun.objects.get(pk=discovery.search_run_id)
        self.assertEqual((run.results_count, run.new_papers_count, run.incomplete_count), (1, 1, 0))
        self.assertEqual(ScreeningDecision.objects.get(paper=paper, stage=self.title_stage).decision,
                         "PENDING")
        self.assertFalse(ScreeningDecision.objects.filter(paper=paper, stage=self.abstract_stage).exists())

    def test_manual_metadata_entry_requires_source_selection(self) -> None:
        response = self._add(origin_source="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Paper.objects.exists())
        response = self._add(origin_source="arxiv")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Discovery.objects.get().source, "arxiv")
        self.assertEqual(SearchRun.objects.get().source, "arxiv")

    def test_full_doi_url_with_mixed_case_is_normalized(self) -> None:
        """A pasted resolver URL like 10.48550/arXiv.2508.06283 is accepted."""
        with patch("literature.services.reference_service.enrich_papers", return_value=0):
            response = self._add(
                title="", abstract="", references="",
                doi="https://doi.org/10.48550/arXiv.2508.06283",
            )
        self.assertEqual(response.status_code, 302)
        paper = Paper.objects.get(doi="10.48550/arxiv.2508.06283")
        manage = self.client.get(
            reverse("paper_manage", args=[self.project.pk, paper.pk])
        )
        self.assertEqual(manage.status_code, 200)

    def test_invalid_reference_does_not_create_a_screenable_paper(self) -> None:
        response = self._add(references="S2:")
        self.assertContains(response, "Unrecognized reference identifier")
        self.assertFalse(Paper.objects.filter(title="A missed study").exists())

    def test_existing_doi_is_linked_without_overwriting_curated_metadata(self) -> None:
        other = Project.objects.create(name="Other")
        search = Search.objects.create(project=other, name="Other search", keywords=["a"])
        paper = Paper.objects.create(
            title="Original title", doi="10.1000/existing", abstract="Original summary",
            references=["https://doi.org/10.1000/ref"],
        )
        Discovery.objects.create(paper=paper, search=search, source="crossref", query="a")
        response = self._add(title="Different title", doi="10.1000/existing")
        self.assertEqual(response.status_code, 302)
        paper.refresh_from_db()
        self.assertEqual(paper.title, "Original title")
        self.assertEqual(paper.abstract, "Original summary")
        self.assertEqual(Paper.objects.count(), 1)
        self.assertEqual(SearchRun.objects.get(source="crossref").duplicates_count, 1)
        self.assertTrue(ScreeningDecision.objects.filter(
            paper=paper, stage=self.title_stage, decision="PENDING",
        ).exists())

    def test_existing_paper_can_gain_another_keyword_without_new_paper(self) -> None:
        self._add()
        self.search.keywords = ["robotics", "mapping"]
        self.search.save()
        self.keyword = f"{self.search.pk}|mapping"
        self._add()
        paper = Paper.objects.get(title="A missed study")
        self.assertEqual(Paper.objects.count(), 1)
        self.assertEqual(paper.keywords, ["mapping", "robotics"])
        self.assertTrue(SearchRun.objects.filter(source="crossref", query_used="mapping",
                                                 duplicates_count=1).exists())

    def test_incomplete_doi_addition_can_be_completed_without_rerunning_search(self) -> None:
        with patch("literature.services.reference_service.enrich_papers", return_value=0):
            response = self._add(title="", abstract="", references="", doi="10.1000/missed")
        self.assertEqual(response.status_code, 302)
        paper = Paper.objects.get(doi="10.1000/missed")
        self.assertFalse(ScreeningDecision.objects.filter(paper=paper, stage=self.title_stage).exists())
        self.assertEqual(SearchRun.objects.get(source="crossref").incomplete_count, 1)
        manage_url = reverse("paper_manage", args=[self.project.pk, paper.pk])
        filled = self.client.post(manage_url, {
            "action": "details", "title": "Completed by hand", "doi": paper.doi,
            "authors": "A. Author", "abstract": "Now has a summary",
            "references": "10.1000/older", "publication_year": "2024", "venue": "Venue",
        })
        self.assertEqual(filled.status_code, 302)
        self.assertEqual(ScreeningDecision.objects.get(paper=paper, stage=self.title_stage).decision,
                         "PENDING")

    def test_doi_lookup_completes_before_initial_queue_decision(self) -> None:
        def fill(papers):
            paper = papers[0]
            paper.title = "Lookup title"
            paper.abstract = "Lookup abstract"
            paper.references = ["https://doi.org/10.1000/older"]
            paper.save(update_fields=["title", "abstract", "references"])
            return 1

        with patch("literature.services.reference_service.enrich_papers", side_effect=fill):
            self._add(title="", abstract="", references="", doi="10.1000/lookup")
        paper = Paper.objects.get(doi="10.1000/lookup")
        self.assertEqual(SearchRun.objects.get(source="crossref").incomplete_count, 0)
        self.assertTrue(ScreeningDecision.objects.filter(
            paper=paper, stage=self.title_stage, decision="PENDING",
        ).exists())

    def test_papers_view_search_is_project_scoped(self) -> None:
        self._add()
        other = Project.objects.create(name="Other")
        other_search = Search.objects.create(project=other, name="S2", keywords=["robotics"])
        alien = Paper.objects.create(title="Another study", doi="10.1000/alien")
        Discovery.objects.create(paper=alien, search=other_search, source="manual", query="robotics")
        page = self.client.get(reverse("papers_view", args=[self.project.pk]) + "?q=missed")
        self.assertContains(page, "A missed study")
        self.assertNotContains(page, "Another study")
        detail = reverse("paper_manage", args=[self.project.pk, alien.pk])
        self.assertEqual(self.client.get(detail).status_code, 404)
        project_page = self.client.get(reverse("project_detail", args=[self.project.pk]))
        self.assertContains(project_page, "View Papers")

    def test_correcting_title_invalidates_abstract_with_audit_history(self) -> None:
        self._add()
        paper = Paper.objects.get(title="A missed study")
        url = reverse("paper_manage", args=[self.project.pk, paper.pk])

        def correct(stage, decision):
            return self.client.post(url, {
                "action": "status", "stage": stage,
                "decision": decision,
            })

        self.assertEqual(correct("Title", "ACCEPT").status_code, 302)
        self.assertTrue(ScreeningDecision.objects.filter(
            paper=paper, stage=self.abstract_stage, decision="PENDING",
        ).exists())
        self.assertEqual(correct("Abstract", "ACCEPT").status_code, 302)
        self.assertEqual(correct("Title", "REJECT").status_code, 302)
        self.assertFalse(ScreeningDecision.objects.filter(paper=paper, stage=self.abstract_stage).exists())
        self.assertEqual(ScreeningRevision.objects.filter(paper=paper).count(), 4)
        self.assertTrue(ScreeningRevision.objects.filter(
            paper=paper, stage=self.abstract_stage, previous_decision="ACCEPT",
            new_decision="REMOVED",
        ).exists())
        blocked = correct("Abstract", "ACCEPT")
        self.assertContains(blocked, "Accept the title before changing the abstract status")
        self.assertEqual(correct("Title", "ACCEPT").status_code, 302)
        self.assertTrue(ScreeningDecision.objects.filter(
            paper=paper, stage=self.abstract_stage, decision="PENDING",
        ).exists())
        with self.assertRaises(InvalidTransition):
            decide(paper=paper, stage=self.title_stage, decision="REJECT")

    def test_paper_not_complete_cannot_be_forced_to_yes(self) -> None:
        with patch("literature.services.reference_service.enrich_papers", return_value=0):
            self._add(title="", abstract="", references="", doi="10.1000/incomplete")
        paper = Paper.objects.get(doi="10.1000/incomplete")
        response = self.client.post(reverse("paper_manage", args=[self.project.pk, paper.pk]), {
            "action": "status", "stage": "Title", "decision": "ACCEPT",
        })
        self.assertContains(response, "A title is required before Title screening")
        self.assertEqual(ScreeningRevision.objects.count(), 0)

    def test_doi_search_opens_lookup_page(self) -> None:
        """Searching a DOI redirects to its lookup result page."""
        response = self.client.post(reverse("paper_add", args=[self.project.pk]), {
            "action": "search", "doi": "https://doi.org/10.1000/looked-up",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("/papers/lookup/?doi=10.1000%2Flooked-up", response["Location"])

    def test_lookup_new_paper_shows_unified_add_panel(self) -> None:
        """Unknown DOIs show read-only details plus keyword and filtering choices."""
        fetched = {"doi": "10.1000/fresh", "title": "Fresh find",
                   "abstract": "Found abstract", "references": ["https://doi.org/10.1000/ref"],
                   "found": True, "error": ""}
        with patch("literature.services.reference_service.lookup_doi", return_value=fetched):
            page = self.client.get(reverse("paper_lookup", args=[self.project.pk]) + "?doi=10.1000/fresh")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "This paper is not in the database.")
        self.assertContains(page, "Fresh find")
        self.assertContains(page, "Add entry")
        self.assertContains(page, "Linked keyword")
        self.assertContains(page, "Add new keyword")
        self.assertContains(page, "Title screening")
        self.assertContains(page, "Abstract screening")
        self.assertNotContains(page, "Update entry")
        self.assertNotContains(page, 'name="title"')
        self.assertNotContains(page, 'name="abstract"')
        self.assertNotContains(page, 'name="references"')

    def test_lookup_save_creates_pending_paper_from_provider_details(self) -> None:
        """Saving links the keyword and queues Title as Pending."""
        fetched = {"doi": "10.1000/fresh", "title": "Fresh find",
                   "abstract": "Found abstract", "references": ["https://doi.org/10.1000/ref"],
                   "found": True, "error": ""}
        with patch("literature.services.reference_service.lookup_doi", return_value=fetched):
            response = self._lookup_post(
                reverse("paper_lookup", args=[self.project.pk]) + "?doi=10.1000/fresh",
                {"action": "save", "doi": "10.1000/fresh",
                 "search_keyword": self.keyword,
                 "new_keyword": "", "new_keyword_search": "",
                 "title_decision": "PENDING", "abstract_decision": "NOT_QUEUED"},
            )
        self.assertEqual(response.status_code, 302)
        paper = Paper.objects.get(doi="10.1000/fresh")
        self.assertEqual(paper.title, "Fresh find")
        self.assertEqual(paper.abstract, "Found abstract")
        self.assertEqual(paper.keywords, ["robotics"])
        self.assertEqual(ScreeningDecision.objects.get(paper=paper, stage=self.title_stage).decision,
                         "PENDING")
        self.assertFalse(ScreeningDecision.objects.filter(paper=paper, stage=self.abstract_stage).exists())
        self.assertEqual(ScreeningRevision.objects.count(), 0)

    def test_lookup_save_with_new_keyword_adds_it_to_search(self) -> None:
        """Typing a new keyword stores it on the chosen search and links the paper."""
        fetched = {"doi": "10.1000/fresh", "title": "Fresh find",
                   "abstract": "Found abstract", "references": ["https://doi.org/10.1000/ref"],
                   "found": True, "error": ""}
        with patch("literature.services.reference_service.lookup_doi", return_value=fetched):
            response = self._lookup_post(
                reverse("paper_lookup", args=[self.project.pk]) + "?doi=10.1000/fresh",
                {"action": "save", "doi": "10.1000/fresh",
                 "search_keyword": "NEW",
                 "new_keyword": "new-topic", "new_keyword_search": str(self.search.pk),
                 "title_decision": "PENDING", "abstract_decision": "NOT_QUEUED"},
            )
        self.assertEqual(response.status_code, 302)
        self.search.refresh_from_db()
        self.assertIn("new-topic", self.search.keywords)
        paper = Paper.objects.get(doi="10.1000/fresh")
        self.assertIn("new-topic", paper.keywords)

    def test_lookup_save_missing_keyword_shows_error(self) -> None:
        """No keyword choice comes back as an error without saving."""
        fetched = {"doi": "10.1000/fresh", "title": "Fresh find",
                   "abstract": "Found abstract", "references": ["https://doi.org/10.1000/ref"],
                   "found": True, "error": ""}
        with patch("literature.services.reference_service.lookup_doi", return_value=fetched):
            response = self._lookup_post(
                reverse("paper_lookup", args=[self.project.pk]) + "?doi=10.1000/fresh",
                {"action": "save", "doi": "10.1000/fresh",
                 "search_keyword": "", "new_keyword": "",
                 "new_keyword_search": "",
                 "title_decision": "PENDING", "abstract_decision": "NOT_QUEUED"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Paper.objects.filter(doi="10.1000/fresh").exists())

    def test_lookup_title_pass_on_missing_abstract_is_allowed(self) -> None:
        """Title screening does not require an abstract or references."""
        fetched = {"doi": "10.1000/fresh", "title": "Fresh find",
                   "abstract": "", "references": [],
                   "found": True, "error": ""}
        with patch("literature.services.reference_service.lookup_doi", return_value=fetched):
            response = self._lookup_post(
                reverse("paper_lookup", args=[self.project.pk]) + "?doi=10.1000/fresh",
                {"action": "save", "doi": "10.1000/fresh",
                 "search_keyword": self.keyword,
                 "new_keyword": "", "new_keyword_search": "",
                 "title_decision": "ACCEPT", "abstract_decision": "PENDING"},
            )
        self.assertEqual(response.status_code, 302)
        paper = Paper.objects.get(doi="10.1000/fresh")
        self.assertEqual(paper.decisions.get(stage=self.title_stage).decision, "ACCEPT")
        self.assertEqual(paper.decisions.get(stage=self.abstract_stage).decision, "PENDING")

    def test_lookup_existing_paper_shows_update_panel(self) -> None:
        """Known DOIs show read-only stored details with an Update entry button."""
        paper = Paper.objects.create(
            title="Known study", doi="10.1000/known", abstract="Summary",
            references=["https://doi.org/10.1000/ref"],
        )
        Discovery.objects.create(paper=paper, search=self.search, source="manual", query="robotics")
        ScreeningDecision.objects.create(paper=paper, stage=self.title_stage, decision="ACCEPT")
        fetched = {"doi": "10.1000/known", "title": "Known study",
                   "abstract": "Summary", "references": ["https://doi.org/10.1000/ref"],
                   "found": True, "error": ""}
        with patch("literature.services.reference_service.lookup_doi", return_value=fetched):
            page = self.client.get(reverse("paper_lookup", args=[self.project.pk]) + "?doi=10.1000/known")
        self.assertContains(page, "Already in the database")
        self.assertContains(page, "Known study")
        self.assertContains(page, "Update entry")
        self.assertContains(page, "Title screening")
        self.assertNotContains(page, "Add entry")
        self.assertNotContains(page, 'name="title"')
        self.assertNotContains(page, 'name="abstract"')

    def test_lookup_update_entry_changes_keyword_and_states(self) -> None:
        """Update path relinks the keyword and applies Title/Abstract choices."""
        paper = Paper.objects.create(
            title="Known study", doi="10.1000/known", abstract="Summary",
            references=["https://doi.org/10.1000/ref"],
        )
        Discovery.objects.create(paper=paper, search=self.search, source="manual", query="robotics")
        fetched = {"doi": "10.1000/known", "title": "Known study",
                   "abstract": "Summary", "references": ["https://doi.org/10.1000/ref"],
                   "found": True, "error": ""}
        with patch("literature.services.reference_service.lookup_doi", return_value=fetched):
            response = self._lookup_post(
                reverse("paper_lookup", args=[self.project.pk]) + "?doi=10.1000/known",
                {"action": "save", "doi": "10.1000/known",
                 "search_keyword": self.keyword,
                 "new_keyword": "", "new_keyword_search": "",
                 "title_decision": "ACCEPT", "abstract_decision": "PENDING"},
            )
        self.assertEqual(response.status_code, 302)
        paper.refresh_from_db()
        self.assertEqual(paper.title, "Known study")
        self.assertEqual(ScreeningDecision.objects.get(paper=paper, stage=self.title_stage).decision,
                         "ACCEPT")
        self.assertTrue(ScreeningRevision.objects.filter(
            paper=paper, stage=self.title_stage, new_decision="ACCEPT",
        ).exists())
