"""
Curated survey snapshots and confirmed citations, independent of screening decisions.

@File: survey_graph_service.py
@Brief: Editable survey-writing graph without mutating draft library records.
@Date: 01/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import re

# Custom library Imports
from django.core import signing
from django.db import transaction

from literature.models import (
    Discovery,
    OpenCitationsLookup,
    Paper,
    Project,
    SurveyCitation,
    SurveyEntry,
)
from literature.services.reference_review_service import extract_reference_ids
from literature.services.reference_service import normalize_doi

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Functions


def citation_key(value: str) -> str:
    """
    Normalize only supported identity formats, not citation titles or prose.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("https://openalex.org/"):
        candidate = raw.rstrip("/").rsplit("/", 1)[-1]
        return candidate if re.fullmatch(r"W\d+", candidate) else ""
    if re.fullmatch(r"S2:[0-9A-Za-z-]+", raw):
        return raw
    if re.fullmatch(r"W\d+", raw):
        return raw
    doi = normalize_doi(raw)
    if doi and re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        return doi
    return ""


def _authors_text(raw) -> str:
    """
    Accept string or list of dict/str without raising on provider shapes.
    """
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return ""
    parts = []
    for item in raw or []:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
        else:
            name = str(item).strip()
        if name:
            parts.append(name)
    return "\n".join(parts)


def project_origin(project: Project, paper: Paper | None) -> tuple[str, str]:
    """
    Derive (inclusion_origin, origin_source) from this project's discoveries only.

    Original when any matching discovery raw_payload.entry_method != user_added
    (ordinary provider history counts as original); otherwise reading.
    Global papers without a project discovery are reading. Never guesses a
    legacy origin; unknown sources stay blank for the curator to choose.
    """
    from literature.services.manual_paper_service import ORIGIN_SOURCES

    valid = set(dict(ORIGIN_SOURCES))
    if paper is None:
        return ("reading", "")
    discoveries = list(
        Discovery.objects.filter(
            paper=paper, search__project=project).order_by("pk")
    )
    if not discoveries:
        return ("reading", "")
    originals = [
        d
        for d in discoveries
        if not isinstance(d.raw_payload, dict) or d.raw_payload.get("entry_method") != "user_added"
    ]
    is_original = bool(originals)
    origin = "search" if is_original else "reading"
    if is_original:
        candidates = [d for d in originals if d.source in valid]
    else:
        candidates = [d for d in discoveries if d.source in valid]
    return (origin, candidates[0].source if candidates else "")


def prefill(
    project: Project, doi: str, metadata: dict, paper: Paper | None, *,
    binding: list[str] | None = None,
) -> dict:
    """
    Sign the cached identities; all visible bibliographic fields remain editable.

    Never mutates Paper/Discovery/keyword/decisions. Reference numbers stay
    blank so curators finalize them once manuscript IDs are known. The signed
    preview retains the optional paper link even when the DOI is blank.

    Args:
        project: Owning project for provenance and preview identity.
        doi: Validated DOI, or blank for a stored paper without one.
        metadata: Fetched metadata used only when paper is absent.
        paper: Cached canonical paper, or None for an uncached DOI.

    Kwargs:
        binding: Worker UUID/epoch. Defaults to empty developer identity.

    Returns:
        dict: Editable form initials with a signed, 30-minute identity token.
    """
    aliases = []
    if paper:
        metadata = {
            "title": paper.title,
            "authors": paper.authors,
            "publication_year": paper.publication_year,
            "venue": paper.venue,
            "publisher": paper.publisher,
            "abstract": paper.abstract,
            "references": paper.references,
            "reference_text": paper.reference_text,
            "openalex_id": paper.openalex_id,
            "source_ids": paper.source_ids,
        }
    if not isinstance(metadata, dict):
        metadata = {}
    ids = metadata.get("source_ids") or {}
    if not isinstance(ids, dict):
        ids = {}
    openalex_id = metadata.get("openalex_id") or ids.get("openalex")
    if openalex_id:
        key = citation_key(str(openalex_id))
        if key:
            aliases.append(key)
    if ids.get("semantic_scholar"):
        key = citation_key("S2:" + str(ids["semantic_scholar"]))
        if key:
            aliases.append(key)
    arxiv = re.fullmatch(r"(\d{4}\.\d{4,5})(?:v\d+)?",
                         str(ids.get("arxiv") or ""))
    if arxiv:
        aliases.append("10.48550/arxiv." + arxiv.group(1))
    references = metadata.get("references") or []
    if not isinstance(references, list):
        references = []
    inclusion_origin, origin_source = project_origin(project, paper)
    return {
        "doi": doi,
        "title": metadata.get("title", ""),
        "authors": _authors_text(metadata.get("authors", [])),
        "publication_year": metadata.get("publication_year"),
        "venue": metadata.get("venue", ""),
        "publisher": metadata.get("publisher", ""),
        "abstract": metadata.get("abstract", ""),
        "reference_text": metadata.get("reference_text") or "\n".join(str(r) for r in references),
        "inclusion_origin": inclusion_origin,
        "origin_source": origin_source,
        "preview_token": signing.dumps(
            {
                "project": project.pk,
                "binding": binding or ["", ""],
                "doi": doi,
                "aliases": aliases,
                "paper": paper.pk if paper else None,
            },
            salt="survey-prefill",
            compress=True,
        ),
    }


