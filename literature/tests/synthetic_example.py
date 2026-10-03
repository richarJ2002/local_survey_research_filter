"""Create a shareable example from the existing synthetic roundtrip fixture only.

@File: synthetic_example.py
@Brief: Explicit absent destination outside source; no live settings or data input.
@Date: 03/10/2026
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


# --- Reuse the qualified fixture, not a second demo-data implementation ---
def main() -> None:
    """Build in temporary private state and atomically publish a two-file copy."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "destination", type=Path, help="absent absolute folder outside the checkout"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    target = args.destination
    if (
        not target.is_absolute()
        or target.exists()
        or target.is_symlink()
        or not target.parent.is_dir()
        or target.resolve().is_relative_to(root)
        or any((p / "project.json").exists() for p in target.resolve().parents)
    ):
        parser.error(
            "use an absent absolute folder with an existing parent outside source/projects"
        )
    with tempfile.TemporaryDirectory(prefix="survey-synthetic-example-") as tmp:
        for key in (
            "WP_ROLE",
            "WP_LAUNCHER",
            "WP_PROJECT_UUID",
            "WP_WORKER_EPOCH",
            "WP_OWNER_FD",
            "LOCAL_SURVEY_CREDENTIALS_FILE",
        ):
            os.environ.pop(key, None)
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings",
            PYTHON_DOTENV_DISABLED="1",
            LOCAL_SURVEY_DB=str(Path(tmp) / "unused.sqlite3"),
            XDG_DATA_HOME=str(Path(tmp) / "state"),
            XDG_CACHE_HOME=str(Path(tmp) / "cache"),
        )
        from literature import project_store as ps

        source = ps.create_project_store(Path(tmp) / "fixture", "Synthetic example — not research")
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import django; django.setup(); "
                    "from literature.tests.test_wp0108_copy_recovery import _SEED; exec(_SEED)"
                ),
            ],
            cwd=root,
            env=dict(os.environ, LOCAL_SURVEY_DB=str(source.location / ps.DATABASE_FILENAME)),
            check=True,
        )
        shared = ps.save_copy_store(source.location, target)
        ps.validate_package(shared.location)
    print(f"Synthetic example ready for Load: {shared.location}")


if __name__ == "__main__":
    main()
