-- Database schema for the Business-for-Sale Finder.
--
-- Written for Postgres on Supabase: paste it into the Supabase SQL editor
-- to create the tables. Columns mirror the Pydantic models in
-- src/business_finder/models.py; keep the two in sync.
--
-- Allowed values are CHECK constraints rather than Postgres ENUM types,
-- because adding a new source or status to a CHECK is a one-line change.

create table if not exists listings (
    id                            uuid primary key default gen_random_uuid(),
    source                        text not null check (source in (
                                      'transworld', 'bizben', 'sunbelt', 'businessesforsale',
                                      'bizbuysell', 'bizscout', 'smb_deal_hunter',
                                      'murphy_business', 'businessmart')),
    source_listing_id             text not null,
    url                           text not null,
    title                         text,
    raw_text                      text not null,
    raw_data                      jsonb,

    -- Extracted fields (ListingFields). Null means "the listing did not say".
    business_name                 text,
    business_type                 text,
    requires_contractor_license   boolean,
    asking_price                  bigint check (asking_price >= 0),
    gross_revenue                 bigint check (gross_revenue >= 0),
    seller_discretionary_earnings bigint check (seller_discretionary_earnings >= 0),
    down_payment                  bigint check (down_payment >= 0),
    year_established              integer check (year_established between 1800 and 2100),
    employee_count                integer check (employee_count >= 0),
    city                          text,
    county                        text,
    state                         text,
    states                        text[] not null default '{}',
    region                        text,
    conflict_notes                text[] not null default '{}',
    source_snippets               jsonb not null default '{}'::jsonb,

    -- Filled in by the geography step.
    location_precision            text not null default 'unknown'
                                      check (location_precision in ('exact', 'approximate', 'unknown')),
    latitude                      double precision,
    longitude                     double precision,
    distance_miles                double precision,

    first_seen_at                 timestamptz not null default now(),
    last_seen_at                  timestamptz not null default now(),
    status                        text not null default 'new'
                                      check (status in ('new', 'interested', 'passed', 'contacted')),

    -- Deduplication: a listing is the same listing if the site's ID matches.
    unique (source, source_listing_id)
);

create table if not exists saved_searches (
    id                                    uuid primary key default gen_random_uuid(),
    name                                  text not null,
    business_types                        text[] not null,
    business_type_guidance                text,
    minimum_revenue                       bigint check (minimum_revenue >= 0),
    minimum_seller_discretionary_earnings bigint check (minimum_seller_discretionary_earnings >= 0),
    asking_price_min                      bigint check (asking_price_min >= 0),
    asking_price_max                      bigint check (asking_price_max >= 0),
    asking_price_target                   bigint check (asking_price_target >= 0),
    minimum_years_in_business             integer check (minimum_years_in_business >= 0),
    target_city                           text not null,
    target_state                          text not null,
    max_distance_miles                    integer check (max_distance_miles > 0),
    notify_slack_channel                  text,
    notify_email_address                  text,
    created_at                            timestamptz not null default now(),

    check (asking_price_min is null or asking_price_max is null
           or asking_price_min <= asking_price_max)
);

create table if not exists matches (
    id               uuid primary key default gen_random_uuid(),
    saved_search_id  uuid not null references saved_searches (id) on delete cascade,
    listing_id       uuid not null references listings (id) on delete cascade,
    match_summary    text not null,
    matched_at       timestamptz not null default now(),
    -- Null until the match has been sent, so failed notifications are retried.
    notified_at      timestamptz,

    -- A listing matches a given saved search at most once.
    unique (saved_search_id, listing_id)
);

-- The notify step looks up matches that have not been sent yet.
create index if not exists matches_not_yet_notified
    on matches (saved_search_id)
    where notified_at is null;
