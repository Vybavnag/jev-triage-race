"""The bundled dataset is the only source of labels, so every row must carry
them; the Ticket type is what enforces that at load time."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.dataset import load_tickets
from app.schemas import Ticket


def write(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "tickets.json"
    path.write_text(json.dumps(rows))
    return path


def test_bundled_dataset_is_fully_labeled_with_unique_ids():
    tickets = load_tickets()
    assert len(tickets) >= 25
    assert all(isinstance(t, Ticket) for t in tickets)
    assert len({t.id for t in tickets}) == len(tickets)


def test_row_without_labels_is_rejected(tmp_path):
    with pytest.raises(ValidationError):
        load_tickets(write(tmp_path, [{"id": "x", "text": "hi"}]))


def test_duplicate_ids_are_rejected(tmp_path):
    row = {"id": "x", "text": "hi", "urgent": False, "team": "billing", "frustration": 1}
    with pytest.raises(ValueError, match="duplicate"):
        load_tickets(write(tmp_path, [row, row]))


def test_empty_dataset_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        load_tickets(write(tmp_path, []))
