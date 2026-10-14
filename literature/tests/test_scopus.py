"""
Qualify optional Scopus search with synthetic responses and project data.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File: test_scopus.py
@Brief: Query/header boundaries, pagination, failures and opt-in integration.
@Date: 13/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import call, patch

# Custom library Imports
import httpx
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from literature.forms import SearchForm, SettingsForm
from literature.models import Discovery, Paper, Project, Search, SearchRun
from literature.providers import DEFAULT_SEARCH_PROVIDERS
from literature.providers.mapping import map_scopus_to_canonical
from literature.providers.scopus import (
    ProviderResult,
    ScopusConfig,
    ScopusError,
    ScopusSource,
)
from literature.services.manual_paper_service import selected_origin
from literature.services.search_service import (
    configuration_preview,
    effective_providers,
    run_search,
)

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Synthetic Elsevier STANDARD records ---


def entry(number: int = 1) -> dict:
    """
    Build a bibliographic search record with no real research data.

    Args:
        None

    Kwargs:
        number: Synthetic identity suffix. Defaults to 1.

    Returns:
        dict: Scopus STANDARD-shaped metadata with a public record link.
    """
    return {
        "eid": f"2-s2.0-850000{number}",
        "dc:identifier": f"SCOPUS_ID:850000{number}",
        "dc:title": f"Synthetic navigation {number}",
        "dc:creator": "Doe, Jane",
        "prism:publicationName": "Synthetic journal",
        "prism:coverDate": "2024-04-03",
        "prism:doi": f"https://doi.org/10.1234/SYNTHETIC-{number}",
        "citedby-count": "7",
        "link": [{"@ref": "scopus", "@href":
                  f"https://www.scopus.com/record/display.uri?eid=2-s2.0-850000{number}"}],
    }


def page(entries: list, total: int) -> httpx.Response:
    """
    Build a synthetic JSON search page.

    Args:
        entries: Search entries, including an empty-result sentinel if needed.
        total: Provider's reported total before truncation.

    Kwargs:
        None

    Returns:
        httpx.Response: Successful JSON response without any network request.
    """
    return httpx.Response(200, json={"search-results": {
        "opensearch:totalResults": str(total), "entry": entries}})


# --- Classes
@override_settings(ELSEVIER_API_KEY="", ELSEVIER_INSTTOKEN="")
class ScopusProviderTests(SimpleTestCase):
    """
    Exercise the actual adapter through httpx's in-memory transport.
    """

    def client_for(self, replies: list) -> httpx.Client:
        """
        Create a borrowed mock client and record its requests.

        Args:
            replies: Responses or transport exceptions in request order.

        Kwargs:
            None

        Returns:
            httpx.Client: Test-owned client closed by test cleanup.

        Side Effects:
            Reset this test's request list and register client cleanup.
        """
        self.requests = []
        pending = iter(replies)

        def respond(request: httpx.Request) -> httpx.Response:
            """
            Record one synthetic request and return its queued outcome.

            Args:
                request: In-memory Scopus GET request.

            Kwargs:
                None

            Returns:
                httpx.Response: Next queued response.

            Raises:
                httpx.RequestError: If a transport failure was queued.
                StopIteration: If the adapter exceeds the expected requests.
            """
            self.requests.append(request)
            reply = next(pending)
            if isinstance(reply, httpx.RequestError):
                raise reply
            return reply

        client = httpx.Client(transport=httpx.MockTransport(respond),
                              follow_redirects=True)
        self.addCleanup(client.close)
        return client

    def test_queries_mapping_and_input_boundaries(self) -> None:
        """
        Translate literal filters and retain only real bibliographic fields.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        source = ScopusSource(api_key="fake-key", insttoken="fake-token")
        config = ScopusConfig(
            query="fallback", keywords=("semantic navigation", "reasoning"),
            venues=("Synthetic journal",), author_names=("Doe",),
            year_min=2018, year_max=2026, page_size=999)
        params = source.build_request_params(config, 0)
        self.assertEqual(params["query"],
                         '(TITLE-ABS-KEY("semantic navigation") OR '
                         'TITLE-ABS-KEY("reasoning")) AND '
                         '(SRCTITLE("Synthetic journal")) AND (AUTH("Doe")) '
                         'AND PUBYEAR > 2017 AND PUBYEAR < 2027')
        self.assertEqual(params["count"], 200)
        self.assertEqual(source.build_request_params(config, 4999)["count"], 1)
        self.assertNotIn("fake-key", str(params))
        self.assertNotIn("fake-token", str(params))
        self.assertEqual(source.default_headers(), {
            "Accept": "application/json", "X-ELS-APIKey": "fake-key",
            "X-ELS-Insttoken": "fake-token"})
        for invalid in (
            replace(config, page_size=0), replace(config, page_size=True),
            replace(config, year_min=True), replace(config, year_min=2027),
            replace(config, keywords=('bad" OR ALL',)),
            replace(config, keywords=("bad\nrow",)),
            ScopusConfig(query="   "),
        ):
            with self.subTest(config=invalid), self.assertRaises(ScopusError):
                source.build_request_params(invalid, 0)
        record = entry()
        record["dc:description"] = "Not an abstract retrieval response"
        record["references"] = ["Not resolved citation identifiers"]
        mapped, origin = map_scopus_to_canonical(record)
        self.assertEqual(origin, {"source": "scopus"})
        self.assertEqual(mapped["doi"], "10.1234/synthetic-1")
        self.assertEqual(mapped["publication_date"], date(2024, 4, 3))
        self.assertEqual(mapped["publication_year"], 2024)
        self.assertEqual(mapped["source_ids"], {"scopus": "2-s2.0-8500001"})
        self.assertEqual(mapped["citation_count"], 7)
        self.assertEqual(mapped["abstract"], "")
        self.assertEqual(mapped["references"], [])
        for value in (False, -1, [], "not-a-count"):
            with self.subTest(value=value), self.assertRaises(
                (TypeError, ValueError)
            ):
                map_scopus_to_canonical({**record, "citedby-count": value})
        legacy_link = {"@ref": "scopus", "@href":
                       "http://www.scopus.com/inward/record.url?scp=8500001"}
        mapped, _ = map_scopus_to_canonical({**record, "link": [legacy_link]})
        self.assertEqual(mapped["urls"][0]["url"],
                         "https://www.scopus.com/inward/record.url?scp=8500001")
        for unsafe in ("javascript:alert(1)", "https://www.scopus.com.evil/",
                       "https://secret@www.scopus.com/", "https://[bad"):
            mapped, _ = map_scopus_to_canonical({
                **record, "link": [{"@ref": "scopus", "@href": unsafe}]})
            self.assertEqual(mapped["urls"][0]["url"],
                             "https://www.scopus.com/record/display.uri?"
                             "eid=2-s2.0-8500001")

    def test_pagination_header_secrecy_and_client_ownership(self) -> None:
        """
        Page through results without putting credentials in query strings.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        client = self.client_for([
            page([entry(1), entry(2)], 3), page([entry(3)], 3)])
        source = ScopusSource(api_key="fake-key", insttoken="fake-token")
        with patch("literature.providers.scopus.time.sleep") as sleep:
            results, total = source.fetch(
                ScopusConfig(query="navigation", page_size=2), client)
        self.assertEqual((len(results), total), (3, 3))
        self.assertEqual([request.url.params["start"]
                          for request in self.requests], ["0", "2"])
        sleep.assert_called_once_with(0.4)
        for request in self.requests:
            self.assertEqual(request.headers["X-ELS-APIKey"], "fake-key")
            self.assertEqual(request.headers["X-ELS-Insttoken"], "fake-token")
            self.assertEqual(request.url.params["view"], "STANDARD")
            self.assertNotIn("fake-key", str(request.url))
            self.assertNotIn("fake-token", str(request.url))
        self.assertFalse(client.is_closed, "a borrowed client stays open")
        own = self.client_for([page([], 0)])
        with patch("literature.providers.scopus.httpx.Client", return_value=own):
            self.assertEqual(source.fetch(ScopusConfig(query="navigation")),
                             ([], 0))
        self.assertTrue(own.is_closed, "an owned client is closed")

    def test_page_ceiling_empty_results_and_offset_limit(self) -> None:
        """
        Honor page ceilings, empty-result sentinels and the 5,000-hit limit.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        source = ScopusSource(api_key="fake-key")
        client = self.client_for([page([entry()], 100)])
        results, total = source.fetch(
            ScopusConfig(query="navigation", max_pages=1), client)
        self.assertEqual(
            (len(results), total, len(self.requests)), (1, 100, 1))
        empty = self.client_for([page([{"error": "Result set was empty"}], 0)])
        self.assertEqual(source.fetch(ScopusConfig(query="none"), empty),
                         ([], 0))
        capped = self.client_for([
            page([entry(number) for number in range(start, start + 200)], 7000)
            for start in range(0, 5000, 200)])
        with patch("literature.providers.scopus.time.sleep"):
            results, total = source.fetch(
                ScopusConfig(query="navigation", page_size=200, max_pages=99),
                capped)
        self.assertEqual((len(results), total, len(self.requests)),
                         (5000, 7000, 25))
        self.assertEqual(self.requests[-1].url.params["start"], "4800")

    def test_access_quota_redirect_and_bad_response_errors_are_safe(self) -> None:
        """
        Reject denied, redirected or malformed responses without secret echoes.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        source = ScopusSource(api_key="fake-key", insttoken="fake-token")
        replies = [
            httpx.Response(code, text="fake-key fake-token", headers={
                "Location": "https://example.invalid/"})
            for code in (400, 401, 403, 302)
        ] + [
            httpx.Response(429, text="fake-key fake-token",
                           headers={"X-ELS-Status": "QUOTA_EXCEEDED"}),
            httpx.Response(200, text="fake-key fake-token"),
            httpx.Response(200, json={"service-error": "fake-key"}),
            httpx.Response(200, json={"search-results": {
                "opensearch:totalResults": "fake-token", "entry": []}}),
            page([{"dc:title": {"bad": "fake-key"}}], 1),
            httpx.Response(200, content=b"a" * 2_000_001),
        ]
        for response in replies:
            with self.subTest(status=response.status_code):
                client = self.client_for([response])
                with self.assertRaises(ScopusError) as caught:
                    source.fetch(ScopusConfig(query="navigation"), client)
                self.assertNotIn("fake-key", str(caught.exception))
                self.assertNotIn("fake-token", str(caught.exception))
                self.assertEqual(len(self.requests), 1)

    def test_retries_and_missing_credentials_fail_without_leaks(self) -> None:
        """
        Bound retry waits and reject missing/invalid keys before HTTP creation.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        config = ScopusConfig(query="navigation")
        for key in ("", "fake key", "fake\nkey", "a" * 4097):
            with self.subTest(key_length=len(key)), patch(
                "literature.providers.scopus.httpx.Client"
            ) as factory, self.assertRaises(ScopusError):
                ScopusSource(api_key=key).fetch(config)
            factory.assert_not_called()
        source = ScopusSource(api_key="fake-key")
        client = self.client_for([
            httpx.Response(503, text="fake-key"),
            httpx.Response(429, headers={"Retry-After": "1000"}), page([], 0)])
        with patch("literature.providers.scopus.time.sleep") as sleep:
            self.assertEqual(source.fetch(config, client), ([], 0))
        self.assertEqual(sleep.call_args_list, [call(1), call(30)])
        failed = self.client_for([
            httpx.ConnectError("fake-key fake-token") for _ in range(3)])
        with (patch("literature.providers.scopus.time.sleep") as sleep,
              self.assertRaises(ScopusError) as caught):
            source.fetch(config, failed)
        self.assertNotIn("fake-key", str(caught.exception))
        self.assertNotIn("fake-token", str(caught.exception))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])


