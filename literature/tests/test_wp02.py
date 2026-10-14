"""Verify the authorized WP02 UI phases using isolated synthetic data.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   test_wp02.py

@Brief:  Synthetic naming, dashboard and screening UI contracts.

@Date:   07/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# @File: test_wp02.py
# @Brief: Synthetic naming, dashboard and screening UI contracts.
# @Date: 07/10/2026

# Regular library imports
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

# Custom library Imports
from django import forms
from django.db import (
    IntegrityError,
    OperationalError,
    close_old_connections,
    transaction,
)
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from literature.forms import WorkspaceNewForm, suggested_project_directory
from literature.models import (
    Discovery,
    Paper,
    PaperKeywordAssignment,
    Project,
    ScreeningDecision,
    Search,
    SearchRun,
)
from literature.project_store import (
    backup_project_store,
    create_project_store,
    open_project_store,
    save_copy_store,
)
from literature.services.search_service import (
    PROVIDERS,
    configuration_preview,
    effective_providers,
    run_search,
    save_current_configuration,
)
from literature.workspace_views import StoreOperationForm

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Name-derived destinations and confirmation stay local ---


class FolderSuggestionTests(SimpleTestCase):
    """Check safe, editable New and Save Copy suggestions."""

    def test_name_unicode_ampersand_and_unsafe_members(self) -> None:
        """Retain Unicode and ampersands while rejecting unsafe members.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                Path(suggested_project_directory(
                    tmp, " Semantic Navigation &Situational Reasoning ",
                )).name, "semantic_navigation_&situational_reasoning",
            )
            self.assertEqual(
                Path(suggested_project_directory(tmp, " Étude\t Über ")).name,
                "étude_über",
            )
            for name in ("", " ", ".", "..", "a/b", "a\\b", "a\0b", "é" * 200):
                with (self.subTest(name=name),
                      self.assertRaises(forms.ValidationError)):
                    suggested_project_directory(tmp, name)
            with self.assertRaises(forms.ValidationError):
                suggested_project_directory("relative-parent", "Name")
            target = str(Path(tmp) / "override")
            form = WorkspaceNewForm({"name": "A/B", "directory": target})
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data["directory"], target)

    def test_new_preview_confirmation_and_collision(self) -> None:
        """Create only after preview and refuse a repeated destination.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"WP_ROLE": "manager", "XDG_DATA_HOME": tmp},
        ):
            data = {"name": "Future Survey", "parent_directory": tmp}
            target = Path(tmp) / "future_survey"
            response = self.client.post(reverse("workspace_new"), data)
            self.assertContains(response, str(target))
            self.assertFalse(target.exists())
            worker = {
                "base": "http://127.0.0.1:12345",
                "prefix": "/p/synthetic/epoch", "project_id": 1,
            }
            token = response.context["new_form"].data["preview_token"]
            with patch(
                "literature.workspace.start_worker", return_value=worker,
            ):
                response = self.client.post(reverse("workspace_new"), {
                    **data, "confirm": "on", "preview_token": token,
                })
            self.assertEqual(response.status_code, 302)
            self.assertTrue((target / "project.sqlite3").is_file())
            original = (target / "project.sqlite3").read_bytes()
            repeated = self.client.post(reverse("workspace_new"), {
                **data, "confirm": "on", "preview_token": token,
            })
            self.assertContains(repeated, "refusing to overwrite")
            self.assertEqual(
                (target / "project.sqlite3").read_bytes(), original)
            override = {**data, "directory": str(Path(tmp) / "chosen-new")}
            preview = self.client.post(reverse("workspace_new"), override)
            token = preview.context["new_form"].data["preview_token"]
            with patch(
                "literature.workspace.start_worker", return_value=worker,
            ):
                saved = self.client.post(reverse("workspace_new"), {
                    **override, "confirm": "on", "preview_token": token,
                })
            self.assertEqual(saved.status_code, 302)
            self.assertTrue(
                (Path(tmp) / "chosen-new" / "project.sqlite3").is_file())

    def test_confirmation_requires_unchanged_signed_preview(self) -> None:
        """Reject missing, forged, expired and changed confirmations.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"WP_ROLE": "manager", "XDG_DATA_HOME": tmp},
        ):
            data = {"name": "A", "parent_directory": tmp, "confirm": "on"}
            first = self.client.post(reverse("workspace_new"), data)
            token = first.context["new_form"].data["preview_token"]
            self.assertFalse((Path(tmp) / "a").exists())
            for changes in ({"name": "B"}, {"directory": str(Path(tmp) / "c")},
                            {"preview_token": token + "forged"}):
                response = self.client.post(reverse("workspace_new"), {
                    **data, "preview_token": token, **changes,
                })
                self.assertContains(response, "Paths changed")
            with patch("django.core.signing.time.time",
                       return_value=time.time() + 1801):
                expired = self.client.post(reverse("workspace_new"), {
                    **data, "preview_token": token,
                })
            self.assertContains(expired, "preview expired")
            self.assertFalse(any((Path(tmp) / name).exists()
                                 for name in ("a", "b", "c")))

    def test_explicit_overlong_path_and_copy_symlink_refused(self) -> None:
        """Keep malformed-path and final-component symlink defenses.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"WP_ROLE": "manager", "XDG_DATA_HOME": tmp},
        ):
            with patch(
                "literature.workspace_views.create_project_store",
            ) as create:
                response = self.client.post(reverse("workspace_new"), {
                    "name": "Safe", "directory": str(Path(tmp) / ("x" * 300)),
                    "preview": "1",
                })
            self.assertContains(response, "File name too long")
            create.assert_not_called()
            source = create_project_store(Path(tmp) / "source", "Safe")
            empty = Path(tmp) / "empty"
            empty.mkdir()
            alias = Path(tmp) / "alias"
            alias.symlink_to(empty, target_is_directory=True)
            with patch("literature.workspace.operate_store") as operate:
                response = self.client.post(reverse("workspace_operation"), {
                    "action": "copy", "source": str(source.location),
                    "destination": str(alias), "preview": "1",
                })
            self.assertContains(response, "symlink; refusing")
            operate.assert_not_called()
            self.assertEqual(list(empty.iterdir()), [])

    def test_copy_staging_name_limit_and_bad_parent(self) -> None:
        """Check expanded parents and the copier's staging byte boundary.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory() as tmp:
            for parent in (str(Path(tmp) / ("x" * 300)),
                           "~wp02_nonexistent_user"):
                form = WorkspaceNewForm({
                    "name": "Safe", "parent_directory": parent,
                })
                self.assertFalse(form.is_valid())
            source = create_project_store(Path(tmp) / "short", "é" * 106)
            form = StoreOperationForm({
                "action": "copy", "source": str(source.location),
                "parent_directory": tmp,
            })
            self.assertTrue(form.is_valid(), form.errors)
            copied = save_copy_store(
                source.location, form.cleaned_data["destination"])
            self.assertTrue(copied.location.is_dir())
            with self.assertRaises(forms.ValidationError):
                suggested_project_directory(tmp, "é" * 107, reserve_bytes=43)

    def test_copy_signed_preview_override_and_identity(self) -> None:
        """Use the stored name, preserve the source and distinguish backup ID.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"WP_ROLE": "manager", "XDG_DATA_HOME": tmp},
        ):
            source = create_project_store(
                Path(tmp) / "project-30",
                "Semantic Navigation &Situational Reasoning",
            )
            original = (source.location / "project.sqlite3").read_bytes()
            data = {"action": "copy", "source": str(source.location),
                    "parent_directory": tmp}
            preview = self.client.post(reverse("workspace_operation"), data)
            destination = (
                Path(tmp) / "semantic_navigation_&situational_reasoning")
            self.assertEqual(
                preview.context["destination_preview"]["resolved"],
                str(destination))
            self.assertFalse(destination.exists())
            token = preview.context["operation_form"].data["preview_token"]
            changed = self.client.post(reverse("workspace_operation"), {
                **data, "destination": str(Path(tmp) / "chosen"),
                "confirm": "on", "preview_token": token,
            })
            self.assertContains(changed, "Paths changed")
            self.assertFalse((Path(tmp) / "chosen").exists())
            override_token = (
                changed.context["operation_form"].data["preview_token"])
            override_saved = self.client.post(reverse("workspace_operation"), {
                **data, "destination": str(Path(tmp) / "chosen"),
                "confirm": "on", "preview_token": override_token,
            })
            self.assertEqual(override_saved.status_code, 302)
            override_copy = open_project_store(Path(tmp) / "chosen")
            self.assertNotEqual(
                override_copy.project_uuid, source.project_uuid)
            confirmed = self.client.post(reverse("workspace_operation"), {
                **data, "confirm": "on", "preview_token": token,
            })
            self.assertEqual(confirmed.status_code, 302)
            copied = open_project_store(destination)
            self.assertNotEqual(copied.project_uuid, source.project_uuid)
            backup = backup_project_store(
                source.location, Path(tmp) / "backup")
            self.assertEqual(backup.project_uuid, source.project_uuid)
            self.assertEqual(
                (source.location / "project.sqlite3").read_bytes(), original)
            override = StoreOperationForm({
                **data, "destination": str(Path(tmp) / "chosen"),
            })
            self.assertTrue(override.is_valid(), override.errors)
            self.assertEqual(override.cleaned_data["destination"],
                             str(Path(tmp) / "chosen"))


class ProjectNameTests(TestCase):
    """Require an explicit owning-project POST for display-name changes."""

    def test_get_read_only_post_name_only_and_manager_refused(self) -> None:
        """Preserve project context and reject manager writes and empty names.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Before", description="Retained")
        other = Project.objects.create(name="Other")
        url = reverse("project_name", args=[project.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.name, "Before")
        self.assertEqual(self.client.post(url, {"name": " "}).status_code, 200)
        with patch.dict(os.environ, {"WP_ROLE": "manager"}):
            self.assertEqual(
                self.client.post(url, {"name": "Forged"}).status_code, 404)
        response = self.client.post(
            url, {"name": "After", "description": "forged"})
        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((project.name, project.description, other.name),
                         ("After", "Retained", "Other"))


# --- Dashboard result projections and retained audit data ---
class DashboardTests(TestCase):
    """Verify the results summary without exposing retained audit history."""

    def test_order_results_and_bounded_project_history(self) -> None:
        """Keep all configurations and audits without rendering their history.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Dashboard")
        search = Search.objects.create(project=project, name="Settings")
        old = Search.objects.create(
            project=project, name="Historical settings")
        foreign = Search.objects.create(
            project=Project.objects.create(name="Foreign"), name="Other")
        paper = Paper.objects.create(title="Stored")
        Discovery.objects.create(search=search, paper=paper)
        for index in range(55):
            SearchRun.objects.create(
                search=search if index % 2 else old,
                status="completed", results_count=2,
            )
        SearchRun.objects.filter(search__project=project).update(
            executed_at=timezone.now())
        SearchRun.objects.create(search=foreign, results_count=1000)
        before = list(SearchRun.objects.order_by("pk").values())
        url = reverse("project_detail", args=[project.pk])
        response = self.client.get(url, {"history_page": 2, "depth": 5,
                                         "stage": "title", "prune": "on"})
        html = response.content.decode()
        self.assertLess(html.index('id="search-heading"'),
                        html.index('id="screening-heading"'))
        self.assertLess(html.index('id="screening-heading"'),
                        html.index('id="plots"'))
        self.assertEqual(html.count('id="results-summary"'), 1)
        self.assertNotIn('class="stats-grid"', html)
        self.assertEqual(html.count('>Configure search criteria</a>'), 1)
        self.assertNotIn('data-search-run', html.split('<script>', 1)[0])
        self.assertEqual(response.context["metrics"]["total_identified"], 110)
        self.assertEqual(response.context["metrics"]["unique_total"], 1)
        self.assertNotIn("history_page", response.context)
        self.assertNotIn("searches", response.context)
        self.assertNotContains(response, 'id="search-history"')
        self.assertEqual(response.context["depth"], 5)
        self.assertEqual(response.context["seed_stage"], "title")
        self.assertTrue(response.context["prune"])
        self.assertNotContains(response, "1000 / 0 / 0")
        for page in (1, 3, "bad"):
            result = self.client.get(url, {"history_page": page})
            self.assertNotIn("history_page", result.context)
            self.assertNotContains(result, "Search history")
        self.assertEqual(
            list(SearchRun.objects.order_by("pk").values()), before)

    def test_results_table_exact_historical_final_and_pending_counts(
        self,
    ) -> None:
        """Map exactly four rows to historical hits and reference-gated counts.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Counts")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="Search")
        SearchRun.objects.create(search=search, status="completed",
                                 results_count=9, duplicates_count=6)
        for index in range(3):
            paper = Paper.objects.create(
                title=f"Paper {index}", abstract="Content",
                reference_text="Bibliography" if index == 0 else "",
            )
            Discovery.objects.create(search=search, paper=paper)
            ScreeningDecision.objects.create(
                paper=paper, stage=project.stages.get(name="Title"),
                decision="ACCEPT")
            ScreeningDecision.objects.create(
                paper=paper, stage=project.stages.get(name="Abstract"),
                decision="REJECT" if index == 2 else "ACCEPT")
        response = self.client.get(
            reverse("project_detail", args=[project.pk]))
        table = response.content.decode().split('id="results-summary"', 1)[1]
        table = table.split("</table>", 1)[0]
        self.assertEqual(table.count("<tr>"), 4)
        for label, value in (("Records found", 9), ("Unique papers", 3),
                             ("Final kept", 1), ("Awaiting references", 1)):
            self.assertIn(
                f'<th scope="row">{label}</th><td>{value}</td>', table)
        self.assertNotContains(response, '<summary>More actions</summary>')
        self.assertContains(response, 'id="results-actions"')
        for route in ("refs_backfill", "citations_enrich"):
            self.assertContains(response, f'method="post" action="'
                                f'{reverse(route, args=[project.pk])}"')

    def test_metrics_once_and_curated_renders_on_both_pages(self) -> None:
        """Reuse metrics and show curated graph at the dashboard's end.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.services.metrics_service import build_metrics
        from literature.services.survey_graph_service import build_survey_graph

        project = Project.objects.create(name="Projection")
        with (
            patch(
                "literature.views.build_metrics", wraps=build_metrics,
            ) as metrics,
            patch(
                "literature.services.survey_graph_service.build_survey_graph",
                wraps=build_survey_graph,
            ) as curated,
        ):
            response = self.client.get(
                reverse("project_detail", args=[project.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(metrics.call_count, 1)
            self.assertEqual(curated.call_count, 1)
            html = response.content.decode()
            self.assertLess(html.index('id="chart-citation"'),
                            html.index('id="curated-survey-section"'))
            self.assertIn('id="survey-entry-dialog"', html)
            response = self.client.get(
                reverse("plots_view", args=[project.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(curated.call_count, 2)


# --- Screening navigation is independent of decision chronology ---
class ScreeningNavigationTests(TestCase):
    """Check one card, five destinations and truthful screening states."""

    def test_one_card_and_five_navigation_destinations(self) -> None:
        """Keep the exact link order and retain the curated charts feature.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Stages")
        response = self.client.get(
            reverse("project_detail", args=[project.pk]))
        html = response.content.decode()
        self.assertEqual(html.count('class="card" id="screening-section"'), 1)
        section = html.split('id="screening-section"', 1)[1]
        section = section.split("</section>", 1)[0]
        self.assertNotIn('class="card"', section)
        nav = section.split('<nav class="page-actions"', 1)[1]
        nav = nav.split("</nav>", 1)[0]
        self.assertEqual(nav.count("<a "), 5)
        destinations = (
            ("View Papers", reverse("papers_view", args=[project.pk])),
            ("Reference completion", reverse("reference_review",
                                             args=[project.pk])),
            ("Title round", reverse(
                "screening_view", args=[project.pk, "title"])),
            ("Abstract round", reverse("screening_view",
                                       args=[project.pk, "abstract"])),
            ("Final metrics", "#final-included"),
        )
        positions = []
        for label, href in destinations:
            link = f'href="{href}">{label}</a>'
            self.assertIn(link, nav)
            positions.append(nav.index(link))
            if not href.startswith("#"):
                self.assertEqual(
                    self.client.get(href, follow=True).status_code, 200)
        self.assertEqual(positions, sorted(positions))
        self.assertIn('id="final-included" tabindex="-1"', section)
        self.assertNotIn(reverse("survey_graph", args=[project.pk]), section)
        charts = self.client.get(reverse("plots_view", args=[project.pk]))
        self.assertContains(charts, reverse("survey_graph", args=[project.pk]))
        self.assertContains(response, "No stored papers yet.")

    def test_statuses_content_passed_reference_pending_and_final(self) -> None:
        """Separate content passes from final reference-gated selection.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Screening states")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="Search")
        title_stage = project.stages.get(name="Title")
        abstract_stage = project.stages.get(name="Abstract")
        states = (
            ("PENDING", None, ""),
            ("REJECT", None, ""),
            ("ACCEPT", "PENDING", ""),
            ("ACCEPT", "REJECT", ""),
            ("ACCEPT", "ACCEPT", ""),
            ("ACCEPT", "ACCEPT", "Bibliography"),
            (None, None, ""),
        )
        for index, (title, abstract, bibliography) in enumerate(states):
            paper = Paper.objects.create(title=f"Stored {index}",
                                         reference_text=bibliography)
            Discovery.objects.create(search=search, paper=paper)
            for stage, decision in ((title_stage, title),
                                    (abstract_stage, abstract)):
                if decision:
                    ScreeningDecision.objects.create(
                        paper=paper, stage=stage, decision=decision)
        before = list(ScreeningDecision.objects.order_by("pk").values())
        response = self.client.get(
            reverse("project_detail", args=[project.pk]))
        self.assertContains(response, "Title: 1 pending / 4 Yes / 1 No.")
        self.assertContains(response,
                            "Abstract content: 1 pending / 2 passed / 1 No.")
        self.assertContains(response, "Reference completion: 1 waiting.")
        self.assertContains(
            response, "Final included "
            "(Abstract passed and references available): 1")
        self.assertContains(
            response, "1 stored papers are not queued for Title.")
        self.assertEqual(
            list(ScreeningDecision.objects.order_by("pk").values()), before)

    def test_title_pending_empty_abstract_hint(self) -> None:
        """Explain Title-before-Abstract without requiring references at Title.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Title first")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="Search")
        paper = Paper.objects.create(title="Title alone")
        Discovery.objects.create(search=search, paper=paper)
        ScreeningDecision.objects.create(
            paper=paper, stage=project.stages.get(name="Title"),
            decision="PENDING")
        response = self.client.get(
            reverse("project_detail", args=[project.pk]))
        self.assertContains(
            response, "Review titles before the Abstract round")
        self.assertContains(response, "Reference completion: 0 waiting.")
        self.assertContains(
            response, "References apply only to papers "
            "that passed Abstract content.")


# --- Explicit current configuration and immutable historical provenance ---
class CurrentSearchTests(TestCase):
    """Qualify phase .3 settings and writer boundaries with fictional data."""

    def _post_values(self, preview: dict) -> dict:
        """Convert the displayed proposal to a normal signed form POST.

        Args:
            preview: Captured proposal and configuration token.

        Kwargs:
            None

        Returns:
            dict: Form values retaining the exact supplied confirmation token.
        """
        return {
            **{key: "" if value is None else value
               for key, value in preview["proposal"].items()},
            "configuration_token": preview["configuration_token"],
        }

    def test_constraint_and_zero_configuration_signed_create(self) -> None:
        """Require signed POST and enforce one current row per project.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Current")
        url = reverse("search_create", args=[project.pk])
        page = self.client.get(url)
        self.assertFalse(project.searches.exists())
        data = {
            "name": "Settings", "keywords_json": '["B", "a"]',
            "venues_json": "[]", "author_names_json": "[]",
            "configuration_token": page.context["configuration_token"],
        }
        self.assertEqual(self.client.post(url, data).status_code, 302)
        current = project.searches.get(is_current=True)
        self.assertEqual(current.keywords, ["a", "B"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Search.objects.create(
                project=project, name="Competitor", is_current=True)
        historical = Search.objects.create(project=project, name="Historical")
        self.assertFalse(historical.is_current)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(project.searches.count(), 2)
        self.assertEqual(project.searches.filter(is_current=True).count(), 1)
        dashboard = self.client.get(
            reverse("project_detail", args=[project.pk]))
        html = dashboard.content.decode()
        self.assertEqual(html.count("data-search-run>"), 1)
        self.assertNotIn(
            reverse("search_run", args=[project.pk, historical.pk]), html)
        self.assertNotContains(dashboard, "historical, read-only")
        self.assertContains(dashboard, "Configure search criteria")
        self.assertTrue(Search.objects.filter(pk=historical.pk).exists())

    def test_single_query_only_adoption_changes_only_flag(self) -> None:
        """Promote legacy syntax with a targeted update, not normalization.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Legacy query")
        old = Search.objects.create(
            project=project, name="Old", query='alpha OR "beta gamma"')
        Search.objects.filter(pk=old.pk).update(
            sources_selected=["invalid"], venues=["UnsOrted", "a"])
        before = Search.objects.values().get(pk=old.pk)
        url = reverse("search_create", args=[project.pk])
        page = self.client.get(url)
        self.assertContains(page, 'alpha OR &quot;beta gamma&quot;')
        with patch.object(Search, "save", side_effect=AssertionError(
                "Adoption must not normalize business fields")):
            response = self.client.post(url, {
                "action": "adopt",
                "configuration_token": page.context["configuration_token"],
            })
        self.assertEqual(response.status_code, 302)
        after = Search.objects.values().get(pk=old.pk)
        self.assertTrue(after.pop("is_current"))
        before.pop("is_current")
        self.assertEqual(after, before)

    def test_single_edited_settings_create_new_snapshot(self) -> None:
        """Keep the single legacy row unchanged when saving edited settings.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Edit legacy")
        old = Search.objects.create(
            project=project, name="Old", keywords=["original"])
        before = Search.objects.values().get(pk=old.pk)
        preview = configuration_preview(project, ["", ""])
        response = self.client.post(
            reverse("search_create", args=[project.pk]), {
                **self._post_values(preview), "keywords_json": '["edited"]',
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Search.objects.values().get(pk=old.pk), before)
        self.assertEqual(project.searches.get(is_current=True).keywords,
                         ["edited"])

    def test_combination_preserves_all_business_rows_and_provenance(
        self,
    ) -> None:
        """Combine future filters without rewriting history, FKs or decisions.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Combine")
        project.ensure_default_stages()
        first = Search.objects.create(
            project=project, name="A", keywords=["Alpha"], venues=["Journal"],
            author_names=["Smith"], year_min=2000, year_max=2020,
            sources_selected=["arxiv"])
        second = Search.objects.create(
            project=project, name="B", keywords=["alpha", "Beta"], venues=[],
            author_names=[], year_min=None, year_max=2025,
            sources_selected=["crossref"])
        Search.objects.filter(pk=second.pk).update(
            sources_selected=["invalid"])
        paper = Paper.objects.create(title="Kept history", abstract="Content")
        run = SearchRun.objects.create(
            search=first, results_count=5, status="completed")
        Discovery.objects.create(
            search=first, search_run=run, paper=paper, source="crossref")
        PaperKeywordAssignment.objects.create(
            search=first, project=project, paper=paper, keyword="Alpha")
        ScreeningDecision.objects.create(
            paper=paper, stage=project.stages.get(name="Title"),
            decision="ACCEPT", notes="Keep this historical decision")
        models = (Paper, SearchRun, Discovery, PaperKeywordAssignment,
                  ScreeningDecision)
        before = {model: list(model.objects.order_by("pk").values())
                  for model in models}
        old = list(project.searches.order_by("pk").values())
        preview = configuration_preview(project, ["", ""])
        proposal = preview["proposal"]
        self.assertEqual(proposal["keywords_json"], '["Alpha", "Beta"]')
        self.assertEqual(proposal["venues_json"], "[]")
        self.assertEqual(proposal["author_names_json"], "[]")
        self.assertIsNone(proposal["year_min"])
        self.assertEqual(proposal["year_max"], 2025)
        self.assertEqual(proposal["sources_selected"], list(PROVIDERS))
        self.assertEqual(self.client.post(
            reverse("search_create", args=[project.pk]),
            self._post_values(preview)).status_code, 302)
        self.assertEqual(project.searches.filter(is_current=True).count(), 1)
        self.assertEqual(list(project.searches.filter(
            pk__in=[first.pk, second.pk]).order_by("pk").values()), old)
        for model in models:
            self.assertEqual(list(model.objects.order_by("pk").values()),
                             before[model])

    def test_each_provider_default_and_widest_restricted_union(self) -> None:
        """Normalize each constituent before union and keep unbounded sides.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Union")
        first = Search.objects.create(
            project=project, name="A", keywords=["b"], venues=["J2"],
            author_names=["Z"], year_min=2000, year_max=2020,
            sources_selected=["arxiv"])
        second = Search.objects.create(
            project=project, name="B", keywords=["A", "B"],
            venues=["j2", "J1"],
            author_names=["A"], year_min=1990, year_max=2025,
            sources_selected=["crossref"])
        restricted = configuration_preview(project, ["", ""])["proposal"]
        self.assertEqual(restricted["sources_selected"], ["crossref", "arxiv"])
        self.assertEqual(restricted["venues_json"], '["J1", "J2"]')
        self.assertEqual(restricted["author_names_json"], '["A", "Z"]')
        self.assertEqual((restricted["year_min"], restricted["year_max"]),
                         (1990, 2025))
        for selection in ([], ["invalid"]):
            Search.objects.filter(pk=first.pk).update(
                sources_selected=selection)
            proposal = configuration_preview(project, ["", ""])["proposal"]
            self.assertEqual(proposal["sources_selected"], list(PROVIDERS))
            self.assertEqual(effective_providers(selection), list(PROVIDERS))
        Search.objects.filter(pk=second.pk).update(year_max=None)
        self.assertIsNone(configuration_preview(
            project, ["", ""])["proposal"]["year_max"])

    def test_query_translation_stale_preview_and_archived_urls(self) -> None:
        """Refuse ambiguous/stale confirmations and all archived provider work.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Stale")
        old = Search.objects.create(
            project=project, name="Legacy", query="one OR two")
        Search.objects.create(project=project, name="Rows", keywords=["three"])
        preview = configuration_preview(project, ["", ""])
        data = self._post_values(preview)
        url = reverse("search_create", args=[project.pk])
        self.assertContains(self.client.post(url, data),
                            "Translate or deliberately replace")
        Search.objects.filter(pk=old.pk).update(query="changed syntax")
        self.assertContains(self.client.post(
            url, {**data, "translated": "on"}), "changed after this preview")
        self.assertFalse(project.searches.filter(is_current=True).exists())
        edit = reverse("search_update", args=[project.pk, old.pk])
        self.assertContains(self.client.get(edit),
                            "Historical search settings")
        self.assertEqual(self.client.post(edit, data).status_code, 403)
        with patch(
            "literature.services.search_service._run_provider",
        ) as fetch:
            with self.assertRaises(ValueError):
                run_search(old.pk)
            self.client.post(reverse("search_run", args=[project.pk, old.pk]))
        fetch.assert_not_called()
        self.assertFalse(SearchRun.objects.exists())
        self.assertFalse(project.stages.exists())
        fresh = configuration_preview(project, ["", ""])
        self.assertEqual(self.client.post(url, {
            **self._post_values(fresh), "translated": "on",
        }).status_code, 302)

    def test_bad_expired_foreign_and_worker_bound_confirmation(self) -> None:
        """Reject forged tokens and identities without creating settings.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Owner")
        other = Project.objects.create(name="Foreign")
        url = reverse("search_create", args=[project.pk])
        preview = configuration_preview(project, ["", ""])
        data = {**self._post_values(preview), "keywords_json": '["valid"]'}
        for token in ("", preview["configuration_token"] + "forged",
                      configuration_preview(other, ["", ""])
                      ["configuration_token"],
                      configuration_preview(project, ["uuid", "epoch"])
                      ["configuration_token"]):
            self.assertEqual(self.client.post(url, {
                **data, "configuration_token": token,
            }).status_code, 200)
            self.assertFalse(project.searches.exists())
        with patch("django.core.signing.time.time",
                   return_value=time.time() + 1801):
            self.assertContains(self.client.post(url, data), "preview expired")
        csrf_client = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post(url, data).status_code, 403)
        self.assertFalse(project.searches.exists())

    def test_invalid_future_rows_and_years_do_not_save(self) -> None:
        """Refuse malformed row JSON and reversed year windows at the form.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Invalid settings")
        data = self._post_values(configuration_preview(project, ["", ""]))
        url = reverse("search_create", args=[project.pk])
        for overrides in ({"keywords_json": "not JSON"},
                          {"keywords_json": "[]"},
                          {"keywords_json": '["valid"]', "year_min": 2025,
                           "year_max": 2000}):
            self.assertEqual(self.client.post(url, {
                **data, **overrides,
            }).status_code, 200)
            self.assertFalse(project.searches.exists())

    def test_manual_forgery_and_historical_assignment_preservation(
        self,
    ) -> None:
        """Restrict reassignment to current while retaining saved links.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.forms import LookupPaperForm, ManualPaperForm
        from literature.services.manual_paper_service import (
            add_manual_paper,
            selected_keyword,
        )
        from literature.services.paper_lookup_service import (
            entry_version,
            save_entry,
        )

        project = Project.objects.create(name="Assignments")
        project.ensure_default_stages()
        old = Search.objects.create(
            project=project, name="Old", keywords=["old"])
        current = Search.objects.create(
            project=project, name="Current", keywords=["new"], is_current=True)
        foreign = Search.objects.create(
            project=Project.objects.create(name="Foreign"), name="Other",
            keywords=["foreign"], is_current=True)
        for target, word in ((old, "old"), (foreign, "foreign")):
            with self.assertRaises(ValueError):
                selected_keyword(project, f"{target.pk}|{word}")
            with self.assertRaises(ValueError):
                add_manual_paper(project, target, word, {
                    "origin_source": "crossref", "title": "Forged",
                })
        self.assertFalse(Paper.objects.exists())
        choices = ManualPaperForm(project=project).fields["search_keyword"]
        self.assertEqual(list(choices.choices),
                         [(f"{current.pk}|new", "Current — new")])
        paper = Paper.objects.create(
            title="Stored", doi="10.1000/history", keywords=["old"],
            sources=["crossref"])
        Discovery.objects.create(
            search=old, paper=paper, source="crossref", query="old")
        assignment = PaperKeywordAssignment.objects.create(
            project=project, paper=paper, search=old, keyword="old")
        values = {
            "search_keyword": f"{old.pk}|old", "title_decision": "PENDING",
            "abstract_decision": "NOT_QUEUED",
        }
        form = LookupPaperForm(
            values, project=project, needs_origin=False, paper=paper)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(LookupPaperForm(
            values, project=project, needs_origin=False).is_valid())
        paper_before = Paper.objects.values().get(pk=paper.pk)
        assignment_before = PaperKeywordAssignment.objects.values().get(
            pk=assignment.pk)
        discoveries_before = list(Discovery.objects.values())
        save_entry(project, paper.doi, {}, values, paper.pk,
                   entry_version(project, paper))
        self.assertEqual(Paper.objects.values().get(pk=paper.pk), paper_before)
        self.assertEqual(PaperKeywordAssignment.objects.values().get(
            pk=assignment.pk), assignment_before)
        self.assertEqual(list(Discovery.objects.values()), discoveries_before)
        self.assertFalse(SearchRun.objects.exists())
        for target in (old, foreign):
            with self.assertRaises(ValueError):
                save_entry(project, paper.doi, {}, {
                    **values, "search_keyword": "NEW", "new_keyword": "forged",
                    "new_keyword_search": target.pk,
                }, paper.pk, entry_version(project, paper))
        save_entry(project, paper.doi, {}, {
            **values, "search_keyword": f"{current.pk}|new",
        }, paper.pk, entry_version(project, paper))
        assignment.refresh_from_db()
        self.assertEqual((assignment.search_id, assignment.keyword),
                         (current.pk, "new"))
        self.assertEqual(Discovery.objects.filter(search=old).count(), 1)

    def test_removed_current_keyword_can_keep_unchanged_saved_link(
        self,
    ) -> None:
        """Retain old assignments without restoring removed search words.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.services.paper_lookup_service import (
            entry_version,
            save_entry,
        )

        project = Project.objects.create(name="Removed keyword")
        project.ensure_default_stages()
        search = Search.objects.create(
            project=project, name="Current", is_current=True, keywords=["old"])
        paper = Paper.objects.create(
            title="Stored", doi="10.1000/removed", keywords=["old"],
            sources=["crossref"])
        PaperKeywordAssignment.objects.create(
            project=project, paper=paper, search=search, keyword="old")
        Discovery.objects.create(
            search=search, paper=paper, query="old", source="crossref")
        Search.objects.filter(pk=search.pk).update(
            keywords=["future"], query="future")
        save_entry(project, paper.doi, {}, {
            "search_keyword": f"{search.pk}|old", "title_decision": "PENDING",
            "abstract_decision": "NOT_QUEUED",
        }, paper.pk, entry_version(project, paper))
        search.refresh_from_db()
        paper.refresh_from_db()
        self.assertEqual((search.keywords, paper.keywords),
                         (["future"], ["old"]))
        self.assertFalse(SearchRun.objects.exists())

    def test_stale_edit_retains_new_keyword_and_run_counters(self) -> None:
        """Re-read signed settings atomically and never overwrite newer audits.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.services.paper_lookup_service import save_entry

        project = Project.objects.create(name="Stale editor")
        project.ensure_default_stages()
        search = Search.objects.create(
            project=project, name="Current", is_current=True, keywords=["old"])
        url = reverse("search_update", args=[project.pk, search.pk])
        token = self.client.get(url).context["configuration_token"]
        save_entry(project, "10.1000/added", {"title": "New paper"}, {
            "search_keyword": "NEW", "new_keyword": "added",
            "new_keyword_search": search.pk, "origin_source": "crossref",
            "title_decision": "PENDING", "abstract_decision": "NOT_QUEUED",
        }, None)
        data = {
            "name": "Current", "keywords_json": '["old"]',
            "venues_json": "[]", "author_names_json": "[]",
            "configuration_token": token,
        }
        self.assertContains(self.client.post(url, data),
                            "changed after this preview")
        search.refresh_from_db()
        self.assertEqual(search.keywords, ["added", "old"])
        fresh = self.client.get(url).context["configuration_token"]
        Search.objects.filter(pk=search.pk).update(
            results_count=123, new_papers_count=9)
        self.assertEqual(self.client.post(url, {
            **data, "keywords_json": '["added", "old"]',
            "configuration_token": fresh,
        }).status_code, 302)
        search.refresh_from_db()
        self.assertEqual((search.results_count, search.new_papers_count),
                         (123, 9))

    def test_edit_get_race_renders_its_signed_snapshot(self) -> None:
        """Render the same business snapshot that confirmation fingerprints.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="GET snapshot race")
        search = Search.objects.create(
            project=project, name="Current", is_current=True, keywords=["old"])
        url = reverse("search_update", args=[project.pk, search.pk])

        def add_before_preview(owner: Project, binding: list[str]) -> dict:
            """Commit new settings after the editor's first configuration read.

            Args:
                owner: Synthetic project whose preview is being rendered.
                binding: Identity for the actual signed preview.

            Kwargs:
                None

            Returns:
                dict: Preview of the freshly committed business fields.
            """
            Search.objects.filter(pk=search.pk).update(
                keywords=["added", "old"], query="added old")
            return configuration_preview(owner, binding)

        with patch("literature.views.configuration_preview",
                   side_effect=add_before_preview):
            response = self.client.get(url)
        initial = response.context["form"].initial
        self.assertEqual(initial["keywords_json"], '["added", "old"]')
        data = {key: "" if value is None else value
                for key, value in initial.items()}
        self.assertEqual(self.client.post(url, {
            **data, "configuration_token": response.context[
                "configuration_token"],
        }).status_code, 302)
        search.refresh_from_db()
        self.assertEqual(search.keywords, ["added", "old"])

    def test_repeat_run_adds_audits_and_failure_stays_isolated(self) -> None:
        """Keep one editable row while recording each provider attempt.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Repeat")
        search = Search.objects.create(
            project=project, name="Current", is_current=True,
            query='alpha OR "beta gamma"',
            sources_selected=["openalex", "arxiv"])
        with (
            patch("literature.services.search_service.time.sleep"),
            patch("literature.services.search_service.OpenAlexSource.fetch",
                  side_effect=ValueError("Synthetic provider outage")),
            patch("literature.services.search_service.ArxivSource.fetch",
                  return_value=([], 0)) as successful,
        ):
            for _ in range(2):
                runs = run_search(search.pk, max_pages=1)
                self.assertEqual([row.status for row in runs],
                                 ["failed", "completed"])
        self.assertEqual(project.searches.count(), 1)
        self.assertEqual(SearchRun.objects.count(), 4)
        self.assertEqual(successful.call_count, 2)
        self.assertEqual(successful.call_args.args[0].query,
                         'alpha OR "beta gamma"')
        other = Project.objects.create(name="Foreign")
        with patch("literature.views.run_search") as provider:
            for route in ("search_update", "search_run"):
                self.assertEqual(self.client.post(reverse(
                    route, args=[other.pk, search.pk])).status_code, 404)
        provider.assert_not_called()

    def test_integrity_winner_is_reread_outside_failed_atomic(self) -> None:
        """Recover a losing confirmation from the committed current row.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Race recovery")
        preview = configuration_preview(project, ["", ""])

        def competitor(*args, **kwargs) -> None:
            """Simulate another committed writer and a rolled-back loser.

            Args:
                *args: Confirmation service arguments unused by this fault.

            Kwargs:
                **kwargs: Confirmation options unused by this fault.

            Returns:
                None

            Raises:
                IntegrityError: Always, after the loser atomic has exited.
            """
            Search.objects.create(
                project=project, name="Winner", is_current=True)
            with transaction.atomic():
                Search.objects.create(
                    project=project, name="Loser", is_current=True)

        with patch("literature.views.save_current_configuration",
                   side_effect=competitor):
            response = self.client.post(
                reverse("search_create", args=[project.pk]), {
                    **self._post_values(preview),
                    "keywords_json": '["synthetic"]',
                })
        winner = project.searches.get(is_current=True)
        self.assertRedirects(response, reverse(
            "search_update", args=[project.pk, winner.pk]))
        self.assertEqual(project.searches.count(), 1)

    def test_write_lock_is_retryable_without_automatic_retry(self) -> None:
        """Return a local GET retry link and leave configuration unmodified.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Locked")
        preview = configuration_preview(project, ["", ""])
        with patch("literature.views.save_current_configuration",
                   side_effect=OperationalError("database is locked")) as save:
            response = self.client.post(
                reverse("search_create", args=[project.pk]), {
                    **self._post_values(preview),
                    "keywords_json": '["synthetic"]',
                })
        self.assertEqual(response.status_code, 503)
        save.assert_called_once()
        self.assertContains(
            response, reverse("search_create", args=[project.pk]),
            status_code=503)
        self.assertFalse(project.searches.exists())
        with patch("literature.views.get_object_or_404",
                   side_effect=OperationalError("database is locked")):
            locked_run = self.client.post(reverse(
                "search_run", args=[project.pk, 1]))
        self.assertEqual(locked_run.status_code, 503)
        retry = reverse("search_create", args=[project.pk])
        self.assertContains(locked_run, f'href="{retry}"', status_code=503)
        with patch(
            "literature.services.search_service._run_provider",
        ) as fetch:
            self.assertEqual(self.client.get(retry).status_code, 200)
        fetch.assert_not_called()


class CurrentSearchConcurrencyTests(TransactionTestCase):
    """Check competing SQLite writers without pretending row locks exist."""

    def test_competing_create_and_adopt_leave_one_winner(self) -> None:
        """Allow explicit retry after contention, with no partial settings.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for adopt in (False, True):
            project = Project.objects.create(name=f"Race {adopt}")
            old = (Search.objects.create(
                project=project, name="Legacy", query="one OR two")
                if adopt else None)
            before = Search.objects.values().get(pk=old.pk) if old else None
            preview = configuration_preview(project, ["", ""])
            values = {
                "name": "Future", "keywords": ["synthetic"], "venues": [],
                "author_names": [], "sources_selected": ["arxiv"],
                "year_min": None, "year_max": None,
            }
            barrier = Barrier(2)

            def save(
                owner: Project, settings: dict, token: str, gate: Barrier,
                adoption: bool,
            ) -> bool:
                """Compete from a distinct connection using the same preview.

                Args:
                    owner: Project for this race iteration.
                    settings: Validated future configuration.
                    token: Exact shared preview token.
                    gate: Barrier coordinating independent connections.
                    adoption: Promote one legacy flag rather than insert.

                Kwargs:
                    None

                Returns:
                    bool: Whether this confirmation committed.
                """
                close_old_connections()
                gate.wait(timeout=10)
                try:
                    save_current_configuration(
                        owner, settings, token, ["", ""], adopt=adoption)
                    return True
                except (IntegrityError, OperationalError, ValueError):
                    return False
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(
                    save, project, values, preview["configuration_token"],
                    barrier, adopt) for _ in range(2)]
                results = [future.result(timeout=20) for future in futures]
            self.assertLessEqual(sum(results), 1)
            self.assertEqual(project.searches.count(),
                             1 if adopt else sum(results))
            if not any(results):
                # Deliberate user retry, never a production autoretry loop.
                save_current_configuration(
                    project, values, preview["configuration_token"],
                    ["", ""], adopt=adopt)
            self.assertEqual(
                project.searches.filter(is_current=True).count(), 1)
            if old:
                after = Search.objects.values().get(pk=old.pk)
                self.assertTrue(after.pop("is_current"))
                before.pop("is_current")
                self.assertEqual(after, before)


