#!/usr/bin/env python
# Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
"""Manager startup and explicitly targeted Django administrative tasks.

@File: manage.py
@Brief: Ordinary startup never falls back to the legacy research database.
@Date: 04/10/2026
"""

import os
import sys
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from pathlib import Path


def _top_level_parser() -> ArgumentParser:
    """Explain common commands without intercepting Django subcommand arguments."""
    parser = ArgumentParser(
        prog="python manage.py",
        description="Manage the Local Survey Research Filter Django application.",
        formatter_class=RawDescriptionHelpFormatter,
        epilog=(
            "Common commands:\n"
            "  python manage.py runserver 8000       Start the New / Load manager\n"
            "  LOCAL_SURVEY_DB=/explicit/dev.sqlite3 python manage.py migrate\n"
            "  python manage.py check                Check project configuration\n"
            "  python manage.py test literature      Run the application tests\n"
            "  python manage.py help                 List all Django commands\n"
            "  python manage.py help runserver       Show manager startup options\n\n"
            "Legacy developer/rollback only: add --legacy-shared-db explicitly.\n"
            "Without an explicit DB, check/test/makemigrations use memory, not legacy data."
        ),
    )
    parser.add_argument("command", nargs="?", help="Django management subcommand")
    return parser


# --- Translate only supported loopback syntax; keep one foreground supervisor ---
def _run_manager(arguments: list[str]) -> None:
    """Exec the ordinary launcher without loading Django or legacy credentials."""
    parser = ArgumentParser(
        prog="python manage.py runserver",
        description=(
            "Start the New / Load manager (same as python3.12 launch.py). "
            "For Django developer/rollback options, explicitly add --legacy-shared-db."
        ),
    )
    parser.add_argument("addrport", nargs="?", default="8000", help="port or localhost:port")
    args = parser.parse_args(arguments)
    host, separator, port = args.addrport.rpartition(":")
    if not separator:
        port = args.addrport
    if (separator and host not in {"127.0.0.1", "localhost"}) or not port.isdecimal():
        parser.error("use a loopback port: 8000, localhost:8000 or 127.0.0.1:8000")
    if not 0 <= int(port) <= 65535:
        parser.error("port must be between 0 and 65535")
    launcher = Path(__file__).resolve().with_name("launch.py")
    os.execv(sys.executable, [sys.executable, str(launcher), "--port", port])


# --- Guard before Django setup/dotenv/database access, including unknown commands ---
def main() -> None:
    """Dispatch normal startup; require deliberate targets for administrative writes."""
    arguments = sys.argv[1:]
    if not arguments or arguments[0] in {"-h", "--help"}:
        _top_level_parser().print_help()
        return
    if len(arguments) == 1 and arguments[0].isdecimal():
        _top_level_parser().error("a port needs 'runserver', e.g. python manage.py runserver 8000")
    legacy = "--legacy-shared-db" in arguments
    if legacy:
        arguments = [arg for arg in arguments if arg != "--legacy-shared-db"]
        if os.environ.get("WP_ROLE", "developer") != "developer":
            _top_level_parser().error("--legacy-shared-db is only for developer/rollback mode")
    fixed_role = os.environ.get("WP_ROLE") in {"worker", "manager"}
    if arguments and not legacy and not fixed_role:
        if arguments[0] == "runserver":
            _run_manager(arguments[1:])
            return
        if arguments == ["help", "runserver"]:
            _run_manager(["--help"])
            return
    if (
        arguments
        and arguments[0] not in {"check", "test", "makemigrations", "help", "version"}
        and not any(arg in {"-h", "--help", "--version"} for arg in arguments)
        and not os.environ.get("LOCAL_SURVEY_DB")
        and not legacy
    ):
        _top_level_parser().error(
            "administrative commands require an explicit LOCAL_SURVEY_DB or "
            "--legacy-shared-db; refusing the implicit projects/db.sqlite3 target"
        )
    if legacy:
        os.environ["LOCAL_SURVEY_LEGACY_SHARED_DB"] = "1"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line([sys.argv[0], *arguments])


if __name__ == "__main__":
    main()
