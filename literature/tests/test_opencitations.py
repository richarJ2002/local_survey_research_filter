#!/usr/bin/env python3

"""Verify bounded OpenCitations requests, DOI caching, and graph integration.

@File:   test_opencitations.py

@Brief:  DOI citation cache, bounded API calls, and graph integration.

@Date:   30/09/2026

@Copyright: 2026 Aerospace Engineering Research & Operation Systems
    (AERO-Sys) group.
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

# Custom library Imports
import httpx
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from literature.models import (
    Discovery, OpenCitationsLookup, Paper, Project, Search,
)
from literature.services.citation_service import build_graph
from literature.services.opencitations_service import (
    enrich_project, parse_edges,
)

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes


class OpenCitationsTests(TestCase):
    """Only explicit POST lookups fetch; graph reads cached DOI links."""

    def _project(self) -> tuple[Project, Paper, Paper]:
        """Discover two DOI-bearing papers without stored provider references.

        Args:
            None

        Kwargs:
            None

        Returns:
            tuple[Project, Paper, Paper]: Project, citing paper, then cited
                paper, each discovered by the fixture search.
        """
        project = Project.objects.create(name="P")
        search = Search.objects.create(
            project=project, name="S", keywords=["a"])
        citing = Paper.objects.create(title="Citing", doi="10.1/new")
        cited = Paper.objects.create(title="Cited", doi="10.2/old")
        for paper in (citing, cited):
            Discovery.objects.create(
                paper=paper, search=search, source="crossref", query="a")
        return project, citing, cited

    def test_parse_pid_fields_and_ignore_missing_dois(self) -> None:
        """Extract normalized DOI pairs, dropping missing IDs and self-links.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        payload = [
            {"citing": "omid:br/1 doi:10.1/NEW; doi:10.1/other",
             "cited": "pmid:123 doi:10.2/OLD"},
            {"citing": "pmid:321", "cited": "doi:10.2/old"},
            {"citing": "doi:10.1/new", "cited": "doi:10.1/new"},
        ]
        self.assertEqual(parse_edges(payload), [
            ["10.1/new", "10.2/old"], ["10.1/other", "10.2/old"],
        ])

    def test_incoming_links_are_cached_and_visible_without_paper_references(
        self,
    ) -> None:
        """Reuse cached incoming edges without rewriting paper references.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project, citing, cited = self._project()
        requested: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            """Record lookups and serve the cited paper's incoming DOI edge.

            Args:
                request (httpx.Request): Mock lookup whose URL determines
                    whether the incoming edge or an empty result is returned.

            Kwargs:
                None

            Returns:
                httpx.Response: HTTP 200 JSON list containing the fixture edge
                    for the cited paper's incoming lookup, otherwise empty.
            """
            requested.append(str(request.url))
            if "/citations/doi:10.2%2Fold" in str(request.url):
                return httpx.Response(200, json=[{
                    "citing": "doi:10.1/new", "cited": "doi:10.2/old",
                }])
            return httpx.Response(200, json=[])

        with (
            httpx.Client(transport=httpx.MockTransport(respond)) as client,
            patch("literature.services.opencitations_service.time.sleep"),
        ):
            first = enrich_project(project, client=client)
            second = enrich_project(project, client=client)
        self.assertEqual(first["checked"], 4)
        self.assertEqual(first["matched"], 1)
        self.assertEqual(first["remaining"], 0)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(second["cached"], 4)
        self.assertEqual(len(requested), 4)
        self.assertEqual(
            set(build_graph(project, seed_stage="found")["edges"]),
            {(citing.pk, cited.pk)})
        citing.refresh_from_db()
        # Cache does not rewrite provider data.
        self.assertEqual(citing.references, [])

    def test_bounded_batches_and_expired_cache(self) -> None:
        """Honor two-request batches and refresh only the expired cache entry.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project, _, _ = self._project()
        requests = 0

        def respond(request: httpx.Request) -> httpx.Response:
            """Count mock lookups and return a cacheable empty citation result.

            Args:
                request (httpx.Request): Lookup request accepted without
                    inspecting its URL or headers.

            Kwargs:
                None

            Returns:
                httpx.Response: HTTP 200 response with an empty JSON list.
            """
            nonlocal requests
            requests += 1
            return httpx.Response(200, json=[])

        with (
            httpx.Client(transport=httpx.MockTransport(respond)) as client,
            patch("literature.services.opencitations_service.time.sleep"),
        ):
            first = enrich_project(project, max_requests=2, client=client)
            second = enrich_project(project, max_requests=2, client=client)
            self.assertEqual((first["remaining"], second["remaining"]), (2, 0))
            self.assertEqual(requests, 4)
            OpenCitationsLookup.objects.filter(
                doi="10.1/new", direction="outgoing"
            ).update(
                checked_at=timezone.now() - timedelta(days=31)
            )
            third = enrich_project(project, max_requests=2, client=client)
        self.assertEqual(third["checked"], 1)
        self.assertEqual(requests, 5)

    def test_rate_limit_stops_batch_without_caching_failure(self) -> None:
        """Stop after the first rate limit and leave all DOI lookups uncached.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project, _, _ = self._project()
        requested = 0

        def respond(request: httpx.Request) -> httpx.Response:
            """Count the attempted lookup and simulate an HTTP rate limit.

            Args:
                request (httpx.Request): Mock lookup accepted without
                    inspecting its URL or headers.

            Kwargs:
                None

            Returns:
                httpx.Response: HTTP 429 response without a JSON payload.
            """
            nonlocal requested
            requested += 1
            return httpx.Response(429)

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            result = enrich_project(project, client=client)
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["remaining"], 4)
        self.assertEqual(requested, 1)
        self.assertEqual(OpenCitationsLookup.objects.count(), 0)

    @override_settings(OPENCITATIONS_TOKEN="test-token")
    def test_token_used_in_header_only(self) -> None:
        """Send the configured token in authorization, never in the lookup URL.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project, _, _ = self._project()
        captured: dict[str, str] = {}

        def respond(request: httpx.Request) -> httpx.Response:
            """Capture authorization and URL and serve an empty DOI result.

            Args:
                request (httpx.Request): Mock request whose authorization
                    header and URL are retained for token assertions.

            Kwargs:
                None

            Returns:
                httpx.Response: HTTP 200 response with an empty JSON list.
            """
            captured["authorization"] = request.headers.get(
                "authorization", "")
            captured["url"] = str(request.url)
            return httpx.Response(200, json=[])

        real_client = httpx.Client

        def make_client(**kwargs) -> httpx.Client:
            """Construct a real client with the token-capturing mock transport.

            Args:
                None

            Kwargs:
                kwargs (dict): Unpacked httpx.Client options such as
                    timeout, forwarded unchanged alongside the mock transport.

            Returns:
                httpx.Client: Client routing requests to the local callback.
            """
            return real_client(
                transport=httpx.MockTransport(respond), **kwargs)

        with patch(
            "literature.services.opencitations_service.httpx.Client",
            side_effect=make_client,
        ):
            enrich_project(project, max_requests=1)
        self.assertEqual(captured["authorization"], "test-token")
        self.assertNotIn("test-token", captured["url"])

    def test_view_is_post_only(self) -> None:
        """Reject GET enrichment and redirect after one explicit POST call.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project, _, _ = self._project()
        url = reverse("citations_enrich", args=[project.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        with patch(
            "literature.services.opencitations_service.enrich_project",
            return_value={
                "checked": 1, "cached": 0, "matched": 1, "failed": 0,
                "remaining": 0, "rate_limited": False,
            },
        ) as enrich:
            response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        enrich.assert_called_once_with(project)

    def test_project_delete_only_discards_unused_citation_cache(self) -> None:
        """Delete unused DOI cache but retain another project's cached lookup.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project, _, _ = self._project()
        other = Project.objects.create(name="Other")
        search = Search.objects.create(
            project=other, name="S2", keywords=["b"])
        paper = Paper.objects.create(
            title="Shared cache keeper", doi="10.3/kept")
        Discovery.objects.create(
            paper=paper, search=search, source="crossref", query="b")
        for doi in ("10.1/new", "10.3/kept"):
            OpenCitationsLookup.objects.create(
                doi=doi, direction="outgoing", edges=[])
        self.assertEqual(self.client.post(
            reverse("project_delete", args=[project.pk])).status_code, 302)
        self.assertEqual(
            set(OpenCitationsLookup.objects.values_list("doi", flat=True)),
            {"10.3/kept"},
        )
