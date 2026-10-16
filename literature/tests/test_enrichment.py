#!/usr/bin/env python3

"""
Verify missing-field enrichment and isolated provider fallbacks.

@File:   test_enrichment.py

@Brief:  Tests for OpenAlex enrichment and incomplete classification.

@Date:   29/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems
    (AERO-Sys) group.

"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

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
    """
    Minimal httpx response double.
    """

    def __init__(self, payload: dict, status: int = 200) -> None:
        """
        Initialize a canned JSON response with empty headers and text.

        Args:
            payload: Provider JSON dictionary or malformed-response list,
                returned unchanged by json().

        Kwargs:
            status (int): HTTP status exposed to lookup code. Defaults to 200.

        Returns:
            None
        """
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = ""

    def json(self) -> dict:
        """
        Return the canned provider JSON value without decoding it.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: Stored provider metadata; malformed-response fixtures return
                a list instead, without coercion.
        """
        return self._payload


class FakeClient:
    """
    httpx.Client double serving one canned payload.
    """

    def __init__(self, payload: dict) -> None:
        """
        Store one provider payload and initialize the request counter.

        Args:
            payload: Provider JSON dictionary or malformed-response list
                served unchanged for every lookup URL.

        Kwargs:
            None

        Returns:
            None
        """
        self._payload = payload
        self.calls = 0

    def get(self, *args, **kwargs) -> FakeResp:
        """
        Count a lookup and return the same successful canned response.

        Args:
            args (tuple): Collected HTTP GET arguments, including the URL;
                ignored because all lookups share the payload.

        Kwargs:
            kwargs (dict): Collected HTTP GET options such as params and
                headers; ignored by this response double.

        Returns:
            FakeResp: HTTP 200 response containing the stored JSON payload.
        """
        self.calls += 1
        return FakeResp(self._payload)

    def close(self) -> None:
        """
        Accept client cleanup without acquiring or releasing resources.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        pass


