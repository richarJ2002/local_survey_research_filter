#!/usr/bin/env python3

'''
@File:   test_views.py

@Brief:  Smoke tests for screening queue and markdown export.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Custom library Imports
from django.test import TestCase
from django.urls import reverse

from literature.models import Paper, Project, ScreeningDecision, Search

# ------------------------------------------------------------------------ #
# View tests
# ------------------------------------------------------------------------ #


class ViewTests(TestCase):
    """
    Title round shows metadata, abstract round shows abstract.
    """

    def test_new_project_help_appears_below_each_field(self) -> None:
        """
        Field examples and guidance are separate from their inputs.
        """
        response = self.client.get(reverse("project_create"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertRegex(
            html,
            r'<input[^>]*id="id_domain"[^>]*>\s*<small class="form-help" '
            r'id="id_domain_helptext">e\.g\. robotics, climate, medicine\.</small>',
        )
        self.assertRegex(
            html,
            r'</textarea>\s*<small class="form-help" '
            r'id="id_research_questions_helptext">One per line\.',
        )
        invalid = self.client.post(reverse("project_create"), {
            "name": "", "domain": "robotics",
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, 'value="robotics"')
        self.assertContains(invalid, 'id="id_name_error"')

    def test_project_context_is_at_top_of_detail_page(self) -> None:
        """
        Keep project context before the simplified Search controls.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(
            name="Navigation survey", description="A focused review",
            survey_topic="Semantic navigation", domain="robotics",
            research_questions="RQ1: Mapping?\nRQ2: Planning?",
        )
        response = self.client.get(
            reverse("project_detail", args=[project.pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertContains(response, 'class="card project-summary"')
        self.assertContains(response, "<h1>Navigation survey</h1>")
        self.assertContains(
            response, "<dt>Survey topic</dt><dd>Semantic navigation</dd>")
        self.assertContains(response, "<dt>Domain focus</dt><dd>robotics</dd>")
        self.assertContains(response, "RQ1: Mapping?<br>RQ2: Planning?")
        self.assertLess(html.index("Research questions"),
                        html.index('id="search-heading"'))

    def test_search_form_has_accessible_loading_state(self) -> None:
        """
        Keep loading feedback and duplicate-submit guards on current runs.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Search feedback")
        Search.objects.create(
            project=project, name="First search", keywords=["navigation"],
            is_current=True)
        response = self.client.get(
            reverse("project_detail", args=[project.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-search-run')
        self.assertContains(
            response, 'class="meta search-status" role="status" hidden')
        self.assertContains(response, 'Searching selected sources.')
        self.assertContains(response, 'form.dataset.submitting === "true"')
        self.assertContains(response, 'button.disabled = true')
        self.assertContains(response, 'prefers-reduced-motion: reduce')

    def test_launch_entry_help_and_port_hint(self) -> None:
        """
        Keep top-level startup help distinct from Django subcommand help.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        root = Path(__file__).resolve().parents[2]
        help_result = subprocess.run(
            [sys.executable, "launch.py", "-h"], cwd=root,
            capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("python3.12 launch.py --port 8000", help_result.stdout)

        port_result = subprocess.run(
            [sys.executable, "launch.py", "8000"], cwd=root,
            capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(port_result.returncode, 2)
        self.assertIn("invalid choice", port_result.stderr)
        self.assertIn("--port", port_result.stderr)

        django_result = subprocess.run(
            [sys.executable, "launch.py", "django", "help", "runserver"],
            cwd=root,
            capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(django_result.returncode, 0)
        self.assertIn("runserver", django_result.stdout)

    def test_project_trash_link_requires_confirmation(self) -> None:
        """
        A card's trash icon opens confirmation; only POST deletes it.
        """
        target = Project.objects.create(name="Review Alpha")
        other = Project.objects.create(name="Review Beta")
        delete_url = reverse("project_delete", args=[target.pk])
        listing = self.client.get(reverse("project_list"))
        self.assertContains(listing, f'href="{delete_url}"')
        self.assertContains(listing, 'aria-label="Delete Review Alpha"')
        self.assertContains(listing, 'viewBox="0 0 24 24"')

        confirmation = self.client.get(delete_url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "Delete project?")
        self.assertContains(confirmation, "Yes, delete")
        self.assertTrue(Project.objects.filter(pk=target.pk).exists())

        response = self.client.post(delete_url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Project.objects.filter(pk=target.pk).exists())
        self.assertTrue(Project.objects.filter(pk=other.pk).exists())

    def test_shared_layout_and_workflow_controls_render(self) -> None:
        """
        Retain shared navigation, results summary and form controls.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Design check")
        pages = (
            reverse("project_list"),
            reverse("project_create"),
            reverse("project_detail", args=[project.pk]),
            reverse("search_create", args=[project.pk]),
            reverse("plots_view", args=[project.pk]),
        )
        for url in pages:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'href="#main-content"')
            self.assertContains(response, 'aria-label="Main navigation"')
            self.assertContains(response, '@media (max-width: 650px)')
            self.assertContains(
                response,
                '.site-header__inner, .site-main, .messages '
                '{ width: max(80%, min(92%, 1100px)); }',
            )
            self.assertNotContains(response, 'max-width: 1100px')
            self.assertContains(response, "Local Survey Research Filter")
            self.assertContains(
                response, "Aerospace Engineering Research &amp; Operation Systems (AERO-Sys) group")
            self.assertContains(response, 'id="theme-toggle"')
            self.assertContains(response, 'localStorage.setItem("lsrf-theme"')

        self.assertContains(self.client.get(pages[0]), 'class="project-grid"')
        self.assertContains(self.client.get(pages[2]), 'id="results-summary"')
        self.assertNotContains(self.client.get(pages[2]), 'class="stats-grid"')
        search = self.client.get(pages[3])
        self.assertContains(search, 'id="search-form"')
        self.assertContains(search, 'name="keywords_json"')

    def test_opencitations_setting_never_displays_saved_token(self) -> None:
        """
        The optional credential stays in .env and is not echoed in HTML.
        """
        from unittest.mock import patch

        from django.test import override_settings

        with override_settings(OPENCITATIONS_TOKEN="stored-secret"):
            response = self.client.get(reverse("settings_view"))
        self.assertContains(response, 'name="opencitations_token"')
        self.assertContains(response, "OpenCitations token configured")
        self.assertNotContains(response, "stored-secret")
        with patch("literature.views.set_key") as save:
            response = self.client.post(reverse("settings_view"), {
                "openalex_email": "research@example.org",
                "opencitations_token": "new-token",
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any(call.args[1:] == ("OPENCITATIONS_TOKEN", "new-token")
                            for call in save.call_args_list))

    def test_elsevier_settings_hide_secrets_and_preserve_blanks(self) -> None:
        """
        Save only submitted Elsevier credentials without echoing secrets.

        Args:
            None

        Kwargs:
            None

        Returns:
            None

        Side Effects:
            Use a temporary credential path and mock all credential writes.
        """
        import tempfile
        from unittest.mock import call, patch

        from django.test import override_settings

        with tempfile.TemporaryDirectory(
            prefix="scopus-settings-", dir="/tmp/opencode",
        ) as tmp:
            credentials = Path(tmp) / "credentials.env"
            with (
                override_settings(
                    LOCAL_SURVEY_CREDENTIALS_FILE=credentials,
                    ELSEVIER_API_KEY="stored-scopus-key",
                    ELSEVIER_INSTTOKEN="stored-scopus-token",
                ),
                patch("literature.views.set_key") as save,
            ):
                url = reverse("settings_view")
                response = self.client.get(url)
                save.assert_not_called()
                for field, secret in (
                    ("elsevier_api_key", "stored-scopus-key"),
                    ("elsevier_insttoken", "stored-scopus-token"),
                ):
                    self.assertContains(response, f'name="{field}"')
                    self.assertNotContains(response, secret)
                    self.assertNotIn(field, response.context["form"].initial)
                    widget = response.context["form"].fields[field].widget
                    self.assertFalse(widget.render_value)
                    self.assertEqual(widget.attrs["placeholder"],
                                     "Configured (hidden)")
                self.assertTrue(
                    response.context["elsevier_api_key_configured"])
                self.assertTrue(
                    response.context["elsevier_insttoken_configured"])
                for link in (
                    "https://dev.elsevier.com/apikey/manage",
                    "https://dev.elsevier.com/support.html",
                    "https://dev.elsevier.com/policy.html",
                ):
                    self.assertContains(response, f'href="{link}"')
                self.assertContains(
                    response, 'rel="noopener noreferrer"', count=3)
                self.assertContains(
                    response, "you must confirm that your")
                self.assertContains(
                    response, "alone does not grant Scopus API")
                self.assertContains(
                    response, "Blank secret fields do not mean a saved key")

                response = self.client.post(url, {
                    "openalex_email": "research@example.org",
                    "elsevier_api_key": "new-scopus-key",
                    "elsevier_insttoken": "new-scopus-token",
                    "ELSEVIER_UNRECOGNISED": "not-allowlisted",
                })
                self.assertEqual(response.status_code, 302)
                self.assertEqual(save.call_args_list, [
                    call(str(credentials), "OPENALEX_EMAIL",
                         "research@example.org"),
                    call(str(credentials), "ELSEVIER_API_KEY",
                         "new-scopus-key"),
                    call(str(credentials), "ELSEVIER_INSTTOKEN",
                         "new-scopus-token"),
                ])
                messages = " ".join(
                    str(message) for message in response.wsgi_request._messages)
                self.assertIn("restart", messages)
                self.assertIn("Elsevier Scopus API key saved", messages)
                self.assertIn("does not delete the key", messages)
                for secret in ("new-scopus-key", "new-scopus-token"):
                    self.assertNotIn(secret, messages)
                    self.assertNotIn(secret, response["Location"])

                for optional in (
                    {"elsevier_api_key": "", "elsevier_insttoken": ""}, {},
                ):
                    with self.subTest(optional=optional):
                        save.reset_mock()
                        response = self.client.post(url, {
                            "openalex_email": "research@example.org",
                            **optional,
                        })
                        self.assertEqual(response.status_code, 302)
                        save.assert_called_once_with(
                            str(credentials), "OPENALEX_EMAIL",
                            "research@example.org")

    def test_elsevier_settings_reject_unsafe_input_without_echoing(self) -> None:
        """
        Reject unsafe header values and hide failed POST credentials.

        Args:
            None

        Kwargs:
            None

        Returns:
            None

        Side Effects:
            Use a temporary credential path and mock all credential writes.
        """
        import tempfile
        from unittest.mock import patch

        from django.test import override_settings

        from literature.forms import SettingsForm

        printable_ascii = "".join(chr(code) for code in range(33, 127))
        for value in (printable_ascii, "x" * 4096):
            form = SettingsForm({
                "openalex_email": "research@example.org",
                "elsevier_api_key": value, "elsevier_insttoken": value,
            })
            self.assertTrue(form.is_valid(), form.errors)
        with (
            tempfile.TemporaryDirectory(
                prefix="scopus-settings-", dir="/tmp/opencode",
            ) as tmp,
            override_settings(
                LOCAL_SURVEY_CREDENTIALS_FILE=Path(tmp) / "credentials.env"),
            patch("literature.views.set_key") as save,
        ):
            url = reverse("settings_view")
            for field in ("elsevier_api_key", "elsevier_insttoken"):
                for value in (
                    "unsafe key", "unsafe\tkey", "unsafe\nkey", "unsafe\rkey",
                    "unsafe\0key", "unsafe\x1fkey", "unsafe\x7fkey",
                    "unsafe\u00e9key", "unsafe-key\n", "x" * 4097,
                ):
                    with self.subTest(field=field, value=value):
                        data = {
                            "openalex_email": "research@example.org",
                            "elsevier_api_key": "new-scopus-key",
                            "elsevier_insttoken": "new-scopus-token",
                        }
                        data[field] = value
                        response = self.client.post(url, data)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(field, response.context["form"].errors)
                        save.assert_not_called()
                        self.assertNotContains(response, "new-scopus-key")
                        self.assertNotContains(response, "new-scopus-token")
                        self.assertNotRegex(
                            response.content.decode(),
                            r'<input[^>]*name="elsevier_(?:api_key|insttoken)"'
                            r'[^>]*\bvalue=',
                        )
            save.side_effect = PermissionError("synthetic save denied")
            with override_settings(ELSEVIER_API_KEY="stored-scopus-key",
                                   ELSEVIER_INSTTOKEN="stored-scopus-token"):
                response = self.client.post(url, {
                    "openalex_email": "research@example.org",
                    "elsevier_api_key": "new-scopus-key",
                    "elsevier_insttoken": "new-scopus-token",
                })
            self.assertContains(response, "Cannot save the credential file")
            self.assertNotContains(response, "new-scopus-key")
            self.assertNotContains(response, "new-scopus-token")
            self.assertNotContains(response, "stored-scopus-key")
            self.assertNotContains(response, "stored-scopus-token")
            self.assertContains(response, "Elsevier Scopus API key configured")
            self.assertContains(
                response, "Elsevier institutional token configured")
            self.assertContains(response, "Configured (hidden)", count=2)
            self.assertNotContains(
                response, "No Elsevier Scopus API key is loaded")

    # --- Title round metadata ---
    def test_screening_title_reserves_four_lines_without_clipping(self) -> None:
        """
        Both screening rounds reserve title space without imposing a height limit.
        """
        import re

        project = Project.objects.create(name="Stable screening layout")
        project.ensure_default_stages()
        for stage_name in ("Title", "Abstract"):
            stage = project.stages.get(name=stage_name)
            paper = Paper.objects.create(
                title="Short title", abstract="Summary")
            ScreeningDecision.objects.create(paper=paper, stage=stage)
            url = reverse("screening_view", args=[
                          project.pk, stage_name.lower()])
            for title in ("Short title", "A long descriptive paper title " * 40):
                with self.subTest(stage=stage_name, title_length=len(title)):
                    paper.title = title
                    paper.save(update_fields=["title"])
                    response = self.client.get(url)
                    self.assertContains(
                        response, f'<h1 class="paper-title">{title}</h1>', html=True)
                    rule = re.search(
                        r"#screening-card \.paper-title\s*\{([^}]+)\}", response.content.decode())
                    self.assertIsNotNone(rule)
                    css = rule.group(1)
                    self.assertIn("min-height: 5.4em;", css)
                    self.assertIn("min-height: 4lh;", css)
                    self.assertNotRegex(
                        css, r"(?:^|;)\s*(?:height|max-height|overflow|text-overflow|line-clamp)\s*:")

    def test_title_round_shows_metadata_not_abstract(self) -> None:
        """
        Round 1 displays venue/date but hides abstract text.
        """
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        title = project.stages.get(name="Title")
        paper = Paper.objects.create(
            title="Big Title",
            abstract="Secret abstract words",
            venue="Nature",
            publication_year=2025,
            authors=[{"name": "A B"}],
        )
        ScreeningDecision.objects.create(paper=paper, stage=title)
        url = reverse("screening_view", args=[project.pk, "title"])
        resp = self.client.get(url)
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Big Title" in content
        assert "Nature" in content
        assert "Secret abstract words" not in content

    def test_screening_metadata_has_stable_slots_for_missing_and_long_values(self) -> None:
        """
        Keep all rows in both rounds and retain long text without clamping.
        """
        import re

        project = Project.objects.create(name="Stable metadata layout")
        project.ensure_default_stages()
        for stage_name in ("Title", "Abstract"):
            paper = Paper.objects.create(
                title="Title is the focus", abstract="Summary")
            ScreeningDecision.objects.create(
                paper=paper, stage=project.stages.get(name=stage_name))
            url = reverse("screening_view", args=[
                          project.pk, stage_name.lower()])
            missing = self.client.get(url)
            self.assertContains(
                missing, '<dt>DOI</dt><dd class="metadata-medium">Not supplied</dd>', html=True)
            self.assertContains(
                missing, '<dt>Cited by</dt><dd>0</dd>', html=True)
            self.assertContains(
                missing, '<dt>Keywords</dt><dd class="metadata-long">Not supplied</dd>', html=True)
            paper.authors = [{"name": "A very long author name " * 12}]
            paper.venue = "A long journal and venue name " * 12
            paper.doi = "10.1000/" + stage_name.lower() + "-" + "long-identifier" * 12
            paper.keywords = [
                "Long descriptive keyword " * 10, "Another keyword"]
            paper.save(update_fields=["authors", "venue", "doi", "keywords"])
            populated = self.client.get(url)
            self.assertContains(populated, paper.author_display())
            self.assertContains(populated, paper.venue)
            self.assertContains(populated, paper.doi)
            self.assertContains(populated, ", ".join(paper.keywords))
            for response in (missing, populated):
                with self.subTest(stage=stage_name, populated=response is populated):
                    html = response.content.decode()
                    metadata = re.search(
                        r'<dl class="meta screening-metadata">(.*?)</dl>', html, re.DOTALL).group(1)
                    self.assertEqual(re.findall(r"<dt>(.*?)</dt>", metadata), [
                                     "Authors", "Published", "Journal / venue", "DOI", "Cited by", "Keywords"])
                    for selector, lines in (("dd", 1), ("dd.metadata-medium", 2), ("dd.metadata-long", 4)):
                        rule = re.search(r"#screening-card \.screening-metadata " +
                                         re.escape(selector) + r"\s*\{([^}]+)\}", html).group(1)
                        self.assertIn(f"min-height: {lines}lh;", rule)
                        self.assertNotRegex(
                            rule, r"(?:^|;)\s*(?:height|max-height|overflow|text-overflow|line-clamp)\s*:")

    # --- Abstract round ---
    def test_abstract_round_shows_abstract(self) -> None:
        """
        Round 2 displays abstract.
        """
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        abstract = project.stages.get(name="Abstract")
        paper = Paper.objects.create(
            title="T", abstract="Visible summary here")
        ScreeningDecision.objects.create(paper=paper, stage=abstract)
        url = reverse("screening_view", args=[project.pk, "abstract"])
        resp = self.client.get(url)
        assert resp.status_code == 200
        assert "Visible summary here" in resp.content.decode()