@transaction.atomic
def save_survey_entry(
    project: Project, values: dict, entry_id: int | None = None, *,
    binding: list[str] | None = None,
) -> SurveyEntry:
    """
    Save a curated snapshot and manual links atomically without draft writes.

    Args:
        project: Owning project for the entry and cited targets.
        values: Validated editable bibliographic fields and optional links.

    Kwargs:
        entry_id: Existing entry to edit. Defaults to None for creation.
        binding: Worker UUID/epoch. Defaults to empty developer identity.

    Returns:
        SurveyEntry: Saved independent snapshot.

    Raises:
        ValueError: If fields, preview binding, version or links are invalid.
        DatabaseError: If the transaction cannot commit; no partial saves.
    """
    entry = (
        SurveyEntry.objects.select_for_update().get(project=project, pk=entry_id)
        if entry_id
        else SurveyEntry(project=project)
    )
    try:
        reference_number = int(values.get("reference_number"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Enter a survey reference number of 1 or higher.") from exc
    if reference_number < 1:
        raise ValueError("Enter a survey reference number of 1 or higher.")
    title = str(values.get("title") or "").strip()
    if not title:
        raise ValueError("Enter a title for this survey entry.")
    doi = normalize_doi(str(values.get("doi") or "")) or ""
    if doi and not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        raise ValueError("Enter a valid DOI or leave it blank.")
    if entry_id:
        try:
            version = signing.loads(values.get(
                "version", ""), salt="survey-version", max_age=86400)
        except (signing.BadSignature, TypeError, ValueError) as exc:
            raise ValueError(
                "This edit expired. Reload the entry before saving.") from exc
        if version != [entry.pk, entry.updated_at.isoformat()]:
            raise ValueError(
                "This entry changed in another tab. Reload before saving.")
    if (
        project.survey_entries.filter(reference_number=reference_number)
        .exclude(pk=entry_id)
        .exists()
    ):
        raise ValueError(
            "That survey reference number is already used in this project.")
    old_doi = normalize_doi(entry.doi or "") or ""
    if doi and project.survey_entries.filter(doi__iexact=doi).exclude(pk=entry_id).exists():
        raise ValueError(
            "This DOI already has a curated entry in this project. Edit that entry instead."
        )
    if entry_id and doi != old_doi:
        entry.identity_aliases = []
        entry.paper = None
    token = values.get("preview_token")
    if token and not entry_id:
        try:
            preview = signing.loads(token, salt="survey-prefill", max_age=1800)
        except (signing.BadSignature, TypeError, ValueError) as exc:
            raise ValueError(
                "The DOI preview expired. Look it up again or use a blank manual form."
            ) from exc
        if not isinstance(preview, dict):
            raise ValueError(
                "The DOI preview expired. Look it up again or use a blank manual form."
            )
        if preview.get("project") != project.pk:
            raise ValueError("The preview belongs to another project.")
        if preview.get("binding", ["", ""]) != (binding or ["", ""]):
            raise ValueError("The preview belongs to another worker. Reload.")
        if (
            not isinstance(preview.get("doi"), str)
            or "aliases" not in preview
            or "paper" not in preview
        ):
            raise ValueError(
                "The DOI preview expired. Look it up again or use a blank manual form."
            )
        if doi == preview.get("doi"):
            aliases = preview.get("aliases")
            paper_pk = preview.get("paper")
            if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
                raise ValueError(
                    "The DOI preview expired. Look it up again or use a blank manual form."
                )
            if any(not citation_key(a) for a in aliases):
                raise ValueError(
                    "The DOI preview expired. Look it up again or use a blank manual form."
                )
            if paper_pk is not None and not isinstance(paper_pk, int):
                raise ValueError(
                    "The DOI preview expired. Look it up again or use a blank manual form."
                )
            entry.identity_aliases = list(aliases)
            entry.paper = Paper.objects.filter(
                pk=paper_pk).first() if paper_pk else None
    if "inclusion_origin" in values:
        raw_origin = values.get("inclusion_origin") or "reading"
        if raw_origin not in ("search", "reading", "unspecified"):
            raise ValueError("Choose where this entry came from.")
        entry.inclusion_origin = raw_origin
    entry.reference_number = reference_number
    entry.title = title
    entry.doi = doi
    for name in (
        "authors",
        "publication_year",
        "venue",
        "publisher",
        "abstract",
        "reference_text",
        "origin_source",
    ):
        setattr(entry, name, values.get(name) or (
            None if name == "publication_year" else ""))
    entry.reference_ids = extract_reference_ids(entry.reference_text)
    entry.save()
    if "cited_entries" in values:
        raw_targets = values.get("cited_entries")
        if isinstance(raw_targets, (str, bytes)):
            raise ValueError("Choose valid entries from this project.")
        try:
            target_ids = {int(getattr(item, "pk", item))
                          for item in (raw_targets or [])}
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Choose valid entries from this project.") from exc
        if entry.pk in target_ids:
            raise ValueError("An entry cannot reference itself.")
        if target_ids:
            found = set(
                SurveyEntry.objects.filter(project=project, pk__in=target_ids).values_list(
                    "pk", flat=True
                )
            )
            if target_ids != found:
                raise ValueError("Choose entries from this project.")
        existing = set(entry.manual_outgoing.values_list(
            "cited_id", flat=True))
        to_remove = existing - target_ids
        to_add = target_ids - existing
        if to_remove:
            SurveyCitation.objects.filter(
                citing=entry, cited_id__in=sorted(to_remove)).delete()
        for tid in sorted(to_add):
            target = SurveyEntry.objects.filter(
                project=project, pk=tid).first()
            if not target or tid == entry.pk:
                raise ValueError("Choose entries from this project.")
            SurveyCitation.objects.get_or_create(citing=entry, cited=target)
    return entry


def build_survey_graph(project: Project) -> dict:
    """
    Render chosen entries only; union identifier-matched, cached and manually confirmed edges.
    """
    entries = list(project.survey_entries.select_related("paper"))
    by_key = {}
    for entry in entries:
        for alias in [entry.doi, *(entry.identity_aliases or [])]:
            if not isinstance(alias, str):
                continue
            key = citation_key(alias)
            if key:
                # Ambiguous cached identities cannot confirm which snapshot was cited.
                by_key[key] = entry.pk if key not in by_key or by_key[key] == entry.pk else None
    automatic = set()
    for entry in entries:
        for identifier in entry.reference_ids or []:
            target = by_key.get(citation_key(identifier))
            if target and target != entry.pk:
                automatic.add((entry.pk, target))
    doi_keys = {key for key in by_key if key.startswith("10.")}
    if doi_keys:
        lookups = OpenCitationsLookup.objects.filter(doi__in=sorted(doi_keys))
    else:
        lookups = OpenCitationsLookup.objects.none()
    for lookup in lookups:
        for pair in lookup.edges or []:
            if isinstance(pair, list) and len(pair) == 2:
                source, target = (by_key.get(citation_key(value))
                                  for value in pair)
                if source and target and source != target:
                    automatic.add((source, target))
    manual = set(
        SurveyCitation.objects.filter(citing__project=project, cited__project=project).values_list(
            "citing_id", "cited_id"
        )
    )
    return {
        "title": "Curated survey graph",
        "curated": True,
        "nodes": [
            {
                "id": entry.pk,
                "title": entry.title,
                "year": entry.publication_year,
                "citations": 0,
                "venue": entry.venue,
                "is_seed": True,
                "is_joiner": False,
                "reference_number": entry.reference_number,
            }
            for entry in entries
        ],
        "edges": sorted(automatic | manual),
        "automatic_edges": automatic,
        "manual_edges": manual,
    }
