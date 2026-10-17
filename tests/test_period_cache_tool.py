import sqlite3

from lib.store import Store
from tools.period_cache_without_rowid import convert

OLD = """CREATE TABLE period_cache (
    portfolio_id INTEGER, start TEXT, end TEXT, pct REAL, gain REAL, currency TEXT, movers TEXT,
    v INTEGER, PRIMARY KEY (portfolio_id, start, end))"""


def test_an_old_period_cache_keeps_its_rows_and_becomes_without_rowid(tmp_path):
    path = str(tmp_path / "x.db")
    db = sqlite3.connect(path)
    db.execute(OLD)
    db.executemany("INSERT INTO period_cache VALUES (?,?,?,?,?,?,?,?)",
                   [(1, f"2026-01-0{d}", f"2026-01-0{d}", 1.5, 10.0, "AUD", "[]", 1) for d in range(1, 4)])
    db.commit()
    db.close()
    assert convert(path) == 3
    assert convert(path) is None  # already converted
    db = sqlite3.connect(path)
    assert "WITHOUT ROWID" in db.execute("SELECT sql FROM sqlite_master WHERE name = 'period_cache'").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM period_cache").fetchone()[0] == 3
    db.close()
    assert Store(path).period_count(1, "2026-01-01", "2026-01-31") == 3  # the bot opens it fine
