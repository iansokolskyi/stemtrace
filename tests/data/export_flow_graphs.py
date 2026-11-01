"""Write the API's graph for every capture in celery_flows.json (input of the UI layout tests).

Run from the repository root:

    PYTHONPATH=src python tests/data/export_flow_graphs.py

Each capture is replayed into a store and read back through ``GET /api/graphs/{root_id}``, so the
JSON holds the nodes exactly as the server sends them. A capture with several roots gets one
entry per root (``name``, ``name#2``, ...).
"""

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from stemtrace.core.events import TaskEvent
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.store import GraphStore

DATA = Path(__file__).parent
OUT = (
    DATA.parents[1] / "src/stemtrace/server/ui/frontend/tests/fixtures/flow-graphs.json"
)


def main() -> None:
    """Replay each capture and write the graphs the API returns."""
    captures = json.loads((DATA / "celery_flows.json").read_text())
    out: dict[str, dict[str, object]] = {}
    for name, events in sorted(captures.items()):
        store = GraphStore()
        for event in events:
            store.add_event(TaskEvent.model_validate(event))
        app = FastAPI()
        app.include_router(create_api_router(store))
        client = TestClient(app)
        graphs = client.get("/api/graphs").json()["graphs"]
        for index, graph in enumerate(sorted(graphs, key=lambda g: g["task_id"])):
            key = name if index == 0 else f"{name}#{index + 1}"
            out[key] = client.get(f"/api/graphs/{graph['task_id']}").json()
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(out)} graphs to {OUT}")


if __name__ == "__main__":
    main()
