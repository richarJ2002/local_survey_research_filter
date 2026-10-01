#!/usr/bin/env python
# Copyright (c) 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
"""Django's command-line utility for administrative tasks."""
import os
import sys
from argparse import ArgumentParser, RawDescriptionHelpFormatter


def _top_level_parser() -> ArgumentParser:
    """Explain common commands without intercepting Django subcommand arguments."""
    parser = ArgumentParser(
        prog="python manage.py",
        description="Manage the Local Survey Research Filter Django application.",
        formatter_class=RawDescriptionHelpFormatter,
        epilog=(
            "Common commands:\n"
            "  python manage.py runserver 8000       Start the website on localhost:8000\n"
            "  python manage.py migrate              Apply database migrations\n"
            "  python manage.py check                Check project configuration\n"
            "  python manage.py test literature      Run the application tests\n"
            "  python manage.py help                 List all Django commands\n"
            "  python manage.py help runserver       Show options for one command"
        ),
    )
    parser.add_argument("command", nargs="?", help="Django management subcommand")
    return parser


def main() -> None:
    """Show entry-point help or forward subcommands unchanged to Django."""
    arguments = sys.argv[1:]
    if not arguments or arguments[0] in {"-h", "--help"}:
        _top_level_parser().print_help()
        return
    if len(arguments) == 1 and arguments[0].isdecimal():
        _top_level_parser().error("a port needs 'runserver', e.g. python manage.py runserver 8000")
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
