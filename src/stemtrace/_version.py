"""Package version, kept in a leaf module.

It imports nothing, so ``stemtrace.server`` can read the version without
importing the package root (which pulls in ``stemtrace.library``).
bump-my-version updates the string; see ``.bumpversion.toml``.
"""

__version__ = "0.4.4"
