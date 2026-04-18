-- Add title, description, and cleaned-variant storage to transcriptions.
-- Extend search_vec to cover title and description in addition to filename + text.
-- Cleaned variants are intentionally excluded from search_vec — they are largely
-- redundant with the raw transcript, and future embedding-based search will not
-- need them either.

alter table transcriptions
    add column title       text,
    add column description text,
    add column cleaned     jsonb;

-- Generated columns can't be altered in place; drop and recreate.
alter table transcriptions drop column search_vec;

alter table transcriptions
    add column search_vec tsvector generated always as (
        to_tsvector(
            'english',
            coalesce(filename, '') || ' ' ||
            coalesce(title, '') || ' ' ||
            coalesce(description, '') || ' ' ||
            text
        )
    ) stored;

create index transcriptions_search_idx on transcriptions using gin(search_vec);
