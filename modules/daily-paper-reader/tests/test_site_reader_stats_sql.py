from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = ROOT / "sql" / "create_site_reader_stats_schema.sql"


def test_public_reader_history_is_limited_to_recent_aggregate_counts():
    sql = SCHEMA_SQL.read_text(encoding="utf-8")

    assert "drop policy if exists site_daily_reader_counts_select_today" in sql
    assert "create policy site_daily_reader_counts_select_recent" in sql
    assert "visit_date between" in sql
    assert "::date - 29" in sql
    assert "grant select on public.site_daily_reader_counts" in sql


def test_raw_reader_events_remain_insert_only_for_public_clients():
    sql = SCHEMA_SQL.read_text(encoding="utf-8")

    assert "revoke all on table public.site_daily_reader_events from anon, authenticated" in sql
    assert "grant insert on public.site_daily_reader_events to anon, authenticated" in sql
    assert "grant select on public.site_daily_reader_events to anon" not in sql
    assert "grant select on public.site_daily_reader_events to authenticated" not in sql
