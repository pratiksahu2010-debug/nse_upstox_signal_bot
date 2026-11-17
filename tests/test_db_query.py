from datetime import date

from app.db import PostgresStore


def test_postgres_open_signals_builds_filters_before_order_by():
    store = PostgresStore.__new__(PostgresStore)
    calls = []

    def capture(sql, params, fetch):
        calls.append((sql, params, fetch))
        return []

    store._run = capture

    assert store.open_signals(date(2026, 11, 17), "orb", "RELIANCE") == []
    sql, params, fetch = calls[-1]
    assert sql.index("AND module=%s") < sql.index("ORDER BY")
    assert sql.index("AND symbol=%s") < sql.index("ORDER BY")
    assert params == [date(2026, 11, 17), "orb", "RELIANCE"]
    assert fetch == "all"

    store.open_signals(date(2026, 11, 17))
    sql, params, _ = calls[-1]
    assert "ORDER BY bar_ts, id" in sql
    assert "AND module=%s" not in sql
    assert params == [date(2026, 11, 17)]
