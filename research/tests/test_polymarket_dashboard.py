import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from arb_research.polymarket_dashboard import ReadOnlyStore, export_dashboard, main
from arb_research.polymarket.store import DataStore

RECORDING = Path(__file__).parent / "fixtures/polymarket/recorded-events.json"
BUNDLED = Path(__file__).parents[2] / "apps/web/src/features/polymarket/fixtures/recorded-sync.json"


def test_real_recording_reproduces_bundled_envelope_without_network(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline exporter must never access the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    data_dir, output = tmp_path / "data", tmp_path / "dashboard.json"
    assert main(["--data-dir", str(data_dir), "--event", "27824", "--recording", str(RECORDING),
                 "--output", str(output)]) == 0
    actual = json.loads(output.read_text())
    assert actual == json.loads(BUNDLED.read_text())
    assert actual["provenance"]["input_sha256"] == hashlib.sha256(RECORDING.read_bytes()).hexdigest()
    assert actual["compliance"]["trading_allowed"] is False
    assert actual["compliance"]["geoblock"]["blocked"] is True
    assert not any(actual[k] for k in ["forecasts", "pnl", "book"])
    manifest = data_dir / "manifest.sqlite3"
    before = manifest.read_bytes()
    store = ReadOnlyStore(data_dir)
    with store.connect() as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM events")
    partial = export_dashboard(store, "27824", limit=1)
    assert len(partial["prices"]["data"]) == 1
    assert partial["prices"]["next_offset"] == 1
    assert partial["compliance"] is None and partial["quality"]["collected_at"] is None
    with DataStore(data_dir).writer():
        with pytest.raises(RuntimeError, match="Sync is active"):
            export_dashboard(store, "27824")
    assert manifest.read_bytes() == before
    # Reject resolved aliases and source paths before any replacement occurs.
    alias = tmp_path / "alias"
    alias.symlink_to(data_dir, target_is_directory=True)
    for target in [manifest, alias / "manifest.sqlite3"]:
        with pytest.raises(SystemExit):
            main(["--data-dir", str(data_dir), "--event", "27824", "--output", str(target)])
    assert manifest.read_bytes() == before
    receipt = tmp_path / "sync-result.json"
    receipt.write_text(json.dumps([{"event_id": "27824", "compliance": actual["compliance"]}]))
    main(["--data-dir", str(data_dir), "--event", "27824", "--sync-result", str(receipt),
          "--output", str(output)])
    local = json.loads(output.read_text())
    assert local["source"] == "local-sync" and local["compliance"] == actual["compliance"]
    assert local["quality"]["collected_at"] is None
    assert local["quality"]["sync_incomplete"] is None

    # A concurrent writer can commit while the export pins the old WAL snapshot.
    original = store.history
    def concurrent_history(*args, **kwargs):
        with sqlite3.connect(manifest) as writer:
            writer.execute("DELETE FROM resolutions")
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "history", concurrent_history)
    stable = export_dashboard(store, "27824")
    assert stable["resolutions"]["data"] == actual["resolutions"]["data"]
    assert store.resolutions(store.event("27824"), stable["market_id"]).data == []
    # A refresh may change metadata/resolutions then fail before any checkpoint
    # persists. Equal old watermarks must still be UNKNOWN, never success.
    assert stable["quality"]["sync_incomplete"] is None
    with sqlite3.connect(manifest) as writer:
        writer.execute("UPDATE checkpoints SET body=json_set(body, '$.pending', json('{\"cursor\":\"partial\"}')) "
                       "WHERE token=(SELECT token FROM checkpoints LIMIT 1)")
    interrupted = export_dashboard(store, "27824")
    assert interrupted["quality"]["sync_incomplete"] is True
    with pytest.raises(SystemExit):
        main(["--data-dir", str(data_dir), "--event", "27824", "--recording", str(RECORDING),
              "--output", str(output)])


def test_read_consumer_does_not_create_missing_store(tmp_path):
    path = tmp_path / "absent"
    with pytest.raises(FileNotFoundError):
        ReadOnlyStore(path)
    assert not path.exists()
