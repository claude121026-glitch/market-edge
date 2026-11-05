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


def test_catch_up_sends_missed_reports_only_in_their_window(monkeypatch):
    from datetime import datetime as real_dt
    called = []
    monkeypatch.setattr(run, "run_morning", lambda *a, **k: called.append("morning") or True)
    monkeypatch.setattr(run, "run_evening", lambda *a, **k: called.append("evening") or True)

    def at(y, m, d, hh, mm):
        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return real_dt(y, m, d, hh, mm, tzinfo=tz)
        monkeypatch.setattr(run, "datetime", FakeDT)
        called.clear()
        return run._catch_up()

    assert at(2026, 11, 6, 7, 40) == [] and called == []                         # Fri 07:40: too early
    assert at(2026, 11, 6, 9, 0) == ["morning brief"] and called == ["morning"]  # Fri 09:00: morning due
    assert at(2026, 11, 6, 19, 0) == ["morning brief", "evening report"]         # Fri 19:00: both checked
    assert at(2026, 11, 7, 12, 0) == [] and called == []                         # Saturday: nothing
    monkeypatch.setattr(run, "run_morning", lambda *a, **k: False)               # already sent today
    assert at(2026, 11, 6, 9, 0) == []
