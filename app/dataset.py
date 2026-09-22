"""Load and validate the bundled labeled ticket dataset at startup."""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas import Ticket

DATASET_PATH = Path(__file__).resolve().parent.parent / "data" / "tickets.json"


def load_tickets(path: Path = DATASET_PATH) -> list[Ticket]:
    raw = json.loads(path.read_text())
    tickets = [Ticket(**item) for item in raw]
    ids = [t.id for t in tickets]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate ticket ids in dataset")
    if not tickets:
        raise ValueError("dataset is empty")
    return tickets
