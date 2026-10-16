#!/usr/bin/env python3

'''
@File:   forms.py

@Brief:  Project, search and settings forms (local-first, no auth).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import re

# Custom library Imports
from django import forms

from literature.models import Paper, Project, Search, SurveyEntry
from literature.services.manual_paper_service import ORIGIN_SOURCES

# ------------------------------------------------------------------------ #
# Project / Search
# ------------------------------------------------------------------------ #

# --- Project form ---


class ProjectForm(forms.ModelForm):
    """
    Create a survey project with topic context.
    """

    class Meta:
        model = Project
        fields = ["name", "survey_topic",
                  "research_questions", "domain", "description"]
        labels = {
            "name": "Project name",
            "survey_topic": "Survey paper topic",
            "research_questions": "Research questions",
            "domain": "Domain focus",
            "description": "Notes",
        }
        help_texts = {
            "survey_topic": "e.g. Semantic navigation in autonomous robots.",
            "research_questions": "One per line. Shown again during reading/scoring.",
            "domain": "e.g. robotics, climate, medicine.",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Battery degradation survey"}),
            "survey_topic": forms.TextInput(attrs={"placeholder": "Survey topic"}),
            "research_questions": forms.Textarea(attrs={"rows": 3, "placeholder": "RQ1: ...\nRQ2: ..."}),
            "domain": forms.TextInput(attrs={"placeholder": "Domain"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }


# --- Curated paper details ---
class PaperDetailsForm(forms.Form):
    """
    Validate manually curated paper fields before queue admission.
    """

    title = forms.CharField(required=False, max_length=500)
    doi = forms.CharField(required=False, max_length=320, label="DOI")
    authors = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}),
                              help_text="One author name per line.")
    publication_year = forms.IntegerField(
        required=False, min_value=1500, label="Year")
    venue = forms.CharField(required=False, max_length=500)
    abstract = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 6}))
    references = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}),
                                 help_text="One DOI, OpenAlex work ID, or S2:paperId per line. Blank papers remain outside screening.")

    def clean_doi(self) -> str:
        """
        Normalize DOI at the form boundary; a DOI may be looked up later.
        """
        from literature.services.reference_service import normalize_doi

        raw = normalize_doi(self.cleaned_data["doi"]) or ""
        if raw and not re.fullmatch(r"10\.\d{4,9}/\S+", raw):
            raise forms.ValidationError("Enter a DOI such as 10.1000/example.")
        return raw

    def clean_references(self) -> list[str]:
        """
        Only persist identifiers the citation graph can resolve.
        """
        from literature.services.reference_service import normalize_doi

        result: list[str] = []
        for raw in self.cleaned_data["references"].splitlines():
            ref = raw.strip()
            if not ref:
                continue
            if (re.fullmatch(r"https://openalex\.org/W[0-9A-Za-z]+", ref)
                    or re.fullmatch(r"S2:[0-9A-Za-z-]+", ref)):
                result.append(ref)
            elif re.fullmatch(r"(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:)\d{4}\.\d{4,5}(?:v\d+)?(?:\.pdf)?", ref, re.IGNORECASE):
                from literature.services.reference_review_service import extract_reference_ids

                result.extend(extract_reference_ids(ref))
            elif (doi := normalize_doi(ref)) and re.fullmatch(r"10\.\d{4,9}/\S+", doi):
                result.append(f"https://doi.org/{doi}")
            else:
                raise forms.ValidationError(
                    f"Unrecognized reference identifier: {ref[:80]}")
        return list(dict.fromkeys(result))

    def clean(self) -> dict:
        cleaned = super().clean()
        if not cleaned.get("title") and not cleaned.get("doi"):
            raise forms.ValidationError(
                "Enter a title or DOI to add this paper.")
        return cleaned


class ManualPaperForm(PaperDetailsForm):
    """
    Add a missed paper and attribute it to an existing keyword row.
    """

    search_keyword = forms.ChoiceField(label="Assign to search keyword")
    origin_source = forms.ChoiceField(label="Where was this paper found?", choices=[
                                      ("", "Choose a source"), *ORIGIN_SOURCES])

    def __init__(self, *args, project: Project, **kwargs) -> None:
        """
        Offer manual assignments only from the current configuration.

        Args:
            *args: Positional Django form arguments, including submitted data.

        Kwargs:
            project: Required owning project for current keyword choices.
            **kwargs: Django form options such as initial values and prefix.

        Returns:
            None
        """
        super().__init__(*args, **kwargs)
        self.fields["search_keyword"].choices = [
            (f"{search.pk}|{keyword}", f"{search.name} — {keyword}")
            for search in project.searches.filter(is_current=True)
            for keyword in (search.keywords or [])
        ]
        self.order_fields([
            "search_keyword", "origin_source", "doi", "title", "authors",
            "publication_year", "venue", "abstract", "references"])


class StatusCorrectionForm(forms.Form):
    """
    Explicit correction outside the immutable screening flow.
    """

    stage = forms.ChoiceField(
        choices=[("Title", "Title"), ("Abstract", "Abstract")])
    decision = forms.ChoiceField(choices=[
        ("PENDING", "Pending / rescreen"), ("ACCEPT", "Yes"), ("REJECT", "No"),
    ])


class MissingAbstractForm(forms.Form):
    """
    Fill a missing abstract at the point where it is needed for screening.
    """

    paper_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    abstract = forms.CharField(
        max_length=50000, label="Paste abstract", widget=forms.Textarea(attrs={"rows": 8}))


class MissingMetadataForm(PaperDetailsForm):
    """
    Expose only unknown metadata, allowing partial completion rather than requiring it all.
    """

    paper_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    publication_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}))
    publisher = forms.CharField(required=False, max_length=300)
    keywords = forms.CharField(required=False, widget=forms.Textarea(
        attrs={"rows": 2}), help_text="One keyword per line. These are paper metadata, not a change to its linked search keyword.")
    citation_count = forms.IntegerField(
        required=False, min_value=0, label="Citation count")
    abstract = forms.CharField(required=False, max_length=50000,
                               label="Paste abstract", widget=forms.Textarea(attrs={"rows": 8}))
    reference_text = forms.CharField(required=False, max_length=200000, label="Paste references", widget=forms.Textarea(
        attrs={"rows": 8}), help_text="Paste the bibliography, including references without DOIs. Unresolved citations are retained.")

    def __init__(self, *args, paper, **kwargs):
        from literature.services.reference_review_service import missing_metadata_fields

        self.paper = paper
        super().__init__(*args, **kwargs)
        missing = missing_metadata_fields(paper)
        self.fields = {name: field for name, field in self.fields.items(
        ) if name == "paper_id" or name in missing}
        self.order_fields(["paper_id", "abstract", "title", "authors", "publication_year", "publication_date",
                          "venue", "publisher", "doi", "keywords", "citation_count", "reference_text"])

    def clean(self) -> dict:
        # Creation-form title/DOI requirements do not apply to a partial metadata update.
        cleaned = forms.Form.clean(self)
        values = [value for name, value in cleaned.items(
        ) if name != "paper_id" and value is not None and value != "" and value != []]
        if not values:
            if "abstract" in self.fields:
                self.add_error(
                    "abstract", "Enter an abstract or another missing detail before saving.")
            else:
                raise forms.ValidationError(
                    "Enter at least one missing detail before saving.")
        date = cleaned.get("publication_date")
        if date and date.year < 1500:
            self.add_error("publication_date",
                           "Enter a publication date from 1500 onwards.")
        return cleaned


class ReferenceCompletionForm(forms.Form):
    """
    Keep the complete pasted bibliography, even when entries lack a DOI.
    """

    paper_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reference_text = forms.CharField(max_length=200000, label="Paste references", widget=forms.Textarea(
        attrs={"rows": 12}), help_text="Paste the bibliography or one DOI, arXiv URL, OpenAlex URL or S2 ID per line. Unresolved citations are retained as text.")


class SurveyEntryForm(PaperDetailsForm):
    """
    Editable survey snapshot, including manuscript reference labels.
    """

    reference_number = forms.IntegerField(
        required=False, min_value=1, max_value=2147483647, label="Survey reference number [x]",
        help_text="Leave blank when adding; set it later under the graph to match your manuscript.",
    )
    publication_year = forms.IntegerField(
        required=False, min_value=1500, max_value=9999, label="Year"
    )
    origin_source = forms.ChoiceField(label="Where was this paper found?", choices=[
                                      ("", "Choose a source"), *ORIGIN_SOURCES])
    inclusion_origin = forms.ChoiceField(
        required=False,
        label="How did this entry reach the survey?",
        choices=SurveyEntry.InclusionOrigin.choices,
    )
    cited_entries = forms.ModelMultipleChoiceField(
        required=False,
        queryset=SurveyEntry.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        label="Links to (this entry cites)",
        help_text="Select as many entries from the table as this paper cites. Untick to remove a manual link; independently matched automatic links may remain.",
    )
    publisher = forms.CharField(required=False, max_length=300)
    version = forms.CharField(required=False, widget=forms.HiddenInput)
    preview_token = forms.CharField(required=False, widget=forms.HiddenInput)
    citation_count = forms.IntegerField(
        required=False, min_value=0, max_value=2147483647,
        widget=forms.HiddenInput,
    )
    reference_text = forms.CharField(required=False, max_length=200000,
                                     label="Bibliography / reference identifiers", widget=forms.Textarea(attrs={"rows": 6}))

    def __init__(self, *args, project: Project | None = None, entry: SurveyEntry | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("references", None)
        self.fields["title"].required = True
        if project is not None:
            qs = SurveyEntry.objects.filter(
                project=project).order_by("reference_number", "pk")
            if entry is not None:
                qs = qs.exclude(pk=entry.pk)
            self.fields["cited_entries"].queryset = qs
        if not self.is_bound:
            if entry is not None:
                if "cited_entries" not in (self.initial or {}):
                    self.initial["cited_entries"] = list(
                        entry.manual_outgoing.values_list(
                            "cited_id", flat=True)
                    )
                if "inclusion_origin" not in (self.initial or {}):
                    self.initial["inclusion_origin"] = entry.inclusion_origin
            elif "inclusion_origin" not in (self.initial or {}):
                self.initial["inclusion_origin"] = SurveyEntry.InclusionOrigin.READING
        self.order_fields(["reference_number", "doi", "title", "authors", "publication_year", "venue", "publisher",
                          "abstract", "reference_text", "origin_source", "inclusion_origin", "cited_entries", "version", "preview_token", "citation_count"])

    def clean_inclusion_origin(self) -> str:
        """
        Old POSTs omit this field; default to reading while keeping legacy unspecified editable.
        """
        value = self.cleaned_data.get("inclusion_origin")
        if not value:
            return SurveyEntry.InclusionOrigin.READING
        return value


class SurveyCitationForm(forms.Form):
    """
    Select a directed manual citation from this project's curated entries only.
    """

    citing = forms.ModelChoiceField(queryset=None, label="Citing entry")
    cited = forms.ModelChoiceField(
        queryset=None, label="References this entry")

    def __init__(self, *args, project: Project, **kwargs):
        super().__init__(*args, **kwargs)
        entries = project.survey_entries.all()
        self.fields["citing"].queryset = entries
        self.fields["cited"].queryset = entries

    def clean(self) -> dict:
        cleaned = super().clean()
        if cleaned.get("citing") and cleaned.get("citing") == cleaned.get("cited"):
            raise forms.ValidationError("An entry cannot reference itself.")
        return cleaned


class DoiSearchForm(forms.Form):
    """
    Search providers for one paper by DOI before adding it.
    """

    doi = forms.CharField(
        max_length=320, label="DOI",
        widget=forms.TextInput(attrs={"placeholder": "e.g. 10.1000/example"}),
        help_text="Paste a DOI or full https://doi.org/ link.",
    )

    def clean_doi(self) -> str:
        """
        Normalize DOI at the form boundary.
        """
        from literature.services.reference_service import normalize_doi

        raw = normalize_doi(self.cleaned_data["doi"]) or ""
        if raw and not re.fullmatch(r"10\.\d{4,9}/\S+", raw):
            raise forms.ValidationError("Enter a DOI such as 10.1000/example.")
        if not raw:
            raise forms.ValidationError("Enter a DOI to search.")
        return raw


class LookupPaperForm(forms.Form):
    """
    Validate explicit current assignments or keep an existing saved link.
    """

    search_keyword = forms.ChoiceField(label="Linked keyword")
    origin_source = forms.ChoiceField(label="Where was this paper found?", choices=[
                                      ("", "Choose a source"), *ORIGIN_SOURCES])
    new_keyword = forms.CharField(
        required=False, max_length=255, label="New keyword",
    )
    new_keyword_search = forms.ChoiceField(
        required=False, label="Search for the new keyword",
        help_text="The keyword will be available in this search after you save.",
    )
    title_decision = forms.ChoiceField(
        label="Title screening",
        choices=[("", "Choose a status"), ("PENDING", "Not screened yet"),
                 ("ACCEPT", "Pass"), ("REJECT", "Fail")],
    )
    abstract_decision = forms.ChoiceField(
        label="Abstract screening",
        choices=[("", "Choose a status"), ("NOT_QUEUED", "Not applicable — Title has not passed"),
                 ("PENDING", "Not screened yet"), ("ACCEPT", "Pass"), ("REJECT", "Fail")],
        help_text="Abstract screening is available only after Title passes.",
    )

    def __init__(
        self, *args, project: Project, needs_origin: bool = True,
        paper: Paper | None = None, **kwargs,
    ) -> None:
        """
        Offer current keywords and a server-derived unchanged saved link.

        Args:
            *args: Positional Django form arguments, including submitted data.

        Kwargs:
            project: Required owning project for selectable keywords.
            needs_origin: Require an origin choice. Defaults to True for a
                new discovery; False keeps the already recorded origin.
            paper: Existing canonical paper. Defaults to None, allowing no
                historical choice for a new entry.
            **kwargs: Django form options such as initial values and prefix.

        Returns:
            None
        """
        super().__init__(*args, **kwargs)
        if not needs_origin:
            self.fields.pop("origin_source")
        searches = list(project.searches.filter(is_current=True))
        self.fields["search_keyword"].choices = [
            ("", "Choose a keyword"),
            *[(search.name, [
                (f"{search.pk}|{keyword}", keyword)
                for keyword in (search.keywords or [])])
                for search in searches],
            ("NEW", "＋ Add new keyword…"),
        ]
        if paper is not None:
            from literature.services.paper_lookup_service import entry_initial

            preserved = entry_initial(project, paper).get("search_keyword", "")
            current_values = {f"{search.pk}|{word}" for search in searches
                              for word in search.keywords}
            if preserved and preserved not in current_values:
                self.fields["search_keyword"].choices.insert(1, (
                    preserved, "Saved linked keyword — keep unchanged: "
                    + preserved.split("|", 1)[-1],
                ))
        self.fields["new_keyword_search"].choices = [
            ("", "Choose a search"),
            *[(str(search.pk), search.name) for search in searches],
        ]
        # A single possible destination needs no second choice from the user.
        if len(searches) == 1:
            self.fields["new_keyword_search"].initial = str(searches[0].pk)

    def clean(self) -> dict:
        """
        Reject ambiguous keywords and impossible screening combinations.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: Validated choices with errors on their owning fields.
        """
        cleaned = super().clean()
        if cleaned.get("search_keyword") == "NEW":
            if not cleaned.get("new_keyword"):
                self.add_error("new_keyword", "Enter the new keyword.")
            if not cleaned.get("new_keyword_search"):
                self.add_error("new_keyword_search",
                               "Choose the search for this keyword.")
        else:
            # Hidden typed values must never override an existing choice.
            cleaned["new_keyword"] = ""
            cleaned["new_keyword_search"] = ""
        title = cleaned.get("title_decision")
        abstract = cleaned.get("abstract_decision")
        if title and abstract:
            if title != "ACCEPT" and abstract != "NOT_QUEUED":
                self.add_error("abstract_decision",
                               "Choose Not applicable until Title passes.")
            elif title == "ACCEPT" and abstract == "NOT_QUEUED":
                self.add_error(
                    "abstract_decision",
                    "Choose the Abstract status after passing Title.")
        return cleaned


# --- Search form ---
class SearchForm(forms.ModelForm):
    """
    Configure one search: title, keyword/venue/author rows, max year range.
    """

    keywords_json = forms.CharField(required=False, widget=forms.HiddenInput)
    venues_json = forms.CharField(required=False, widget=forms.HiddenInput)
    author_names_json = forms.CharField(
        required=False, widget=forms.HiddenInput)
    sources_selected = forms.MultipleChoiceField(
        required=False,
        label="Sources",
        choices=[
            ("openalex", "OpenAlex"),
            ("semantic_scholar", "Semantic Scholar"),
            ("crossref", "Crossref"),
            ("arxiv", "arXiv"),
            ("scopus", "Scopus (Elsevier)"),
        ],
        widget=forms.CheckboxSelectMultiple,
        help_text="Ticked sources are remembered for this search. Scopus is "
        "opt-in and needs an Elsevier API key and authorized API access. "
        "Only select it if your agreement permits local storage and export "
        "for this research; leaving all sources unticked uses the original "
        "four providers, not Scopus. Scopus uses loose phrases (wildcards "
        "remain active); use surnames/initials for its author matching.",
    )

    class Meta:
        model = Search
        fields = ["name", "year_min", "year_max", "sources_selected"]
        labels = {
            "name": "Search Title",
            "year_min": "From year",
            "year_max": "To year",
        }
        help_texts = {
            "name": "e.g. Primary Search.",
            "year_min": "Blank = earliest (max range).",
            "year_max": "Blank = latest (max range).",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Primary Search"}),
            "year_min": forms.NumberInput(attrs={"placeholder": "Blank = earliest"}),
            "year_max": forms.NumberInput(attrs={"placeholder": "Blank = latest"}),
        }

    # --- Parse one row list ---
    def _parse_rows(self, raw: str | None, label: str) -> list[str]:
        """
        Parse hidden JSON rows, strip/dedupe/sort A-Z at the boundary.
        """
        import json as _json

        if not raw:
            return []
        try:
            values = _json.loads(raw)
        except ValueError as exc:
            raise forms.ValidationError(f"{label} rows are invalid.") from exc
        if not isinstance(values, list):
            raise forms.ValidationError(f"{label} rows are invalid.")
        return Search.clean_rows(values)

    def clean(self) -> dict:
        """
        Validate future keyword rows and ordered optional year bounds.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: Sorted future lists and valid form fields.

        Raises:
            ValidationError: If rows are malformed or no keyword is supplied.
        """
        cleaned = super().clean()
        cleaned["keywords"] = self._parse_rows(
            self.data.get("keywords_json"), "Keyword"
        )
        cleaned["venues"] = self._parse_rows(
            self.data.get("venues_json"), "Journal")
        cleaned["author_names"] = self._parse_rows(
            self.data.get("author_names_json"), "Author"
        )
        if not cleaned["keywords"]:
            raise forms.ValidationError("Add at least one keyword row.")
        lower, upper = cleaned.get("year_min"), cleaned.get("year_max")
        if lower is not None and upper is not None and lower > upper:
            self.add_error("year_max", "To year must not precede From year.")
        return cleaned


# ------------------------------------------------------------------------ #
# Workspace New/Load (typed Linux paths; resolution lives in workspace.py)
# ------------------------------------------------------------------------ #

# --- Typed directory, stripped early; existence checks stay server-side ---
class WorkspaceRootForm(forms.Form):
    """
    Remember the workspace folder that lists recognised projects.
    """

    directory = forms.CharField(
        max_length=4096,
        label="Workspace folder",
        widget=forms.TextInput(attrs={
            "placeholder": "/home/you/surveys",
            "autocomplete": "off",
            "inputmode": "text",
        }),
    )

    def clean_directory(self) -> str:
        """
        Strip and expand ~; resolution/permission checks stay in workspace.py.
        """
        raw = (self.cleaned_data.get("directory") or "").strip()
        if not raw:
            raise forms.ValidationError("Enter a workspace folder path.")
        from pathlib import Path

        return str(Path(raw).expanduser())


# --- Shared New/Save Copy suggestion; never rename an existing store ---
def suggested_project_directory(
    parent: str, name: str, *, reserve_bytes: int = 0,
) -> str:
    """
    Suggest a safe child folder without creating files.

    Args:
        parent: Absolute existing parent folder; tilde expansion is allowed.
        name: Display name; retain Unicode and ampersands in the suggestion.

    Kwargs:
        reserve_bytes: Space for a staging suffix, defaulting to zero.

    Returns:
        str: Full resolved destination with whitespace replaced by underscores.

    Raises:
        forms.ValidationError: If the parent or derived member is unsafe.
    """
    import os
    from pathlib import Path

    try:
        base = Path(parent).expanduser()
        if not base.is_absolute() or not base.is_dir():
            raise forms.ValidationError(
                "Choose an absolute existing parent folder.")
        member = re.sub(r"\s+", "_", name.strip().lower())
        if (not member or member in {".", ".."}
                or any(char in member for char in ("/", "\\", "\0"))):
            raise forms.ValidationError(
                "The project name cannot form a safe folder; "
                "enter a destination override.")
        # Save Copy stages .<name>.recovery-<32 hex>; check its byte limit too.
        if (len(os.fsencode(member)) + reserve_bytes
                > os.pathconf(base, "PC_NAME_MAX")):
            raise forms.ValidationError(
                "The suggested folder name is too long; "
                "enter a shorter destination override.")
        return str(base.resolve() / member)
    except (OSError, RuntimeError, ValueError) as exc:
        raise forms.ValidationError(
            "Cannot resolve the chosen parent folder.") from exc


# --- New project: preview a name-derived child or explicitly override it ---
class WorkspaceNewForm(forms.Form):
    """
    Validate an editable New destination without overwriting packages.
    """

    name = forms.CharField(max_length=200, label="Project name")
    parent_directory = forms.CharField(
        required=False, max_length=4096, label="Parent workspace folder",
        help_text="Absolute parent path, e.g. /home/you/surveys; "
        "the project folder is a child.",
    )
    directory = forms.CharField(
        required=False, max_length=4096,
        label="New project folder (destination override)",
        help_text="Leave blank to use the project name beneath the parent.",
    )
    confirm = forms.BooleanField(
        required=False,
        label="I confirm the resolved New destination shown below",
    )
    preview_token = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean(self) -> dict:
        """
        Resolve the suggestion while preserving explicit typed paths.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: Cleaned fields with the destination in directory.

        Raises:
            forms.ValidationError: If no safe destination can be derived.
        """
        from pathlib import Path

        cleaned = super().clean()
        raw = cleaned.get("directory", "")
        if raw:
            try:
                cleaned["directory"] = str(Path(raw).expanduser())
            except (OSError, RuntimeError, ValueError) as exc:
                raise forms.ValidationError(
                    "Cannot expand the destination path.") from exc
        elif cleaned.get("name"):
            cleaned["directory"] = suggested_project_directory(
                cleaned.get("parent_directory", ""), cleaned["name"],
            )
        return cleaned


class ProjectNameForm(forms.ModelForm):
    """
    Edit only the display name through the owning worker.
    """

    class Meta:
        """
        Limit this explicit edit to the project display name.
        """

        model = Project
        fields = ("name",)


# --- Load an existing store at a typed location ---
class WorkspaceOpenForm(forms.Form):
    """
    Open an existing store; never creates a missing store.
    """

    directory = forms.CharField(
        max_length=4096,
        label="Existing project folder",
        widget=forms.TextInput(attrs={
            "placeholder": "/home/you/surveys/my-topic",
            "autocomplete": "off",
            "inputmode": "text",
        }),
    )

    def clean_directory(self) -> str:
        raw = (self.cleaned_data.get("directory") or "").strip()
        if not raw:
            raise forms.ValidationError(
                "Enter the folder path of the project to open.")
        from pathlib import Path

        return str(Path(raw).expanduser())


# ------------------------------------------------------------------------ #
# Settings (credentials stored in .env, never in DB)
# ------------------------------------------------------------------------ #

# --- Settings form ---
class SettingsForm(forms.Form):
    """
    Prompt for polite-pool email and optional implemented-provider tokens.
    """

    openalex_email = forms.EmailField(
        required=False,
        help_text="Required for non-Scopus searches; optional for Scopus-only "
        "search. Leave blank to keep the current value.",
    )
    semantic_scholar_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=True),
        help_text="Optional; used for Semantic Scholar requests. "
        "Stored locally only.",
    )
    opencitations_token = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Optional OpenCitations access token. Leave blank to keep "
        "the current value; restart after saving.",
    )
    elsevier_api_key = forms.RegexField(
        regex=r"\A[!-~]+\Z", required=False, max_length=4096, strip=False,
        label="Elsevier Scopus API key",
        widget=forms.PasswordInput(render_value=False),
        error_messages={
            "invalid": "Use printable ASCII without spaces or control "
            "characters.",
        },
        help_text='Optional. <a href="https://dev.elsevier.com/apikey/manage" '
        'target="_blank" rel="noopener noreferrer">Manage Elsevier API '
        'keys</a>. Leave blank to keep the current value; restart after '
        'saving. Use printable ASCII without spaces or control characters.',
    )
    elsevier_insttoken = forms.RegexField(
        regex=r"\A[!-~]+\Z", required=False, max_length=4096, strip=False,
        label="Elsevier institutional token",
        widget=forms.PasswordInput(render_value=False),
        error_messages={
            "invalid": "Use printable ASCII without spaces or control "
            "characters.",
        },
        help_text='Optional token issued by Elsevier for institutional '
        'access. <a href="https://dev.elsevier.com/support.html" '
        'target="_blank" rel="noopener noreferrer">Elsevier API support</a>. '
        'Leave blank to keep the current value; restart after saving. '
        'Use printable ASCII without spaces or control characters.',
    )

    def clean(self) -> dict:
        """
        Allow Scopus credentials to be configured without an OpenAlex email.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: Validated settings; a new or configured Elsevier API key
            permits an omitted email. Other settings retain the email rule.
        """
        from django.conf import settings

        cleaned = super().clean()
        if "openalex_email" not in self.errors and not cleaned.get(
            "openalex_email"
        ) and not (
            cleaned.get("elsevier_api_key")
            or getattr(settings, "ELSEVIER_API_KEY", "")
        ):
            self.add_error("openalex_email", "This field is required.")
        return cleaned
