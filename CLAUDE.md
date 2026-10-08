# Business-for-Sale Finder

## Purpose
Find small businesses for sale that match a buyer's criteria across several listing sites, and send new matches to Slack (and optionally email) on a weekly schedule. Built for one user today, but structured so it can later become a multi-user website with a proper front end.

## Coding conventions (important)
- **Readability matters more than brevity.** Prefer clear, explicit code over clever or compact code.
  - Descriptive names for variables, functions, and modules — no cryptic abbreviations.
  - Small, single-purpose functions with docstrings explaining what and why.
  - Type hints everywhere. Use Pydantic models for structured data.
  - Avoid dense one-liners, deeply nested comprehensions, and "magic." A few extra lines that read clearly are always preferred.
  - Comments explain *why*, not just *what*.
- Python 3.11+.
- Secrets and environment-specific values (API keys, webhook URLs, inbox credentials, database URL) come from environment variables, never hardcoded.
- Keep the core logic free of any interface code (CLI, web, Slack). Interfaces are thin wrappers that call the core.

## Testing (important)
- **Test-driven development.** For every bug fix or new behavior, write the test first, run it and show that it fails for the right reason, then write the code that makes it pass.
- **Do not change existing tests while fixing a bug or building a feature.** Existing tests are the rules of the game; editing them to make new code pass hides regressions. If an existing test seems wrong or blocks a change, stop and ask the user before touching it. This includes shared test helpers and fixtures.
- Keep the suite small. Prefer a few tests of real behavior over many tests of small variations, and avoid complicated mocking structures; ask before adding a new fake or mock.

## Phase 1 is a pipeline, not an agent
Phase 1 is a deterministic pipeline. Code controls the sequence of steps; a model is used only for the nuanced parts of data extraction. Do **not** use an agent framework or MCP for Phase 1.

Weekly flow:
1. **Ingest** — collect listings broadly from each source (all listings in the target states and price band), not per-search.
2. **Dedupe** — skip listings already stored.
3. **Extract** — parse structured fields with plain code where possible; use Claude Haiku only for fields buried in description text.
4. **Store** — save every listing (raw text + extracted fields), not just matches.
5. **Match** — run each saved search's criteria against stored listings.
6. **Notify** — post new matches to Slack (and optionally email).

Separating ingestion from matching is deliberate: a future website collects listings once and matches them against many users' saved searches.

