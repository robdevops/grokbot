"""One-off: convert an existing database's period_cache to WITHOUT ROWID (stop the bot first)."""

from __future__ import annotations

import sqlite3
import sys

# Keep in step with the period_cache table in lib/store.py.
CREATE = """CREATE TABLE period_cache (
    portfolio_id INTEGER, start TEXT, end TEXT, pct REAL, gain REAL, currency TEXT, movers TEXT,
    v INTEGER, PRIMARY KEY (portfolio_id, start, end)) WITHOUT ROWID"""


def convert(path: str) -> int | None:
    """Rebuild period_cache keeping every row. Returns the row count, or None if there was nothing to do."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        row = db.execute("SELECT sql FROM sqlite_master WHERE name = 'period_cache'").fetchone()
        if row is None or "WITHOUT ROWID" in row[0]:
            return None
        db.execute("BEGIN")
        db.execute("ALTER TABLE period_cache RENAME TO period_cache_old")
        db.execute(CREATE)
        db.execute("INSERT INTO period_cache SELECT * FROM period_cache_old")
        count = db.execute("SELECT COUNT(*) FROM period_cache").fetchone()[0]
        db.execute("DROP TABLE period_cache_old")
        db.execute("COMMIT")
        db.execute("VACUUM")
        return count
    finally:
        db.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tools.period_cache_without_rowid <db path>   (stop the bot first)")
        return 2
    count = convert(argv[1])
    print("period_cache is already WITHOUT ROWID (or absent); nothing to do" if count is None
          else f"converted period_cache: {count} rows kept")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
