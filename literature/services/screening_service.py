#!/usr/bin/env python3

'''
@File:   screening_service.py

@Brief:  Yes/No screening state machine (immutable once decided).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

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
    """Raised when a screening decision cannot be applied."""


def title_ready(paper: Paper) -> bool:
    """A known title is sufficient for Title screening; other metadata is completed later."""
    return bool((paper.title or "").strip())


def abstract_available(value: str | None) -> bool:
    """Distinguish an actual abstract from the confirmed HAL audience-only tag."""
    # ponytail: reject only the observed exact tag; short real abstracts remain valid.
    return isinstance(value, str) and bool(value.strip()) and value.strip().casefold() != "international audience"


def has_reference_list(paper: Paper) -> bool:
    """Recognize either provider identifiers or a manually supplied bibliography."""
    return bool(paper.references or (paper.reference_text or "").strip())


def _reference_filter() -> Q:
    """Match a reference list without pretending raw bibliography text creates graph edges."""
    return ~Q(paper__references=[]) | Q(paper__reference_text__regex=r"\S")


def final_decisions(project: Project) -> QuerySet[ScreeningDecision]:
    """Final selection requires Abstract content acceptance and reference completion."""
    return ScreeningDecision.objects.filter(stage__project=project, stage__name="Abstract", decision="ACCEPT", paper__discoveries__search__project=project).filter(_reference_filter()).distinct()


def pending_references(project: Project) -> QuerySet[ScreeningDecision]:
    """Papers passing Abstract content but still waiting for a bibliography."""
    return ScreeningDecision.objects.filter(stage__project=project, stage__name="Abstract", decision="ACCEPT", paper__discoveries__search__project=project).exclude(_reference_filter()).distinct()


def _validate_status(paper: Paper, stage: Stage, decision: str) -> None:
    """Enforce project membership and prerequisites at both decision entry points."""
    if stage.name not in {"Title", "Abstract"}:
        raise InvalidTransition("Choose Title or Abstract screening.")
    if not paper.discoveries.filter(search__project=stage.project).exists():
        raise InvalidTransition("Paper does not belong to this project.")
    if not title_ready(paper):
        raise InvalidTransition("A title is required before Title screening.")
    if stage.name == "Abstract":
        if not paper.decisions.filter(stage__project=stage.project, stage__name="Title", decision="ACCEPT").exists():
            raise InvalidTransition("Accept the title before changing the abstract status.")
        if decision == "ACCEPT" and not abstract_available(paper.abstract):
            raise InvalidTransition("No abstract found. Enter the abstract before accepting this paper.")


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
    """
    normalized = (decision or "").upper()
    if normalized not in {
        ScreeningDecision.Decision.ACCEPT,
        ScreeningDecision.Decision.REJECT,
    }:
        raise InvalidTransition(f"Decision must be ACCEPT or REJECT, got {decision!r}")

    _validate_status(paper, stage, normalized)
    current, _ = ScreeningDecision.objects.get_or_create(
        paper=paper, stage=stage, defaults={"decision": ScreeningDecision.Decision.PENDING}
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
        raise InvalidTransition("This paper was already decided in another request.")

    if normalized == ScreeningDecision.Decision.ACCEPT and not stage.is_final:
        next_stage = (
            Stage.objects.filter(project=stage.project, order=stage.order + 1).first()
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
    """
    new_status = (decision or "").upper()
    if stage.name not in {"Title", "Abstract"} or new_status not in {
        ScreeningDecision.Decision.PENDING,
        ScreeningDecision.Decision.ACCEPT,
        ScreeningDecision.Decision.REJECT,
    }:
        raise InvalidTransition("Choose a Title or Abstract status: Pending, Yes or No.")
    reason = (reason or "").strip()
    if not reason or len(reason) > 255:
        raise InvalidTransition("Explain this correction (up to 255 characters).")
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
        abstract_stage = Stage.objects.get(project=stage.project, name="Abstract")
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