@override_settings(ELSEVIER_API_KEY="fake-key", ELSEVIER_INSTTOKEN="",
                   OPENALEX_EMAIL="")
class ScopusIntegrationTests(TestCase):
    """
    Keep Scopus opt-in and reuse project-scoped audits and deduplication.
    """

    def test_private_settings_save_and_fresh_process_loading(self) -> None:
        """
        Preserve hidden credentials across reload, blank save and restart.

        Args:
            None

        Kwargs:
            None

        Returns:
            None

        Side Effects:
            Write a 0600 credential file under a unique temporary directory
            and start a database-free child using only that synthetic file.
            No inherited provider credentials or external APIs are used.
        """
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as tmp:
            credentials = Path(tmp) / "credentials.env"
            with (override_settings(LOCAL_SURVEY_CREDENTIALS_FILE=credentials),
                  patch.dict(os.environ, {"WP_LAUNCHER": "1"})):
                response = self.client.post(reverse("settings_view"), {
                    "elsevier_api_key": "synthetic-startup-key",
                    "elsevier_insttoken": "synthetic-startup-token"})
                original = credentials.read_bytes()
                reloaded = self.client.get(reverse("settings_view"))
                for secret in ("synthetic-startup-key",
                               "synthetic-startup-token"):
                    self.assertNotContains(reloaded, secret)
                self.assertContains(reloaded, "Configured (hidden)")
                self.assertEqual(credentials.read_bytes(), original)
                unchanged = self.client.post(reverse("settings_view"), {
                    "elsevier_api_key": "", "elsevier_insttoken": ""})
                self.assertEqual(unchanged.status_code, 302)
                self.assertEqual(credentials.read_bytes(), original)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
            root = Path(__file__).resolve().parents[2]
            unused_database = Path(tmp) / "unused-worker.sqlite3"
            environment = {
                "DJANGO_SETTINGS_MODULE": "config.settings",
                "PYTHONPATH": str(root),
                "LOCAL_SURVEY_DB": str(unused_database),
                "LOCAL_SURVEY_CREDENTIALS_FILE": str(credentials),
                "WP_ROLE": "worker",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            script = (
                "import django; django.setup(); "
                "from literature.providers.scopus import ScopusSource; "
                "headers = ScopusSource().default_headers(); "
                "assert headers['X-ELS-APIKey'] == 'synthetic-startup-key'; "
                "assert headers['X-ELS-Insttoken'] == 'synthetic-startup-token'"
            )
            restarted = subprocess.run(
                [sys.executable, "-c", script], cwd=root, env=environment,
                capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertFalse(unused_database.exists())

    def test_opt_in_defaults_settings_and_search_forms(self) -> None:
        """
        Preserve legacy selections and permit Scopus-only credential setup.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for selection in (None, [], ["invalid"]):
            self.assertEqual(effective_providers(selection),
                             list(DEFAULT_SEARCH_PROVIDERS))
        fresh = Project.objects.create(name="Synthetic empty project")
        self.assertEqual(configuration_preview(fresh, ["", ""])["proposal"][
            "sources_selected"], list(DEFAULT_SEARCH_PROVIDERS))
        search = Search.objects.create(
            project=fresh, name="Synthetic", keywords=["navigation"],
            is_current=True)
        self.assertEqual(search.sources_selected,
                         list(DEFAULT_SEARCH_PROVIDERS))
        search.sources_selected = ["scopus"]
        search.save()
        search.refresh_from_db()
        self.assertEqual(search.sources_selected, ["scopus"])
        self.assertEqual(configuration_preview(fresh, ["", ""])["proposal"][
            "sources_selected"], ["scopus"])
        form = SearchForm({"name": "Synthetic", "sources_selected": ["scopus"],
                           "keywords_json": json.dumps(["navigation"])})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(selected_origin("scopus"), "scopus")
        with override_settings(ELSEVIER_API_KEY=""):
            self.assertTrue(SettingsForm(
                {"elsevier_api_key": "fake-key"}).is_valid())
            self.assertFalse(SettingsForm({}).is_valid())
        with (tempfile.TemporaryDirectory(dir="/tmp/opencode") as tmp,
              override_settings(LOCAL_SURVEY_CREDENTIALS_FILE=Path(
                  tmp) / "credentials.env"),
              patch("literature.views.set_key") as save):
            response = self.client.post(reverse("settings_view"), {
                "elsevier_api_key": "fake-new-key"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any(item.args[1] == "ELSEVIER_API_KEY"
                            for item in save.call_args_list))
        self.assertFalse(any(item.args[1] == "OPENALEX_EMAIL"
                             for item in save.call_args_list))

    def test_scopus_run_reuses_dedup_and_preserves_curated_fields(self) -> None:
        """
        Store Scopus provenance without overwriting existing paper edits.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Synthetic Scopus project")
        search = Search.objects.create(
            project=project, name="Synthetic", keywords=["navigation"],
            sources_selected=["scopus"], is_current=True)
        paper = Paper.objects.create(
            doi="10.1234/synthetic-1", title="User-maintained title",
            abstract="User-maintained abstract", sources=["openalex"])
        Discovery.objects.create(paper=paper, search=search, source="openalex")
        mapped, _ = map_scopus_to_canonical(entry())
        with (
            patch("literature.providers.scopus.ScopusSource.fetch",
                  return_value=([ProviderResult(mapped, entry())], 1)),
            patch("literature.services.search_service.enrich_papers"),
        ):
            response = self.client.post(
                reverse("search_run", args=[project.pk, search.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Paper.objects.count(), 1)
        paper.refresh_from_db()
        self.assertEqual(paper.title, "User-maintained title")
        self.assertEqual(paper.abstract, "User-maintained abstract")
        self.assertEqual(set(paper.sources), {"openalex", "scopus"})
        self.assertEqual(paper.source_ids["scopus"], "2-s2.0-8500001")
        self.assertEqual(paper.discoveries.filter(source="scopus").count(), 1)
        audit = SearchRun.objects.get(source="scopus")
        self.assertEqual(audit.query_used, '(TITLE-ABS-KEY("navigation"))')
        self.assertEqual(
            (audit.status, audit.duplicates_count), ("completed", 1))

        # A DOI-less paper remains the same entry after the user edits its title.
        paper.doi = None
        paper.save(update_fields=["doi"])
        mapped["doi"] = None
        for identifier in ("8500001", "SCOPUS_ID:8500001", "2-s2.0-8500001"):
            mapped["source_ids"] = {"scopus": identifier}
            with (patch("literature.providers.scopus.ScopusSource.fetch",
                        return_value=([ProviderResult(mapped, entry())], 1)),
                  patch("literature.services.search_service.enrich_papers")):
                repeated = run_search(search.pk)[0]
            self.assertEqual(Paper.objects.count(), 1)
            self.assertEqual(repeated.duplicates_count, 1)
            paper.refresh_from_db()
            self.assertEqual(paper.title, "User-maintained title")

    def test_scopus_failure_is_audited_without_stopping_other_providers(self) -> None:
        """
        Retain a safe Scopus failure audit while other providers succeed.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Synthetic mixed search")
        search = Search.objects.create(
            project=project, name="Synthetic", keywords=["navigation"],
            sources_selected=["openalex", "scopus"], is_current=True)
        with (
            override_settings(ELSEVIER_API_KEY=""),
            patch("literature.services.search_service.OpenAlexSource.fetch",
                  return_value=([], 0)),
            patch("literature.providers.scopus.httpx.Client") as factory,
            patch("literature.services.search_service.time.sleep"),
        ):
            runs = run_search(search.pk)
        factory.assert_not_called()
        self.assertEqual([(run.source, run.status) for run in runs],
                         [("openalex", "completed"), ("scopus", "failed")])
        self.assertIn("Elsevier Scopus API key", runs[1].error_message)
        self.assertEqual(Paper.objects.count(), 0)
