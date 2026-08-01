-- WAGS Insight — Supabase schema for Task 01
--
-- Run this once in: Supabase dashboard -> SQL Editor -> New query -> paste -> Run
-- Safe to re-run: every statement is idempotent.
--
-- Two tables only. Nothing here is created from application code, by design.

-- ---------------------------------------------------------------------------
-- chat_messages — one row per turn.
--
-- Three row shapes share this table, which is why most columns are nullable:
--   role='user'      question text, no tokens, no tool data
--   role='tool'      tool_args + tool_result, no content
--   role='assistant' final answer text, tokens, model, latency
-- ---------------------------------------------------------------------------
create table if not exists chat_messages (
  id                bigserial     primary key,
  session_id        text          not null,             -- groups one conversation
  role              text          not null,             -- user | assistant | tool
  content           text,                               -- question, or final answer
  tool_args         jsonb,                              -- keys the model chose
  tool_result       jsonb,                              -- aggregates only, never raw rows
  prompt_tokens     int,                                -- from Groq usage
  completion_tokens int,                                -- from Groq usage
  model             text,                               -- llama-3.3-70b-versatile
  latency_ms        int,                                -- round trip for that call
  cost_usd          numeric(12,6),                      -- left NULL until the rate is confirmed
  created_at        timestamptz   not null default now(),

  constraint chat_messages_role_check
    check (role in ('user', 'assistant', 'tool'))
);

create index if not exists idx_chat_messages_session
  on chat_messages (session_id, created_at);


-- ---------------------------------------------------------------------------
-- settings — runtime knobs, editable without a deploy.
-- ---------------------------------------------------------------------------
create table if not exists settings (
  key        text        primary key,
  value      text        not null,                      -- stored as text, parsed in code
  updated_at timestamptz not null default now()
);

insert into settings (key, value) values
  ('daily_query_limit', '10'),
  ('data_start',        '2026-01-01'),
  ('max_buckets',       '200')
on conflict (key) do nothing;


-- ---------------------------------------------------------------------------
-- Security. Do not skip this.
--
-- Without RLS these tables are readable by anyone holding the anon key, which
-- is public by design and ships in browser code. Chat history and business
-- figures would be exposed.
--
-- RLS on with NO policies blocks the anon key completely. The service-role key
-- the backend uses bypasses RLS, so the app keeps working and only the server
-- can reach these tables.
-- ---------------------------------------------------------------------------
alter table chat_messages enable row level security;
alter table settings      enable row level security;


-- ---------------------------------------------------------------------------
-- Verify. Both should return rows after a successful run.
-- ---------------------------------------------------------------------------
select table_name, column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public'
  and table_name in ('chat_messages', 'settings')
order by table_name, ordinal_position;

select * from settings order by key;
