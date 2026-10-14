"""
Expose Django's ASGI application for the configured process role.

Import selects config.settings if unset and initializes the module-level
``application`` callable through Django's ASGI factory.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   asgi.py

@Brief:  Expose Django's ASGI application for the configured process role.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import os

# Custom library Imports
from django.core.asgi import get_asgi_application

# ------------------------------------------------------------------------ #
# Module setup
# ------------------------------------------------------------------------ #

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_asgi_application()
