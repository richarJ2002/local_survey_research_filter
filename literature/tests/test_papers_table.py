"""Fixed-width paper-table sorting and page navigation.

@File: test_papers_table.py
@Brief: Global ordering, missing metadata, project-scoped statuses and stable pagination.
@Date: 02/10/2026
"""

from urllib.parse import parse_qs

from django.test import TestCase
from django.urls import reverse

from literature.models import (
    Discovery,
    Paper,
    PaperKeywordAssignment,
    Project,
    ScreeningDecision,
    Search,
)


# --- Project table boundaries ---
class PapersTableTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Table tests")
        self.project.ensure_default_stages()
        self.search = Search.objects.create(
            project=self.project, name="Search", keywords=["battery"]
        )
        self.url = reverse("papers_view", args=[self.project.pk])

    def paper(self, title, **values):
        paper = Paper.objects.create(title=title, **values)
        Discovery.objects.create(paper=paper, search=self.search, source="openalex")
        return paper

    def decide(self, paper, stage, value):
        ScreeningDecision.objects.create(
            paper=paper, stage=self.project.stages.get(name=stage), decision=value
        )

    def ids(self, response):
        self.assertEqual(response.status_code, 200)
        return [row["paper"].pk for row in response.context["rows"]]

    def test_metadata_sorts_both_directions_with_missing_values_first(self):
        missing = self.paper("", sources=[], keywords=[])
        beta = self.paper(
            "beta", doi="10.1000/b", publication_year=2022, sources=["Zulu"], keywords=["alpha"]
        )
        alpha = self.paper(
            "Alpha", doi="10.1000/a", publication_year=2020, sources=["alpha"], keywords=["Beta"]
        )
        PaperKeywordAssignment.objects.create(
            project=self.project, paper=beta, search=self.search, keyword="Zulu"
        )
        for key in ("title", "doi", "year", "source", "keywords"):
            with self.subTest(sort=key):
                self.assertEqual(
                    self.ids(self.client.get(self.url, {"sort": key, "direction": "asc"})),
                    [missing.pk, alpha.pk, beta.pk],
                )
                self.assertEqual(
                    self.ids(self.client.get(self.url, {"sort": key, "direction": "desc"})),
                    [beta.pk, alpha.pk, missing.pk],
                )

    def test_title_status_uses_visible_labels_and_current_project(self):
        unqueued = self.paper("Unqueued")
        missing = self.paper("   ")
        yes = self.paper("Yes")
        no = self.paper("No")
        pending = self.paper("Pending")
        for paper, value in ((yes, "ACCEPT"), (no, "REJECT"), (pending, "PENDING")):
            self.decide(paper, "Title", value)
        other = Project.objects.create(name="Other")
        other.ensure_default_stages()
        ScreeningDecision.objects.create(
            paper=unqueued, stage=other.stages.get(name="Title"), decision="ACCEPT"
        )
        foreign_search = Search.objects.create(project=other, name="Foreign")
        foreign = Paper.objects.create(title="Foreign")
        Discovery.objects.create(paper=foreign, search=foreign_search, source="crossref")
        expected = [missing.pk, no.pk, unqueued.pk, pending.pk, yes.pk]
        for direction, ids in (("asc", expected), ("desc", expected[::-1])):
            response = self.client.get(self.url, {"sort": "title_status", "direction": direction})
            self.assertEqual(self.ids(response), ids)
            self.assertNotContains(response, "Foreign")
            self.assertContains(response, "Untitled paper")

    def test_legacy_manual_is_blank_and_sorts_missing_without_mutating_sources(self):
        legacy = self.paper("Unknown origin", sources=["manual"])
        supplemented = self.paper("Known origin", sources=["manual", "arxiv"])
        provider = self.paper("Provider origin", sources=["crossref", "manual"])
        before = list(Paper.objects.values())
        response = self.client.get(self.url, {"sort": "source", "direction": "asc"})
        self.assertEqual(self.ids(response), [legacy.pk, supplemented.pk, provider.pk])
        self.assertEqual(
            [row["display_sources"] for row in response.context["rows"]],
            [[], ["arxiv"], ["crossref"]],
        )
        self.assertContains(response, '<td><br><span class="meta">—</span></td>', html=True)
        self.assertNotContains(response, "manual<br>")
        descending = self.client.get(self.url, {"sort": "source", "direction": "desc"})
        self.assertEqual(self.ids(descending), [provider.pk, supplemented.pk, legacy.pk])
        self.assertEqual(list(Paper.objects.values()), before)

    def test_multivalued_cells_sort_by_first_value_with_stable_ties(self):
        missing = self.paper("Missing", sources=[""], keywords=["   "])
        first = self.paper("Alpha set", sources=["Crossref", "Zulu"], keywords=["Battery", "Zinc"])
        second = self.paper("Beta set", sources=["crossref"], keywords=["battery"])
        last = self.paper("Other set", sources=["Openalex"], keywords=["Lidar"])
        for key in ("source", "keywords"):
            self.assertEqual(
                self.ids(self.client.get(self.url, {"sort": key, "direction": "asc"})),
                [missing.pk, first.pk, second.pk, last.pk],
            )
            self.assertEqual(
                self.ids(self.client.get(self.url, {"sort": key, "direction": "desc"})),
                [last.pk, first.pk, second.pk, missing.pk],
            )

    def test_abstract_status_distinguishes_final_reference_pending_and_missing_abstract(self):
        blank = self.paper("Blank")
        final = self.paper("Final", reference_text="Bibliography")
        rejected = self.paper("Rejected")
        refs_pending = self.paper("References pending")
        pending = self.paper("Pending", abstract="Existing abstract")
        abstract_missing = self.paper("Abstract missing")
        for paper, value in (
            (final, "ACCEPT"),
            (rejected, "REJECT"),
            (refs_pending, "ACCEPT"),
            (pending, "PENDING"),
            (abstract_missing, "PENDING"),
        ):
            self.decide(paper, "Abstract", value)
        expected = [
            blank.pk,
            final.pk,
            rejected.pk,
            refs_pending.pk,
            pending.pk,
            abstract_missing.pk,
        ]
        for direction, ids in (("asc", expected), ("desc", expected[::-1])):
            response = self.client.get(
                self.url, {"sort": "abstract_status", "direction": direction}
            )
            self.assertEqual(self.ids(response), ids)
            self.assertContains(response, "Passed content; references pending")
            self.assertContains(response, "Final selected")
            self.assertContains(response, "abstract needed")

    def test_global_sort_search_and_page_jump(self):
        query = "Target & matches"
        papers = [self.paper(f"{query} {index:02}") for index in range(59, -1, -1)]
        self.paper("Does not match")
        filters = {"q": query, "sort": "title", "direction": "asc"}
        expected = [paper.pk for paper in papers[::-1]]
        for number in (1, 2, 3):
            response = self.client.get(self.url, {**filters, "page": number})
            self.assertEqual(self.ids(response), expected[(number - 1) * 25 : number * 25])
            self.assertEqual(response.context["page"].paginator.count, 60)
            self.assertContains(response, 'name="q" value="Target &amp; matches"')
            self.assertContains(response, 'id="paper-page-number"')
            self.assertContains(response, 'max="3"')
            params = parse_qs(response.context["page_query"])
            self.assertEqual(params, {key: [value] for key, value in filters.items()})
        for bad_page, expected_number in (("nonsense", 1), (999, 3)):
            response = self.client.get(self.url, {**filters, "page": bad_page})
            self.assertEqual(response.context["page"].number, expected_number)

    def test_sort_links_toggle_reset_page_and_preserve_query(self):
        self.paper("Find & sort")
        response = self.client.get(
            self.url, {"q": "Find &", "sort": "title", "direction": "asc", "page": 99}
        )
        headers = response.context["headers"]
        self.assertEqual(headers[0]["aria_sort"], "ascending")
        links = [link for header in headers for link in header["links"]]
        self.assertEqual(len(links), 7)
        for link in links:
            params = parse_qs(link["url"])
            self.assertEqual(params["q"], ["Find &"])
            self.assertNotIn("page", params)
            self.assertEqual(params["direction"], ["desc" if link["active"] else "asc"])
        self.assertContains(response, 'aria-sort="ascending"')
        self.assertContains(response, "Sort Paper descending")
        self.assertContains(response, 'type="hidden" name="sort" value="title"')

    def test_invalid_ordering_and_empty_single_page_are_safe(self):
        for filters in (
            {},
            {"sort": "__class__", "direction": "DROP TABLE"},
            {"sort": "year", "direction": "bad"},
        ):
            response = self.client.get(self.url, filters)
            self.assertEqual(self.ids(response), [])
            self.assertEqual(
                (response.context["sort"], response.context["direction"]), ("year", "desc")
            )
            self.assertContains(response, "Jump to page")
            self.assertContains(response, 'max="1"')
            self.assertContains(response, "table-layout:fixed")
            self.assertContains(response, 'class="papers-table"')
            self.assertContains(response, '<col style="width:24rem">', html=True)
            self.assertContains(response, 'aria-label="Project papers table"')

    def test_equal_keys_have_stable_pages_and_sorting_does_not_mutate_data(self):
        papers = [self.paper("Same title", publication_year=2020) for _ in range(27)]
        models = (Paper, Discovery, ScreeningDecision, PaperKeywordAssignment)
        before = [list(model.objects.values()) for model in models]
        for direction in ("asc", "desc"):
            first = self.client.get(self.url, {"sort": "year", "direction": direction})
            second = self.client.get(self.url, {"sort": "year", "direction": direction, "page": 2})
            self.assertEqual(self.ids(first) + self.ids(second), [paper.pk for paper in papers])
        self.assertEqual([list(model.objects.values()) for model in models], before)
