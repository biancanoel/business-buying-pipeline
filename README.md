# Business-for-Sale Finder

Finds small businesses for sale that match saved search criteria and posts new matches to Slack. See CLAUDE.md for the design.

## Setup

```bash
uv sync
uv run pytest
```

The database schema is in `db/schema.sql`. Saved searches live in `saved_searches/` as JSON.