class CurrentSearchSchemaTests(SimpleTestCase):
    """Prove schema-only upgrades and initial-read locks in scratch DBs."""

    def test_worker_client_keeps_explicit_tokens_and_uses_native_action(
        self,
    ) -> None:
        """Keep forged/stale tests exact while normal forms send saved tokens.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from urllib.parse import parse_qs

        from literature.tests.test_wp0105_lifecycle import _Client

        client = _Client()
        url = "http://127.0.0.1:1/project/1/search/new/"
        action = "http://127.0.0.1:1/project/1/search/7/edit/"
        client.configuration_tokens[url] = "saved-preview"
        client.configuration_actions[url] = action
        for explicit in ("known-stale", "forged", ""):
            with patch.object(client.opener, "open") as opening:
                client.post(url, {"configuration_token": explicit})
            opening.assert_called_once()
            request = opening.call_args.args[0]
            self.assertEqual(request.full_url, url)
            self.assertEqual(parse_qs(request.data.decode(),
                                      keep_blank_values=True),
                             {"configuration_token": [explicit]})
        with patch.object(client.opener, "open") as opening:
            client.post(url, {"name": "normal"})
        opening.assert_called_once()
        request = opening.call_args.args[0]
        self.assertEqual(request.full_url, action)
        self.assertEqual(parse_qs(request.data.decode()),
                         {"configuration_token": ["saved-preview"],
                          "name": ["normal"]})

    def test_admin_cannot_bypass_search_confirmation_or_repoint_audits(
        self,
    ) -> None:
        """Keep affected admin entries browse-only, including bulk deletion.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from django.contrib import admin
        from django.test import RequestFactory

        from literature.admin import ReadOnlyAuditAdmin

        request = RequestFactory().get("/admin/")
        for model in (Search, SearchRun, Discovery):
            registered = admin.site._registry[model]
            self.assertIsInstance(registered, ReadOnlyAuditAdmin)
            self.assertFalse(registered.has_add_permission(request))
            self.assertFalse(registered.has_change_permission(request))
            self.assertFalse(registered.has_delete_permission(request))

    def test_synthetic_0012_upgrade_preserves_all_rows(self) -> None:
        """Explicitly migrate synthetic old schema without changing its rows.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        script = '''
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
executor = MigrationExecutor(connection)
executor.migrate([("literature", "0012_surveyentry_inclusion_origin")])
apps = executor.loader.project_state(
    [("literature", "0012_surveyentry_inclusion_origin")]).apps
Project = apps.get_model("literature", "Project")
Search = apps.get_model("literature", "Search")
owner = Project.objects.create(name="Synthetic old schema")
Search.objects.create(project=owner, name="Legacy", query="a OR b",
                      sources_selected=["invalid"], keywords=[])
legacy = Search.objects.get(project=owner)
Search.objects.create(project=owner, name="Second", keywords=["B", "a"],
                      venues=["UnsOrted", "a"], year_min=None, year_max=2025)
Paper = apps.get_model("literature", "Paper")
Run = apps.get_model("literature", "SearchRun")
Discovery = apps.get_model("literature", "Discovery")
Assignment = apps.get_model("literature", "PaperKeywordAssignment")
Stage = apps.get_model("literature", "Stage")
Decision = apps.get_model("literature", "ScreeningDecision")
paper = Paper.objects.create(title="Synthetic old paper", doi="10.1000/schema",
                             abstract="Content", reference_text="Bibliography")
run = Run.objects.create(search=legacy, query_used="a OR b",
                         status="completed", results_count=7,
                         duplicates_count=3)
Discovery.objects.create(search=legacy, search_run=run, paper=paper,
                         query="a OR b", source="manual",
                         raw_payload={"retained": "historical audit"})
Assignment.objects.create(project=owner, search=legacy, paper=paper,
                          keyword="a")
title = Stage.objects.create(project=owner, name="Title", order=1)
abstract = Stage.objects.create(project=owner, name="Abstract", order=2,
                                is_final=True)
Decision.objects.create(paper=paper, stage=title, decision="ACCEPT")
Decision.objects.create(paper=paper, stage=abstract, decision="REJECT")
tables = connection.introspection.table_names()
before = {}
with connection.cursor() as cursor:
    for table in tables:
        if table != "django_migrations":
            cursor.execute('SELECT * FROM "' + table + '" ORDER BY 1')
            before[table] = cursor.fetchall()
MigrationExecutor(connection).migrate(
    [("literature", "0013_search_is_current")])
with connection.cursor() as cursor:
    for table, rows in before.items():
        cursor.execute('SELECT * FROM "' + table + '" ORDER BY 1')
        after = cursor.fetchall()
        if table == "literature_search":
            names = [field[0] for field in cursor.description]
            position = names.index("is_current")
            assert all(row[position] == 0 for row in after)
            after = [row[:position] + row[position + 1:] for row in after]
        assert after == rows, table
    cursor.execute("PRAGMA integrity_check")
    assert cursor.fetchall() == [("ok",)]
    cursor.execute("PRAGMA foreign_key_check")
    assert cursor.fetchall() == []
print("schema-only 0012 to 0013 PASS")
'''
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ,
                       LOCAL_SURVEY_DB=str(Path(tmp) / "old.sqlite3"),
                       WP_ROLE="developer", PYTHON_DOTENV_DISABLED="1",
                       DJANGO_SETTINGS_MODULE="config.settings")
            result = subprocess.run(
                [sys.executable, "-c", script], env=env, check=True,
                capture_output=True, text=True, timeout=30)
            self.assertIn("schema-only 0012 to 0013 PASS", result.stdout)

    def test_initial_adoption_read_lock_has_no_partial_mutation(self) -> None:
        """Return 503 under a real exclusive synthetic SQLite file lock.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory() as tmp:
            source = create_project_store(Path(tmp) / "locked", "Lock")
            db = source.location / "project.sqlite3"
            env = dict(os.environ, LOCAL_SURVEY_DB=str(db),
                       WP_ROLE="developer",
                       PYTHON_DOTENV_DISABLED="1",
                       DJANGO_SETTINGS_MODULE="config.settings")
            prepare = (
                "import django; django.setup(); "
                "from literature.models import Search, Project; "
                "Search.objects.create(project=Project.objects.get(), "
                "name='Legacy', query='old OR syntax')")
            subprocess.run([sys.executable, "-c", prepare], env=env,
                           check=True, capture_output=True, timeout=30)
            probe = (
                "import django; django.setup(); "
                "from django.test import Client; "
                "print(Client().post('/project/1/search/new/', "
                "{'action':'adopt','configuration_token':'invalid'})"
                ".status_code)")
            with sqlite3.connect(db, isolation_level=None) as lock:
                lock.execute("BEGIN EXCLUSIVE")
                try:
                    result = subprocess.run(
                        [sys.executable, "-c", probe], env=env, check=True,
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.stdout.strip(),
                                     "503", result.stderr)
                finally:
                    lock.rollback()
                self.assertEqual(lock.execute(
                    "SELECT is_current, query FROM literature_search"
                ).fetchall(), [(0, "old OR syntax")])
