"""Custom build hook to compile React UI during package build."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Build hook that compiles the React UI before packaging."""

    PLUGIN_NAME = "custom"

    def initialize(self, _version: str, _build_data: dict[str, Any]) -> None:
        """Run npm install and build for the frontend."""
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
            ["npm", "install"],
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
