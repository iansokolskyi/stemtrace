"""Custom build hook to compile React UI during package build."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

if TYPE_CHECKING:
    from collections.abc import Mapping

SKIP_ENV_VAR = "STEMTRACE_SKIP_UI_BUILD"


def npm_install_command(frontend_dir: Path) -> list[str]:
    """Return the npm command that installs frontend dependencies.

    Uses ``npm ci`` against the committed lockfile when it exists, so the
    resolved versions are exactly the locked ones. Lifecycle scripts are never
    run; none of the frontend dependencies need them to build.

    Args:
        frontend_dir: Directory containing ``package.json``.

    Returns:
        The command to pass to ``subprocess.run``.
    """
    if (frontend_dir / "package-lock.json").exists():
        return ["npm", "ci", "--ignore-scripts"]
    return ["npm", "install", "--ignore-scripts"]


def ui_build_skipped(environ: Mapping[str, str] | None = None) -> bool:
    """Return True when the UI build was disabled via the environment.

    Args:
        environ: Environment mapping to inspect. Defaults to ``os.environ``.

    Returns:
        Whether ``STEMTRACE_SKIP_UI_BUILD`` is set to a truthy value.
    """
    env = os.environ if environ is None else environ
    return env.get(SKIP_ENV_VAR, "").strip().lower() in {"1", "true", "yes"}


class CustomBuildHook(BuildHookInterface):
    """Build hook that compiles the React UI before packaging."""

    PLUGIN_NAME = "custom"

    def initialize(self, _version: str, _build_data: dict[str, Any]) -> None:
        """Install frontend dependencies and build the UI bundle."""
        if ui_build_skipped():
            self.app.display_info(f"{SKIP_ENV_VAR} is set, skipping UI build")
            return

        frontend_dir = (
            Path(self.root) / "src" / "stemtrace" / "server" / "ui" / "frontend"
        )
        dist_dir = frontend_dir / "dist"

        if not frontend_dir.exists():
            self.app.display_warning(f"Frontend directory not found: {frontend_dir}")
            return

        # Skip if dist already exists (e.g., in Docker multi-stage build)
        if dist_dir.exists() and (dist_dir / "index.html").exists():
            self.app.display_info("Frontend dist already exists, skipping build")
            return

        package_json = frontend_dir / "package.json"
        if not package_json.exists():
            self.app.display_warning("No package.json found, skipping UI build")
            return

        self.app.display_info("Installing frontend dependencies...")
        subprocess.run(
            npm_install_command(frontend_dir),
            cwd=frontend_dir,
            check=True,
            capture_output=True,
        )

        self.app.display_info("Building frontend...")
        subprocess.run(
            ["npm", "run", "build"],
            cwd=frontend_dir,
            check=True,
            capture_output=True,
        )

        self.app.display_success("Frontend build complete!")
