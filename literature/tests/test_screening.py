#!/usr/bin/env python3

"""Verify screening advancement, rejection, and decision immutability.

@File:   test_screening.py

@Brief:  Tests for Yes/No screening transitions and immutability.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems
    (AERO-Sys) group.

"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.test import TestCase

from literature.models import (
    Discovery, Paper, Project, ScreeningDecision, Search,
)
from literature.services import screening_service

# ------------------------------------------------------------------------ #
# Screening tests
# ------------------------------------------------------------------------ #


class ScreeningTests(TestCase):
    """Accept advances, reject stops, decided rows are immutable."""

    # --- Setup helper ---
    def _setup(self) -> tuple:
        """Create project with Title -> Abstract stages and one paper.

        Args:
            None

        Kwargs:
            None

        Returns:
            tuple: Project, Title stage, Abstract stage, and discovered paper
                with a pending Title decision.
        """
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        title = project.stages.get(order=1)
        abstract = project.stages.get(order=2)
        paper = Paper.objects.create(title="T")
        search = Search.objects.create(
            project=project, name="S", keywords=["test"])
        Discovery.objects.create(
            paper=paper, search=search, source="manual", query="test")
        ScreeningDecision.objects.get_or_create(paper=paper, stage=title)
        return project, title, abstract, paper

    # --- Accept advances ---
    def test_accept_creates_next_stage(self) -> None:
        """Create a pending Abstract decision after accepting the Title.

        Accept in Title creates PENDING Abstract row.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        _project, title, abstract, paper = self._setup()
        screening_service.decide(paper=paper, stage=title, decision="ACCEPT")
        nxt = ScreeningDecision.objects.filter(
            paper=paper, stage=abstract).first()
        assert nxt is not None
        assert nxt.decision == "PENDING"

    # --- Reject stops ---
    def test_reject_stops(self) -> None:
        """Stop at Title rejection without creating an Abstract decision.

        Reject creates no next row.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        _project, title, abstract, paper = self._setup()
        screening_service.decide(paper=paper, stage=title, decision="REJECT")
        assert not ScreeningDecision.objects.filter(
            paper=paper, stage=abstract).exists()

    # --- Immutable ---
    def test_decided_is_immutable(self) -> None:
        """Require InvalidTransition when changing an accepted Title decision.

        Second decision on same stage raises.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        _project, title, _abstract, paper = self._setup()
        screening_service.decide(paper=paper, stage=title, decision="ACCEPT")
        try:
            screening_service.decide(
                paper=paper, stage=title, decision="REJECT")
        except screening_service.InvalidTransition:
            return
        raise AssertionError("Expected InvalidTransition")