## Search criteria
Criteria are data, not code. A "saved search" is a Pydantic model stored in the database (even though there is only one today):
- Business type
- Minimum revenue
- Minimum SDE (seller's discretionary earnings)
- Asking price — target is about $1M; support a configurable range around it (e.g. $600K–$1.4M)
- Target city — currently Petaluma, CA
- Allowable distance from target city (miles)
- Business age (minimum years in business)

Each saved search also has a notification target (Slack channel / email address).

Other exact values are still to be filled in by the user.

## Target geography
The current target city is Petaluma, CA (Sonoma County), with a 250-mile radius. Ingest by **state**, not by city or county: collect every listing in California, Oregon and Nevada (the list of states is configuration, not code), then let the distance filter make the precise cut. A county list would miss businesses inside the radius (e.g. Sacramento, Fresno, Reno), and the extra listings are cheap because extraction only runs on new ones. The ingest states belong to the pipeline's configuration, not to a saved search, because ingestion is shared by every saved search.

## Sources
| Priority | Source | Access method | Notes |
|---|---|---|---|
| — | Transworld (tworld.com) | **Blocked; not fetched** | Search and detail pages return a Cloudflare bot check to plain HTTP requests (checked October 2026). Do not try to get past it. Many Transworld listings reach us through BizBen instead (see below). |
| 1 | BizBen (bizben.com) | Direct public fetch (built: `sources/bizben.py`) | Mostly California, heavy Bay Area / Northern California volume, but also has Oregon and Nevada pages. Transworld syndicates many listings here (URL paths containing `tw:`, tworld.com contacts), and some Sunbelt brokers post here too, so expect cross-source duplicates. The adapter pages through each state page: `california-state-ca` (about 3,100 listings, 78 pages of 40), `oregon-or` (about 40) and `nevada-nv` (about 90), as `/business-for-sale/<slug>?page=N`. Some posts have a `states` list instead of `state`; multi-state posts get no single state and reads the listing JSON that Next.js embeds in the page (`categoryPageResult`), not the HTML cards. robots.txt disallows `?offset=` and `?orderby=`, so paginate with `?page=` only. BizBen uses 0 for blank money fields; treat 0 as unknown. Listings often include asking price, cash flow, gross revenue, year established and employee counts. Some listings give only "California" as location — tag these as approximate/unknown. Also offers saved searches with daily emails as a fallback. |
| 2 | Sunbelt Network (sunbeltnetwork.com) | Direct public fetch | Result pages with clean URLs (e.g. `/business-search/business-results/county-sonoma-state-california/`); use state-level pages if the site has them, otherwise loop over each state's counties. Search cards show asking price, gross revenue and cash flow. |
| 3 | BusinessesForSale.com | RSS feed | Public RSS feed of new listings; filter to the target states. Cheapest integration. Also has email alerts. |
| 4 | BizBuySell | Saved-search email alerts | Do not scrape or automate logins. Read alert emails from a dedicated inbox. Most small regional brokers syndicate here, so this covers many broker sites indirectly. |
| 5 | BizScout | Saved-search email alerts | Listings are behind a login. Use the account's own alerts/matching, delivered to the dedicated inbox. |
| 6 | SMB Deal Hunter newsletter | Email (newsletter) | Off-market deals, often trades and home services. Subscribe the dedicated inbox and extract listings from each issue with Claude. |
| Later | Murphy Business, BusinessMart | Direct public fetch / RSS | Some California listings, lower volume. Optional. |

Excluded on purpose: LoopNet (business listings duplicate BizBuySell — same parent company), VR Business Brokers (robots.txt disallows automated access), online-only marketplaces (Flippa, Exchange Marketplace, Website Closers, Quiet Light), healthcare-only brokers, and regional brokers outside the target states.

Be polite with fetches: low frequency, identifiable user agent, respect each site's terms of use. All web adapters fetch through `sources/polite_http.py` (10 seconds between requests by default; user agent from `BUSINESS_FINDER_USER_AGENT`). Never try to get past a bot check; `BlockedByBotCheckError` means stop. Check robots.txt before building a new adapter.

## Architecture
```
business_finder/
    models.py          # Pydantic models: Listing, ListingFields, SavedSearch, Match
    sources/           # One adapter per site; every adapter returns the same Listing shape
        transworld.py
        bizben.py
        sunbelt.py
        businessesforsale_rss.py
        email_alerts.py   # Parses BizBuySell / BizScout alerts and the SMB Deal Hunter newsletter from the inbox (Gmail API or IMAP)
    extraction/
        extractor.py      # Extractor interface: extract(listing_text) -> ListingFields
        claude_extractor.py
        prompts/          # Provider-neutral prompt and JSON schema files
    matching.py         # Applies SavedSearch criteria to stored listings
    geography.py        # Geocoding and distance calculation
    storage.py          # Database access
    notifiers/
        slack.py
        email.py
    pipeline.py         # Runs the weekly flow end to end
cli.py                  # Thin CLI wrapper, e.g. `python cli.py run`
```

## Extraction
- Use an `Extractor` interface so the model backend is swappable. First implementation: `ClaudeExtractor` using Claude Haiku via direct API calls (model name `claude-haiku-5-5`). An `OllamaExtractor` may be added later.
- Provider and model name come from configuration, not code.
- Prompt and JSON schema live in their own files and are provider-neutral.
- Every extracted field is nullable. The prompt must instruct the model to return null when a value is not stated, never guess. Validate all output with Pydantic.
- Optionally return the source snippet for each extracted value so results can be spot-checked.
- Business type needs the model too: sites use their own wording ("janitorial", "maid service"), and whether a home-service business needs a contractor's license is a judgment call. Plain string matching against the saved search's `business_types` is not enough.
- Only extract new listings (dedupe first) to keep cost low.
- Skip Claude extraction for listings whose structured asking price is above the highest `asking_price_max` of any saved search (currently $1.2M). They are still stored with their plain-code fields. Listings with no stated asking price are extracted.
- Build an evaluation set of 20–30 hand-checked listings early, to measure extraction accuracy and to compare models later.

## Geography
Location precision varies by source: city, county, state, or region. Geocode to the most specific level available, compute the distance from the center city, and tag each result as `exact`, `approximate`, or `unknown`. Never silently drop listings with vague locations; surface them with the tag.

## Storage
Postgres on Supabase (free tier) from day one, so scheduled runs have persistent state and a future website can read the same database. Store for every listing: source, URL, raw text, extracted fields, first-seen date, last-seen date, and status (e.g. new / interested / passed / contacted).

## Notifications
- **Slack:** Create a Slack app now and use its incoming webhook. This keeps the upgrade path open for Phase 2 interactivity (buttons, slash commands).
- **Email (optional):** Send via SMTP or a service such as Resend or SendGrid.
- Each Slack message should include the listing's stable internal ID so Phase 2 features (e.g. a "deep dive" button) can reference it.
- Show where each listing sits relative to the target price, plus a one-line summary of why it matched.

## Deployment path
1. Run locally from the CLI while building and tuning.
2. Move the weekly run to GitHub Actions on a cron schedule, with a manual "Run workflow" trigger. Secrets stored as GitHub secrets.
3. Optionally add a small local Gradio page for editing criteria and browsing results.
4. Later: a hosted website (e.g. FastAPI backend + front end) reusing the same core package and database.

## Build order
1. Pydantic models and the database schema.
2. BizBen adapter (done) + `ClaudeExtractor` + storage — get real data flowing end to end into the database. (Transworld was planned here but blocks automated access.)
3. Matching and geography.
4. Slack notifier → first complete end-to-end run.
5. Sunbelt adapter and BusinessesForSale.com RSS adapter.
6. Email ingestion for BizBuySell and BizScout alerts and the SMB Deal Hunter newsletter.
7. GitHub Actions schedule.
8. Optional: Murphy Business and BusinessMart.

## Phase 2 (later, not now)
These live in Slack and are where an agent or MCP adds real value:
- **Deep dive on a listing:** from a Slack message, an agent researches the business (website, reviews, time on market, red flags) and reports back.
- **Chat with the listings database:** expose the database through an MCP server so it can be queried in natural language (e.g. "anything new near San Diego with over $250K SDE?").

## Future multi-user considerations
- Email-alert ingestion is tied to one account's saved searches. In a multi-user product it works only as a shared, broad ingestion feed.
- Republishing other sites' listing content on a public website carries legal risk. Prefer linking out over displaying full listings, and explore data partnerships or licensing before productizing.
