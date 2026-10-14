"""
Register the literature Django app and its default primary-key field type.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   apps.py

@Brief:  Register the literature Django app and its default primary-key field type.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Custom library Imports
from django.apps import AppConfig

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes


class LiteratureConfig(AppConfig):
    """
    Identify literature and default model primary keys to BigAutoField.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'literature'
