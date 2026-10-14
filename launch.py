#!/usr/bin/env python3
"""
Parse manager startup or developer Django commands and delegate runtime.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File: launch.py
@Brief: Thin entry point; runtime isolation belongs to config.launcher.
@Date: 03/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

# Custom library Imports
from config import launcher

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Public startup flags and opaque Django arguments ---


def _build_arg_parser() -> argparse.ArgumentParser:
    """
    Build the launcher parser without consuming Django's own flags.

    Args:
        None

    Kwargs:
        None

    Returns:
        argparse.ArgumentParser: Manager flags plus an opaque django command.
    """
    parser = argparse.ArgumentParser(
        description="Linux local survey New / Load manager; Python 3.12 "
        "required. Developer commands: .venv/bin/python launch.py django "
        "<args> (legacy rollback: --legacy-shared-db).",
        epilog="Examples: python3.12 launch.py --port 8000; "
        ".venv/bin/python launch.py django check. Administrative writes "
        "require LOCAL_SURVEY_DB or --legacy-shared-db; untargeted "
        "test/check/makemigrations use memory.",
    )
    parser.add_argument("command", nargs="?", choices=("django",),
                        help="run guarded Django commands without bootstrap")
    parser.add_argument("django_args", nargs=argparse.REMAINDER,
                        help="arguments forwarded unchanged to Django")
    parser.add_argument("--port", type=int, default=8000,
                        help="loopback manager port (default: 8000; 0: free)")
    parser.add_argument(
        "--install", action="store_true",
        help="explicitly repair/apply existing requirements",
    )
    parser.add_argument(
        "--legacy-env",
        help="explicit existing .env compatibility (settings saves there)",
    )
    parser.add_argument("--worker", action="store_true",
                        help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse plain CLI values and delegate to the owning runtime module.

    Args:
        None

    Kwargs:
        argv: Arguments to parse; defaults to sys.argv[1:].

    Returns:
        int: Runtime exit status.

    Raises:
        SystemExit: If argparse or Django handles help or invalid arguments.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _build_arg_parser()
    args = parser.parse_args(arguments)
    if args.command == "django":
        if args.install or args.legacy_env or args.worker or args.port != 8000:
            parser.error("manager startup flags cannot precede django")
        return launcher.run_django(args.django_args)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    return launcher.run_manager(
        args.port, install=args.install, legacy_env=args.legacy_env,
        worker=args.worker, arguments=arguments,
    )


# ------------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------------ #

if __name__ == "__main__":
    raise SystemExit(main())
