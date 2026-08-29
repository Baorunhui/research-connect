-- Daily Paper Reader homepage audience statistics.
-- Raw anonymous hashes are insert-only for public clients; only daily totals are readable.

create schema if not exists private;
revoke all on schema private from public;

create table if not exists public.site_daily_reader_events (
  visit_date date not null,
  visitor_hash text not null,
  recorded_at timestamptz not null default now(),
  primary key (visit_date, visitor_hash),
  constraint site_daily_reader_events_hash_format
    check (visitor_hash ~ '^[a-f0-9]{64}$')
);

create table if not exists public.site_daily_reader_counts (
  visit_date date primary key,
  reader_count bigint not null default 0,
  updated_at timestamptz not null default now(),
  constraint site_daily_reader_counts_nonnegative
    check (reader_count >= 0)
);

alter table public.site_daily_reader_events enable row level security;
alter table public.site_daily_reader_counts enable row level security;

revoke all on table public.site_daily_reader_events from anon, authenticated;
revoke all on table public.site_daily_reader_counts from anon, authenticated;

grant usage on schema public to anon, authenticated;
grant insert on public.site_daily_reader_events to anon, authenticated;
grant select on public.site_daily_reader_counts to anon, authenticated;
grant select, insert, update, delete on public.site_daily_reader_events to service_role;
grant select, insert, update, delete on public.site_daily_reader_counts to service_role;

drop policy if exists site_daily_reader_events_insert_today
  on public.site_daily_reader_events;
create policy site_daily_reader_events_insert_today
  on public.site_daily_reader_events
  for insert
  to anon, authenticated
  with check (
    visit_date = (now() at time zone 'Asia/Shanghai')::date
    and visitor_hash ~ '^[a-f0-9]{64}$'
  );

drop policy if exists site_daily_reader_counts_select_today
  on public.site_daily_reader_counts;
drop policy if exists site_daily_reader_counts_select_recent
  on public.site_daily_reader_counts;
create policy site_daily_reader_counts_select_recent
  on public.site_daily_reader_counts
  for select
  to anon, authenticated
  using (
    visit_date between
      ((now() at time zone 'Asia/Shanghai')::date - 29)
      and (now() at time zone 'Asia/Shanghai')::date
  );

create or replace function private.increment_site_daily_reader_count()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.site_daily_reader_counts (visit_date, reader_count, updated_at)
  values (new.visit_date, 1, now())
  on conflict (visit_date) do update
    set reader_count = public.site_daily_reader_counts.reader_count + 1,
        updated_at = now();
  return new;
end;
$$;

revoke all on function private.increment_site_daily_reader_count() from public;
revoke all on function private.increment_site_daily_reader_count() from anon, authenticated;

drop trigger if exists site_daily_reader_events_increment_count
  on public.site_daily_reader_events;
create trigger site_daily_reader_events_increment_count
after insert on public.site_daily_reader_events
for each row
execute function private.increment_site_daily_reader_count();
