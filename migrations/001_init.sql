create extension if not exists vector;
create extension if not exists pgcrypto;

create table if not exists users (
  id uuid primary key default gen_random_uuid(),
  email text not null,                       -- stored lowercased and stripped
  password_hash text not null,               -- argon2id encoded string
  created_at timestamptz not null default now(),
  last_login_at timestamptz
);
create unique index if not exists users_email_idx on users(lower(email));

create table if not exists auth_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  token_hash text not null unique,           -- sha256 hex of the opaque cookie token; never store the token
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  expires_at timestamptz not null,
  user_agent text, ip text
);
create index if not exists auth_sessions_user_idx on auth_sessions(user_id);

create table if not exists documents (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid references users(id) on delete cascade,   -- null = public seed document
  filename text not null,
  mime text not null,
  size_bytes bigint not null,
  sha256 text not null,
  status text not null default 'queued',    -- queued|parsing|chunking|embedding|ready|failed
  progress int not null default 0,          -- 0..100
  error text,
  page_count int, chunk_count int,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  expires_at timestamptz                    -- null for public seed docs, or when DOC_TTL_DAYS=0
);
create index if not exists documents_owner_idx on documents(owner_id);

create table if not exists blobs (
  document_id uuid primary key references documents(id) on delete cascade,
  content_type text not null,
  data bytea not null
);

create table if not exists chunks (
  id bigserial primary key,
  document_id uuid not null references documents(id) on delete cascade,
  owner_id uuid,                            -- denormalised from documents (null = public seed); used for defence-in-depth filtering
  chunk_index int not null,
  page int,                                 -- 1-based, null for unpaged formats
  content text not null,
  embedding vector(768) not null,
  tsv tsvector generated always as (to_tsvector('english', content)) stored,
  unique (document_id, chunk_index)
);
create index if not exists chunks_doc_idx on chunks(document_id);
-- No ANN index in v1 (see README "Design decisions").

create table if not exists jobs (
  id bigserial primary key,
  kind text not null,                       -- 'ingest_document'
  payload jsonb not null,
  status text not null default 'queued',    -- queued|running|succeeded|failed
  attempts int not null default 0,
  max_attempts int not null default 3,
  run_after timestamptz not null default now(),
  locked_at timestamptz, locked_by text,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists jobs_claim_idx on jobs(status, run_after);

create table if not exists threads (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references users(id) on delete cascade,
  created_at timestamptz not null default now()
);
create table if not exists messages (
  id bigserial primary key,
  thread_id uuid not null references threads(id) on delete cascade,
  role text not null check (role in ('user','assistant')),
  content text not null,
  doc_ids uuid[],
  citations jsonb,
  trace jsonb,                              -- graph steps, timings, rewritten query, grounding verdict
  created_at timestamptz not null default now()
);
