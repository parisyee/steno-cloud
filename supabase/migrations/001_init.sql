-- Enable full-text search
create extension if not exists unaccent;

create table transcriptions (
    id          uuid primary key default gen_random_uuid(),
    filename    text,
    text        text not null,
    search_vec  tsvector generated always as (
                    to_tsvector('english', coalesce(filename, '') || ' ' || text)
                ) stored,
    created_at  timestamptz not null default now()
);

create index transcriptions_search_idx on transcriptions using gin(search_vec);
create index transcriptions_created_at_idx on transcriptions (created_at desc);
