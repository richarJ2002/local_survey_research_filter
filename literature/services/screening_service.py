#!/usr/bin/env python3

"""Apply immutable screening actions and explicit audited status corrections.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   screening_service.py

@Brief:  Apply immutable screening actions and explicit audited status corrections.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from literature.models import Paper, Project, ScreeningDecision, ScreeningRevision, Stage

# ------------------------------------------------------------------------ #
# Decide / advance
# ------------------------------------------------------------------------ #


class InvalidTransition(ValueError):
    """Report invalid screening prerequisites, status changes or completion."""


# --- Functions
def title_ready(paper: Paper) -> bool:
    """Check the sole metadata prerequisite for Title screening.

    Args:
        paper (Paper): Paper whose title is inspected.

    Kwargs:
        None

    Returns:
        bool: True when title contains non-whitespace text. Other metadata is
            deliberately completed at later stages and does not block Title.
    """
    return bool((paper.title or "").strip())


def abstract_available(value: str | None) -> bool:
    """Distinguish an actual abstract from the confirmed HAL audience-only tag.

    Args:
        value: Candidate abstract string or None.

    Kwargs:
        None

    Returns:
        bool: True for nonblank strings except the stripped, case-insensitive
            exact tag "International audience"; no length heuristic is used.
    """
    # ponytail: reject only the observed exact tag; short real abstracts remain valid.
    return isinstance(value, str) and bool(value.strip()) and value.strip().casefold() != "international audience"


def has_reference_list(paper: Paper) -> bool:
    """Recognize provider identifiers or a manually supplied bibliography.

    Args:
        paper (Paper): Paper exposing references and reference_text.

    Kwargs:
        None

    Returns:
        bool: True if references is truthy or reference_text contains nonblank
            text. This does not validate identifiers or invent citation edges.
    """
    return bool(paper.references or (paper.reference_text or "").strip())


def _reference_filter() -> Q:
    """Build the decision-query predicate for completed references.

    Args:
        None

    Kwargs:
        None

    Returns:
        Q: Match paper.references unequal to [] or a non-whitespace
            character in paper.reference_text. Bibliography text qualifies for
            completion without implying any resolvable citation graph edges.
    """
    return ~Q(paper__references=[]) | Q(paper__reference_text__regex=r"\S")


def final_decisions(project: Project) -> QuerySet[ScreeningDecision]:
    """Select Abstract accepts with completed references and project discovery.

    Args:
        project (Project): Project bounding decisions and discovery membership.

    Kwargs:
        None

    Returns:
        QuerySet[ScreeningDecision]: Lazy, distinct Abstract ACCEPT rows with
            reference IDs or nonblank bibliography text, discovered in project.
            No Title/found fallback or new abstract-content validation occurs.
    """
    return ScreeningDecision.objects.filter(stage__project=project, stage__name="Abstract", decision="ACCEPT", paper__discoveries__search__project=project).filter(_reference_filter()).distinct()


def pending_references(project: Project) -> QuerySet[ScreeningDecision]:
    """Select discovered Abstract accepts still awaiting reference completion.

    Args:
        project (Project): Project bounding decisions and discovery membership.

    Kwargs:
        None

    Returns:
        QuerySet[ScreeningDecision]: Lazy, distinct Abstract ACCEPT rows not
            matching completed references; these are not final selections.
    """
    return ScreeningDecision.objects.filter(stage__project=project, stage__name="Abstract", decision="ACCEPT", paper__discoveries__search__project=project).exclude(_reference_filter()).distinct()


def _validate_status(paper: Paper, stage: Stage, decision: str) -> None:
    """Enforce project membership and prerequisites for decision changes.

    Args:
        paper (Paper): Paper that must have project discovery and a title.
        stage (Stage): Title or Abstract stage owning the project boundary.
        decision (str): Proposed normalized status; Abstract ACCEPT also
            requires a usable abstract. This helper does not validate the enum.

    Kwargs:
        None

    Returns:
        None

    Raises:
        InvalidTransition: If stage is unsupported, project discovery/title is
            missing, Abstract lacks Title acceptance, or acceptance lacks text.

    Side Effects:
        Read discoveries and decisions without writing.
    """
    if stage.name not in {"Title", "Abstract"}:
        raise InvalidTransition("Choose Title or Abstract screening.")
    if not paper.discoveries.filter(search__project=stage.project).exists():
        raise InvalidTransition("Paper does not belong to this project.")
    if not title_ready(paper):
        raise InvalidTransition("A title is required before Title screening.")
    if stage.name == "Abstract":
        if not paper.decisions.filter(stage__project=stage.project, stage__name="Title", decision="ACCEPT").exists():
            raise InvalidTransition(
                "Accept the title before changing the abstract status.")
        if decision == "ACCEPT" and not abstract_available(paper.abstract):
            raise InvalidTransition(
                "No abstract found. Enter the abstract before accepting this paper.")


@transaction.atomic
def decide(
    *,
    paper: Paper,
    stage: Stage,
    decision: str,
    notes: str = "",
    reason: str = "",
) -> ScreeningDecision:
    """Apply Yes/No decision and create next-stage PENDING row if accepted.

    Rules (MVP, Yes/No only):
    - Current row must be PENDING, else raise InvalidTransition.
    - decision must be ACCEPT or REJECT.
    - On ACCEPT in a non-final stage, ensure a PENDING row exists in the
      next stage (order+1) so Round 2 picks it up.
    - On REJECT, the paper stops (no next row).

    Args:
        None

    Kwargs:
        paper (Paper): Required keyword-only project-discovered paper.
        stage (Stage): Required keyword-only Title or Abstract stage.
        decision (str): Required keyword-only ACCEPT or REJECT, ignoring case.
        notes (str): Notes to store on the decided row. Defaults to "".
        reason (str): Reason to store on the decided row. Defaults to "".

    Returns:
        ScreeningDecision: The current row instance containing the applied
            status, notes, reason and decision/update timestamps.

    Raises:
        InvalidTransition: If status/prerequisites/model validation fail, the
            row is already decided, or a concurrent conditional update loses.

    Side Effects:
        In one transaction, create the current row if absent and conditionally
        update only PENDING status. On ACCEPT in a non-final stage, ensure a
        next-order stage row exists if that stage is configured; on REJECT,
        do not create a successor. Ordinary decisions do not create revisions.
    """
    normalized = (decision or "").upper()
    if normalized not in {
        ScreeningDecision.Decision.ACCEPT,
        ScreeningDecision.Decision.REJECT,
    }:
        raise InvalidTransition(
            f"Decision must be ACCEPT or REJECT, got {decision!r}")

    _validate_status(paper, stage, normalized)
    current, _ = ScreeningDecision.objects.get_or_create(
        paper=paper, stage=stage, defaults={
            "decision": ScreeningDecision.Decision.PENDING}
    )
    if current.decision != ScreeningDecision.Decision.PENDING:
        raise InvalidTransition(f"Already decided: {current.decision}")

    current.decision = normalized
    current.notes = notes
    current.reason = reason
    current.decided_at = timezone.now()
    try:
        current.full_clean(exclude=["paper", "stage"])
    except ValidationError as exc:
        raise InvalidTransition(str(exc)) from exc
    # SQLite does not implement row locks; a conditional write also protects immutability.
    current.updated_at = timezone.now()
    updated = ScreeningDecision.objects.filter(pk=current.pk, decision="PENDING").update(
        decision=current.decision, notes=current.notes, reason=current.reason,
        decided_at=current.decided_at, updated_at=current.updated_at,
    )
    if not updated:
        raise InvalidTransition(
            "This paper was already decided in another request.")

    if normalized == ScreeningDecision.Decision.ACCEPT and not stage.is_final:
        next_stage = (
            Stage.objects.filter(project=stage.project,
                                 order=stage.order + 1).first()
        )
        if next_stage is not None:
            ScreeningDecision.objects.get_or_create(
                paper=paper,
                stage=next_stage,
                defaults={"decision": ScreeningDecision.Decision.PENDING},
            )
    return current


@transaction.atomic
def correct_decision(
    *, paper: Paper, stage: Stage, decision: str, reason: str,
) -> ScreeningDecision:
    """Explicitly correct a status while retaining an audit trail.

    The normal screening action remains immutable. Correcting Title away
    from ACCEPT invalidates any Abstract decision, which is itself logged.

    Args:
        None

    Kwargs:
        paper (Paper): Required keyword-only project-discovered paper.
        stage (Stage): Required keyword-only Title or Abstract stage.
        decision (str): Required keyword-only PENDING, ACCEPT or REJECT,
            case-insensitive; must differ from the current status.
        reason (str): Required keyword-only nonblank correction explanation,
            stripped and limited to 255 characters.

    Returns:
        ScreeningDecision: Saved current row, with corrected status/reason and
            decided_at cleared for PENDING or set to now for a decided status.

    Raises:
        InvalidTransition: If stage/status/reason/prerequisites are invalid or
            the proposed status is unchanged.
        ValidationError: If model validation rejects the corrected row.
        Stage.DoesNotExist: If a Title correction has no Abstract stage.

    Side Effects:
        Save current status and a ScreeningRevision in one transaction,
        retaining notes. Title ACCEPT ensures an Abstract row; any other Title
        status logs an existing Abstract row as REMOVED and deletes it.
    """
    new_status = (decision or "").upper()
    if stage.name not in {"Title", "Abstract"} or new_status not in {
        ScreeningDecision.Decision.PENDING,
        ScreeningDecision.Decision.ACCEPT,
        ScreeningDecision.Decision.REJECT,
    }:
        raise InvalidTransition(
            "Choose a Title or Abstract status: Pending, Yes or No.")
    reason = (reason or "").strip()
    if not reason or len(reason) > 255:
        raise InvalidTransition(
            "Explain this correction (up to 255 characters).")
    _validate_status(paper, stage, new_status)

    current, _ = ScreeningDecision.objects.select_for_update().get_or_create(
        paper=paper, stage=stage,
        defaults={"decision": ScreeningDecision.Decision.PENDING},
    )
    previous = current.decision
    if previous == new_status:
        raise InvalidTransition("This paper already has that status.")
    current.decision = new_status
    current.reason = reason
    current.decided_at = None if new_status == ScreeningDecision.Decision.PENDING else timezone.now()
    current.full_clean(exclude=["paper", "stage"])
    current.save()
    ScreeningRevision.objects.create(
        paper=paper, stage=stage, previous_decision=previous,
        new_decision=new_status, reason=reason,
    )

    if stage.name == "Title":
        abstract_stage = Stage.objects.get(
            project=stage.project, name="Abstract")
        if new_status == ScreeningDecision.Decision.ACCEPT:
            ScreeningDecision.objects.get_or_create(
                paper=paper, stage=abstract_stage,
                defaults={"decision": ScreeningDecision.Decision.PENDING},
            )
        else:
            abstract_row = ScreeningDecision.objects.select_for_update().filter(
                paper=paper, stage=abstract_stage,
            ).first()
            if abstract_row:
                ScreeningRevision.objects.create(
                    paper=paper, stage=abstract_stage,
                    previous_decision=abstract_row.decision,
                    new_decision="REMOVED", reason=("Title correction: " + reason)[:255],
                )
                abstract_row.delete()
    return current
