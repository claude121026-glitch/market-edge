"""Once-per-day guards for jobs that are scheduled twice (GitHub Actions backup runs)."""
import sys

from market_edge import config, nse, run


def test_morning_and_evening_run_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    con = nse.connect()
    con.execute("INSERT INTO daily (symbol, series, date, close) VALUES ('ABC','EQ','2026-11-04', 1)")
    con.commit()
    con.close()
    run._mark("evening", "2026-11-04")
    monkeypatch.setattr(sys, "argv", ["run", "evening", "--no-update"])
    run.main()
    assert "already reported" in capsys.readouterr().out

    from datetime import datetime
    run._mark("morning", datetime.now(run.IST).date().isoformat())
    monkeypatch.setattr(sys, "argv", ["run", "morning"])
    run.main()
    assert "already sent" in capsys.readouterr().out
