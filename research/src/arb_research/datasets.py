"""Closed, packaged publication registry. No URL/path/provider supplied by clients."""

import hashlib
import json
from datetime import datetime
from importlib.resources import files

from .contracts import CreateBacktest


def _load() -> dict:
    root = files("arb_research").joinpath("fixtures")
    registry = json.loads(root.joinpath("registry.json").read_text())
    datasets = {}
    for item in registry:
        if item["status"] != "published" or item["frozen"] is not True:
            continue
        raw = root.joinpath(item["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Dataset checksum mismatch: " + item["id"])
        datasets[item["id"]] = {**item, "bars": json.loads(raw)}
    return datasets


def catalog() -> list[dict]:
    return [{k: v for k, v in d.items() if k not in ("bars", "file")} for d in _load().values()]


def snapshot(request: CreateBacktest) -> dict:
    dataset = _load().get(request.dataset_id)
    if dataset is None:
        raise ValueError("dataset_id must identify a published frozen Dataset")
    if (request.start < datetime.fromisoformat(dataset["start"])
            or request.end > datetime.fromisoformat(dataset["end"])):
        raise ValueError("Requested interval is outside Dataset coverage")
    bars = [
        b for b in dataset["bars"]
        if request.start <= datetime.fromisoformat(b["timestamp"])
        and datetime.fromisoformat(b["close_timestamp"]) <= request.end
    ]
    if len(bars) <= request.strategy.parameters.slow_window:
        raise ValueError("Interval must contain more complete bars than slow_window")
    return {**dataset, "bars": bars}
