from __future__ import annotations

import json

from src.swing import cli as cli_module


def test_update_volume_cache_writes_payload_and_reports_count(monkeypatch, tmp_path, capsys, database):
    payload_path = tmp_path / "volume.json"
    payload_path.write_text(
        json.dumps(
            {
                "AAPL": {"average_volume": 41_657_768, "average_dollar_volume": 12_345_678_900},
                "MSFT": {"average_volume": 29_001_478, "average_dollar_volume": 9_876_543_210},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("SWING_DATABASE_PATH", str(database.path))
    monkeypatch.setattr("sys.argv", ["swing-trader", "update-volume-cache", str(payload_path)])
    cli_module.main()

    result = json.loads(capsys.readouterr().out)
    assert result == {"symbols_updated": 2}

    cached = database.get_volume_cache_many(["AAPL", "MSFT"], ttl_seconds=86400)
    assert cached["AAPL"] == {"average_volume": 41_657_768.0, "average_dollar_volume": 12_345_678_900.0}
    assert cached["MSFT"] == {"average_volume": 29_001_478.0, "average_dollar_volume": 9_876_543_210.0}


def test_update_volume_cache_with_empty_payload_updates_nothing(monkeypatch, tmp_path, capsys, database):
    payload_path = tmp_path / "empty.json"
    payload_path.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("SWING_DATABASE_PATH", str(database.path))
    monkeypatch.setattr("sys.argv", ["swing-trader", "update-volume-cache", str(payload_path)])
    cli_module.main()

    result = json.loads(capsys.readouterr().out)
    assert result == {"symbols_updated": 0}
