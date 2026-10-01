#!/usr/bin/env python3

'''
@File:   test_enrichment.py

@Brief:  Tests for OpenAlex enrichment and incomplete classification.

@Date:   29/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.test import TestCase, override_settings

from literature.models import Paper, Project, Search
from literature.services.reference_service import (
    backfill_missing_references,
    enrich_papers,
)

# ------------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------------ #


class FakeResp:
    """Minimal httpx response double."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = ""

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """httpx.Client double serving one canned payload."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls = 0

    def get(self, *args, **kwargs) -> FakeResp:
        self.calls += 1
        return FakeResp(self._payload)

    def close(self) -> None:
        pass


class FallbackClient:
    """Route DOI lookups to independent provider responses."""

    def __init__(self, crossref: dict, semantic: dict) -> None:
        self.crossref = crossref
        self.semantic = semantic
        self.headers: dict = {}

    def get(self, url: str, **kwargs) -> FakeResp:
        if "openalex.org" in url:
            return FakeResp({"results": []})
        if "crossref.org" in url:
            return FakeResp({"message": self.crossref})
        self.headers = kwargs.get("headers", {})
        return FakeResp(self.semantic)


# ------------------------------------------------------------------------ #
# Enrichment tests
# ------------------------------------------------------------------------ #


class EnrichmentTests(TestCase):
    """Missing fields are filled from lookup; present fields untouched."""

    # --- Fills blanks ---
    def test_enrich_fills_blanks_only(self) -> None:
        """Abstract + refs filled; existing values never overwritten."""
        project = Project.objects.create(name="P")
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        from literature.models import Discovery

        thin = Paper.objects.create(title="Thin", doi="10.1/thin")
        Discovery.objects.create(paper=thin, search=search, source="crossref", query="a")
        payload = {
            "results": [
                {
                    "doi": "https://doi.org/10.1/thin",
                    "title": "Thin Full",
                    "abstract_inverted_index": {"Some": [0], "text": [1]},
                    "referenced_works": ["https://openalex.org/W1"],
                }
            ]
        }
        filled = enrich_papers([thin], client=FakeClient(payload))
        self.assertEqual(filled, 1)
        thin.refresh_from_db()
        self.assertEqual(thin.abstract, "Some text")
        self.assertEqual(thin.references, ["https://openalex.org/W1"])
        self.assertEqual(thin.title, "Thin")

    # --- No DOI skipped ---
    def test_no_doi_skipped(self) -> None:
        """Papers without DOI cannot be enriched."""
        paper = Paper.objects.create(title="No DOI")
        client = FakeClient({"results": []})
        self.assertEqual(enrich_papers([paper], client=client), 0)
        self.assertEqual(client.calls, 0)

    @override_settings(SEMANTIC_SCHOLAR_KEY="test-key")
    def test_crossref_and_s2_fill_missing_fields(self) -> None:
        """Crossref DOI references and S2 abstract complement an OpenAlex miss."""
        thin = Paper.objects.create(title="Thin", doi="10.1/thin")
        client = FallbackClient(
            {"title": ["Other title"], "reference": [{"DOI": "10.2/older"}]},
            {"abstract": "An abstract", "references": []},
        )
        self.assertEqual(enrich_papers([thin], client=client), 1)
        thin.refresh_from_db()
        self.assertEqual(thin.title, "Thin")
        self.assertEqual(thin.abstract, "An abstract")
        self.assertEqual(thin.references, ["https://doi.org/10.2/older"])
        self.assertEqual(client.headers.get("x-api-key"), "test-key")

    def test_s2_reference_ids_fill_when_crossref_has_none(self) -> None:
        """S2 DOI and paper IDs remain matchable citation identifiers."""
        thin = Paper.objects.create(title="Thin", doi="10.1/thin", abstract="Present")
        client = FallbackClient(
            {},
            {"references": [
                {"citedPaper": {"externalIds": {"DOI": "10.2/older"}}},
                {"citedPaper": {"paperId": "s2-paper", "externalIds": {}}},
            ]},
        )
        self.assertEqual(enrich_papers([thin], client=client), 1)
        thin.refresh_from_db()
        self.assertEqual(thin.references, ["https://doi.org/10.2/older", "S2:s2-paper"])

    def test_enrichment_replaces_audience_placeholder_not_real_abstract(self) -> None:
        placeholder = Paper.objects.create(title="SMaNa", doi="10.1000/smana", abstract="International audience", references=["https://doi.org/10.1000/ref"])
        real = Paper.objects.create(title="Other", doi="10.1000/other", abstract="A genuine short abstract.", references=["https://doi.org/10.1000/ref"])
        client = FallbackClient({}, {"abstract": "Retrieved real abstract"})
        self.assertEqual(enrich_papers([placeholder, real], client=client), 1)
        placeholder.refresh_from_db()
        real.refresh_from_db()
        self.assertEqual(placeholder.abstract, "Retrieved real abstract")
        self.assertEqual(real.abstract, "A genuine short abstract.")

    def test_lookup_ignores_audience_tag_and_continues_provider_fallback(self) -> None:
        from literature.services.reference_service import lookup_doi

        client = FallbackClient({"title": ["SMaNa"], "abstract": "International audience"}, {"abstract": "The actual abstract"})
        self.assertEqual(lookup_doi("10.1000/smana", client=client)["abstract"], "The actual abstract")

    def test_crossref_can_replace_audience_tag_when_only_abstract_is_missing(self) -> None:
        paper = Paper.objects.create(title="SMaNa", doi="10.1000/smana", abstract="International audience", references=["https://doi.org/10.1000/ref"])
        client = FallbackClient({"abstract": "Actual Crossref abstract"}, {})
        self.assertEqual(enrich_papers([paper], client=client), 1)
        paper.refresh_from_db()
        self.assertEqual(paper.abstract, "Actual Crossref abstract")

    def test_s2_direct_paper_references_are_not_discarded(self) -> None:
        """The /paper endpoint returns direct referenced Paper objects, not citation edges."""
        thin = Paper.objects.create(title="Thin", doi="10.1000/thin", abstract="Present")
        client = FallbackClient({}, {"references": [
            {"externalIds": {"DOI": "10.1000/OLDER"}, "paperId": "abc"},
            {"externalIds": {}, "paperId": "def"},
            {"externalIds": None, "paperId": None},
            None,
        ]})
        self.assertEqual(enrich_papers([thin], client=client), 1)
        thin.refresh_from_db()
        self.assertEqual(thin.references, ["https://doi.org/10.1000/older", "S2:def"])

    def test_lookup_retains_authors_year_venue_and_reference_links(self) -> None:
        """Complementary metadata reaches the read-only preview rather than being discarded."""
        from literature.services.reference_service import lookup_doi

        client = FallbackClient(
            {"title": ["A paper"], "author": [{"given": "Jane", "family": "Researcher"}],
             "published": {"date-parts": [[2025]]}, "container-title": ["A journal"],
             "reference": [{"DOI": "10.1000/older"}]},
            {"abstract": "The abstract", "references": []},
        )
        data = lookup_doi("10.1000/example", client=client)
        self.assertEqual(data["title"], "A paper")
        self.assertEqual(data["authors"][0]["name"], "Jane Researcher")
        self.assertEqual(data["publication_year"], 2025)
        self.assertEqual(data["venue"], "A journal")
        self.assertEqual(data["references"], ["https://doi.org/10.1000/older"])
        self.assertEqual(data["abstract"], "The abstract")

    def test_malformed_provider_payloads_do_not_crash_lookup(self) -> None:
        """A non-object JSON response is treated as a provider miss, not trusted metadata."""
        from literature.services.reference_service import lookup_doi

        data = lookup_doi("10.1000/example", client=FakeClient([]))
        self.assertFalse(data["found"])
        self.assertEqual(data["references"], [])
        self.assertEqual(data["title"], "")

    def test_s2_rate_limit_stops_further_s2_requests(self) -> None:
        """A shared-limit response should not trigger another DOI S2 call."""
        class LimitedClient(FallbackClient):
            def __init__(self) -> None:
                super().__init__({}, {})
                self.s2_calls = 0

            def get(self, url: str, **kwargs) -> FakeResp:
                if "semanticscholar.org" in url:
                    self.s2_calls += 1
                    return FakeResp({}, status=429)
                return super().get(url, **kwargs)

        client = LimitedClient()
        papers = [Paper.objects.create(title=f"P{i}", doi=f"10.1/{i}") for i in range(2)]
        self.assertEqual(enrich_papers(papers, client=client), 0)
        self.assertEqual(client.s2_calls, 1)

    # --- Backfill view ---
    def test_backfill_button(self) -> None:
        """POST fills corpus gaps and redirects with counts."""
        project = Project.objects.create(name="P")
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        from literature.models import Discovery

        thin = Paper.objects.create(title="Thin", doi="10.1/thin")
        Discovery.objects.create(paper=thin, search=search, source="crossref", query="a")
        payload = {
            "results": [
                {
                    "doi": "https://doi.org/10.1/thin",
                    "title": "Thin Full",
                    "abstract_inverted_index": {"Some": [0], "text": [1]},
                    "referenced_works": ["https://openalex.org/W1"],
                }
            ]
        }
        from unittest.mock import patch

        with patch(
            "literature.services.reference_service.httpx.Client",
            return_value=FakeClient(payload),
        ):
            resp = self.client.post(f"/project/{project.pk}/refs/backfill/")
        self.assertEqual(resp.status_code, 302)
        thin.refresh_from_db()
        self.assertEqual(thin.references, ["https://openalex.org/W1"])