class FallbackClient:
    """
    Route DOI lookups to independent provider responses.
    """

    def __init__(self, crossref: dict, semantic: dict) -> None:
        """
        Store independent Crossref and Semantic Scholar response payloads.

        Args:
            crossref (dict): Work metadata wrapped in Crossref's message key.
            semantic (dict): Semantic Scholar paper metadata served directly.

        Kwargs:
            None

        Returns:
            None
        """
        self.crossref = crossref
        self.semantic = semantic
        self.headers: dict = {}

    def get(self, url: str, **kwargs) -> FakeResp:
        """
        Route provider lookups and capture Semantic Scholar headers.

        Args:
            url (str): Lookup URL selecting an empty OpenAlex result, the
                Crossref payload, or the Semantic Scholar fallback.

        Kwargs:
            kwargs (dict): Collected HTTP GET options; headers, when present,
                are recorded for the Semantic Scholar request.

        Returns:
            FakeResp: Successful JSON response for the selected provider.
        """
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
    """
    Missing fields are filled from lookup; present fields untouched.
    """

    # --- Fills blanks ---
    def test_enrich_fills_blanks_only(self) -> None:
        """
        Fill missing abstract and references without replacing the title.

        Abstract + refs filled; existing values never overwritten.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="P")
        search = Search.objects.create(
            project=project, name="S", keywords=["a"])
        from literature.models import Discovery

        thin = Paper.objects.create(title="Thin", doi="10.1/thin")
        Discovery.objects.create(
            paper=thin, search=search, source="crossref", query="a")
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
        """
        Skip DOI-less enrichment without making any provider requests.

        Papers without DOI cannot be enriched.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        paper = Paper.objects.create(title="No DOI")
        client = FakeClient({"results": []})
        self.assertEqual(enrich_papers([paper], client=client), 0)
        self.assertEqual(client.calls, 0)

    @override_settings(SEMANTIC_SCHOLAR_KEY="test-key")
    def test_crossref_and_s2_fill_missing_fields(self) -> None:
        """
        Combine Crossref refs and an S2 abstract after an OpenAlex miss.

        Crossref DOI references and S2 abstract complement an OpenAlex miss.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
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
        """
        Retain S2 DOI and paper-ID references when Crossref has none.

        S2 DOI and paper IDs remain matchable citation identifiers.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        thin = Paper.objects.create(
            title="Thin", doi="10.1/thin", abstract="Present")
        client = FallbackClient(
            {},
            {"references": [
                {"citedPaper": {"externalIds": {"DOI": "10.2/older"}}},
                {"citedPaper": {"paperId": "s2-paper", "externalIds": {}}},
            ]},
        )
        self.assertEqual(enrich_papers([thin], client=client), 1)
        thin.refresh_from_db()
        self.assertEqual(thin.references, [
                         "https://doi.org/10.2/older", "S2:s2-paper"])

    def test_enrichment_replaces_audience_placeholder_not_real_abstract(
        self,
    ) -> None:
        """
        Replace the audience placeholder while preserving a real abstract.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        placeholder = Paper.objects.create(
            title="SMaNa", doi="10.1000/smana",
            abstract="International audience",
            references=["https://doi.org/10.1000/ref"],
        )
        real = Paper.objects.create(
            title="Other", doi="10.1000/other",
            abstract="A genuine short abstract.",
            references=["https://doi.org/10.1000/ref"],
        )
        client = FallbackClient({}, {"abstract": "Retrieved real abstract"})
        self.assertEqual(enrich_papers([placeholder, real], client=client), 1)
        placeholder.refresh_from_db()
        real.refresh_from_db()
        self.assertEqual(placeholder.abstract, "Retrieved real abstract")
        self.assertEqual(real.abstract, "A genuine short abstract.")

    def test_lookup_ignores_audience_tag_and_continues_provider_fallback(
        self,
    ) -> None:
        """
        Skip Crossref's audience tag and retain the S2 abstract fallback.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.services.reference_service import lookup_doi

        client = FallbackClient(
            {"title": ["SMaNa"], "abstract": "International audience"},
            {"abstract": "The actual abstract"},
        )
        self.assertEqual(lookup_doi("10.1000/smana", client=client)
                         ["abstract"], "The actual abstract")

    def test_crossref_can_replace_audience_tag_when_only_abstract_is_missing(
        self,
    ) -> None:
        """
        Fill the placeholder abstract from Crossref despite existing refs.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        paper = Paper.objects.create(
            title="SMaNa", doi="10.1000/smana",
            abstract="International audience",
            references=["https://doi.org/10.1000/ref"],
        )
        client = FallbackClient({"abstract": "Actual Crossref abstract"}, {})
        self.assertEqual(enrich_papers([paper], client=client), 1)
        paper.refresh_from_db()
        self.assertEqual(paper.abstract, "Actual Crossref abstract")

    def test_s2_direct_paper_references_are_not_discarded(self) -> None:
        """
        Retain DOI and S2 IDs from direct reference objects.

        The /paper endpoint returns direct referenced Paper objects, not
        citation edges.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        thin = Paper.objects.create(
            title="Thin", doi="10.1000/thin", abstract="Present")
        client = FallbackClient({}, {"references": [
            {"externalIds": {"DOI": "10.1000/OLDER"}, "paperId": "abc"},
            {"externalIds": {}, "paperId": "def"},
            {"externalIds": None, "paperId": None},
            None,
        ]})
        self.assertEqual(enrich_papers([thin], client=client), 1)
        thin.refresh_from_db()
        self.assertEqual(thin.references, [
                         "https://doi.org/10.1000/older", "S2:def"])

    def test_lookup_retains_authors_year_venue_and_reference_links(
        self,
    ) -> None:
        """
        Keep authors, year, venue, abstract, and refs in the lookup preview.

        Complementary metadata reaches the read-only preview rather than
        being discarded.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.services.reference_service import lookup_doi

        client = FallbackClient(
            {"title": ["A paper"],
             "author": [{"given": "Jane", "family": "Researcher"}],
             "published": {"date-parts": [[2025]]},
             "container-title": ["A journal"],
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
        """
        Treat non-object provider JSON as a miss with empty metadata.

        A non-object JSON response is treated as a provider miss, not trusted
        metadata.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.services.reference_service import lookup_doi

        data = lookup_doi("10.1000/example", client=FakeClient([]))
        self.assertFalse(data["found"])
        self.assertEqual(data["references"], [])
        self.assertEqual(data["title"], "")

    def test_lookup_keeps_maximum_citation_count_across_providers(self) -> None:
        """
        Snapshot the highest reported count for proportional graph nodes.

        OpenAlex reports nothing while Crossref and S2 disagree; the
        merged preview keeps the maximum without other field changes.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.services.reference_service import lookup_doi

        client = FallbackClient(
            {"is-referenced-by-count": 5},
            {"citationCount": 9},
        )
        self.assertEqual(
            lookup_doi("10.1000/example", client=client)["citation_count"], 9)

    def test_s2_rate_limit_stops_further_s2_requests(self) -> None:
        """
        Stop S2 lookups after one shared-limit response across two papers.

        A shared-limit response should not trigger another DOI S2 call.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        class LimitedClient(FallbackClient):
            """
            Simulate a shared Semantic Scholar limit and count S2 calls.
            """

            def __init__(self) -> None:
                """
                Initialize empty fallback payloads and the S2 call counter.

                Args:
                    None

                Kwargs:
                    None

                Returns:
                    None
                """
                super().__init__({}, {})
                self.s2_calls = 0

            def get(self, url: str, **kwargs) -> FakeResp:
                """
                Return HTTP 429 for S2 and route other provider lookups.

                Args:
                    url (str): Provider URL used to identify S2 requests.

                Kwargs:
                    kwargs (dict): Collected HTTP GET options forwarded
                        unchanged for non-S2 lookups, including params and
                        headers.

                Returns:
                    FakeResp: Rate-limit response for S2 or the inherited
                        successful response for another provider.
                """
                if "semanticscholar.org" in url:
                    self.s2_calls += 1
                    return FakeResp({}, status=429)
                return super().get(url, **kwargs)

        client = LimitedClient()
        papers = [Paper.objects.create(
            title=f"P{i}", doi=f"10.1/{i}") for i in range(2)]
        self.assertEqual(enrich_papers(papers, client=client), 0)
        self.assertEqual(client.s2_calls, 1)

    # --- Backfill view ---
    def test_backfill_button(self) -> None:
        """
        Fill missing references and redirect after a backfill POST.

        POST fills corpus gaps and redirects with counts.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="P")
        search = Search.objects.create(
            project=project, name="S", keywords=["a"])
        from literature.models import Discovery

        thin = Paper.objects.create(title="Thin", doi="10.1/thin")
        Discovery.objects.create(
            paper=thin, search=search, source="crossref", query="a")
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
