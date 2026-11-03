-- Apply the same migration to separate development and production projects.
begin;
create table public.research_threads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  title text not null default '새로운 조사' check (length(title) <= 100),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  revision integer not null default 0,
  context jsonb not null default '{}'::jsonb,
  unique (id, user_id)
);
create table public.research_turns (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null,
  user_id uuid not null default auth.uid(),
  sequence integer not null,
  question text not null check (length(question) between 1 and 2000),
  result jsonb not null,
  created_at timestamptz not null default now(),
  foreign key (thread_id, user_id) references public.research_threads(id, user_id) on delete cascade,
  unique (thread_id, sequence)
);
create index research_threads_owner_updated on public.research_threads(user_id, updated_at desc);
create index research_turns_owner on public.research_turns(user_id);
alter table public.research_threads enable row level security;
alter table public.research_turns enable row level security;
create policy own_threads on public.research_threads to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy own_turns on public.research_turns to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
revoke all on public.research_threads, public.research_turns from anon, authenticated;
grant select, insert, update, delete on public.research_threads to authenticated;
grant select, insert on public.research_turns to authenticated;

create function public.create_research_thread()
returns setof public.research_threads language sql security invoker set search_path = '' as $$
  insert into public.research_threads default values returning *;
$$;

-- The revision check and both writes are one transaction. Two tabs cannot
-- silently overwrite each other's conversation state.
create function public.append_research_turn(p_thread uuid, p_revision integer,
  p_question text, p_result jsonb, p_context jsonb)
returns integer language plpgsql security invoker set search_path = '' as $$
declare new_revision integer;
begin
  if octet_length(p_result::text) + octet_length(p_context::text) > 2000000 then
    raise exception 'Research snapshot too large';
  end if;
  update public.research_threads
    set revision = revision + 1, updated_at = now(), context = p_context,
        title = case when revision = 0 then left(p_question, 100) else title end
    where id = p_thread and user_id = auth.uid() and revision = p_revision
    returning revision into new_revision;
  if new_revision is null then
    raise exception 'Research not found or changed concurrently';
  end if;
  insert into public.research_turns(thread_id, user_id, sequence, question, result)
    values(p_thread, auth.uid(), new_revision, p_question, p_result);
  return new_revision;
end;
$$;
revoke execute on function public.create_research_thread() from public, anon;
revoke execute on function public.append_research_turn(uuid, integer, text, jsonb, jsonb) from public, anon;
grant execute on function public.create_research_thread() to authenticated;
grant execute on function public.append_research_turn(uuid, integer, text, jsonb, jsonb) to authenticated;
commit;
