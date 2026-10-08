# Business-for-Sale Finder

Finds small businesses for sale that match saved search criteria and posts new matches to Slack. See CLAUDE.md for the design.

## Setup

```bash
uv sync
uv run pytest
```

The database schema is in `db/schema.sql`. Saved searches live in `saved_searches/` as JSON.

## Extraction evaluation

Hand-checked listings live in `data/evaluation/` (gitignored, since they hold real listing text).

```bash
uv run python cli.py build-eval-set          # rebuild eval_set.json from candidates + review marks
uv run --env-file .env python cli.py evaluate  # re-extract and score against the checked values
```

Run `evaluate` after any change to the extraction prompt, schema or model settings.
