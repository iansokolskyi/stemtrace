"""The v0.4.2 ``core/graph.py``, loaded under another name for differential tests.

``tests/data/graph_baseline_v0_4_2.py.txt`` is a verbatim copy of the module at
commit 4c74027 (the release before the chord_unlock fixes). A pinned copy is
loaded, not ``git show`` at test time, so the tests need no git history (CI
checks out one commit) and the baseline never moves. It is text, not a ``.py``
file, so linters and coverage ignore it.
"""

import sys
from pathlib import Path
from types import ModuleType

_NAME = "stemtrace_graph_baseline_v0_4_2"
_SOURCE = Path(__file__).parent / "data" / "graph_baseline_v0_4_2.py.txt"


def load_baseline() -> ModuleType:
    """Return the baseline graph module (loaded once)."""
    module = sys.modules.get(_NAME)
    if module is None:
        module = ModuleType(_NAME)
        # pydantic resolves forward references through ``sys.modules``.
        sys.modules[_NAME] = module
        code = compile(_SOURCE.read_text(), str(_SOURCE), "exec")
        exec(code, module.__dict__)
    return module
