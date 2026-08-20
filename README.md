# movie-recs

Recommend movies and TV from a short user description. Stdlib-only Python script
(`movie_recs.py`, ~90 KB) that calls TMDB, Gemini, OMDb, and Wikipedia.

![Example HTML output](./movie_recs_html.png)

## Where to start

| You are… | Read |
|---|---|
| New user — want to run it | [`quickstart.md`](./quickstart.md) |
| Claude Code agent — invoking the skill | [`SKILL.md`](./SKILL.md) |
| Maintainer — want the full reference | [`SKILL.md`](./SKILL.md) |

## What it does

- Keyword lookup (spy / espionage / assassin / time-travel)
- Recommendations seeded by a film, an actor, or a half-remembered plot
- `--html` for a self-contained `watch_{slug}.html` (posters, streaming providers, reviews)
- Optional NotebookLM memory across sessions

## Requirements

- Python 3.10+ (stdlib only; `diskcache` and `anthropic` are optional)
- API keys: TMDB (required), Gemini (required for `--plot-query`), OMDb (optional), Anthropic (optional)

See [`quickstart.md`](./quickstart.md) for how to obtain and set them up.

## Layout

```
movie_recs.py       the script
SKILL.md            full skill reference (Claude Code integration, NotebookLM, quirks)
quickstart.md       1-page setup + first runs
tmdb-api.json       TMDB OpenAPI 3.1 spec (bundled, no secrets)
notebooks.json      UUIDs of the two NotebookLM notebooks (recs + plots)
movie_recs_html.png example HTML output (shown above)
```

## License

[MIT](./LICENSE) — Copyright (c) 2026 riverolfe.