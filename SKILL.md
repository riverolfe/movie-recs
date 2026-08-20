---
name: movie-recs
description: Recommend movies and TV from a short user description. Use when the user asks for movie suggestions, "what should I watch", "movies like X", "spy movies", an actor's best films, or a plot they half-remember ("that one where a kid hides an alien"). Can render results as a browsable HTML page with posters, streaming providers, reviews, and IMDb/Letterboxd/RT links.
allowed-tools: Bash(python3:*), Bash(export:*)
---

# movie-recs

Everything runs through the bundled `movie_recs.py`. Stdlib (urllib) plus one optional dependency, `diskcache` — **do not hand-roll `urllib` calls**, the script already covers every documented flow.

```
SCRIPT=~/.claude/skills/movie-recs/movie_recs.py
```

## Operational rules

**Timeouts.** Pass `timeout=600000` (10 min) on every Bash invocation that runs the script or per-film Wikipedia plot fetches. The script triggers sequential network calls; a cold `--plot-queries "a" "b" "c"` + wiki plots for 10 films can easily exceed the default 120s. Streaming-only `/` doesn't need an extended timeout.

**Step-back on denial.** If a Bash call is denied (auto-mode classifier, permission prompt, or any "this action was denied" response), do NOT retry the same command. One denial = stop and re-plan:
- If a key is being echoed, drop it from the command and use `env VAR=...` prefix or pre-set it via `Mcp` / `Read` of an env file.
- If a `--plot-query` returns hallucinated titles, narrow the phrasing or switch to `--similar-to` rather than re-issuing.
- If a wiki plot fetch returns `None`, present the partial result to the user and ask before retrying — don't loop.
- If the classifier blocks a specific phrasing, rewrite the command (e.g. `python3 -c` wrapper instead of inline `-c`, or `--out /tmp/x.html` to a path that doesn't echo secrets).

Log the denial once, summarize what's missing, and propose an alternative. Never retry the same command more than once.

## Auth

| Var | Required? | Effect if missing |
|-----|-----------|-------------------|
| `TMDB_API_KEY` | **yes** | script exits; register at https://www.themoviedb.org/settings/api (~2 min, free) |
| `OMDB_API_KEY` | no | skips the long plot paragraph + Rotten Tomatoes score on HTML cards |
| `GEMINI_API_KEY` | **yes for `--plot-query`** | default path runs Gemini; without it, exit |
| `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` | no | `--no-google` legacy path falls back to token-overlap scoring instead of LLM-as-judge |
| `notebooklm-mcp` (harness) | no | cross-session memory + plot semantic search; if unavailable, fall back to stateless flow (see `## NotebookLM`) |

Export the key first so the command stays a plain `python3` invocation:

```bash
export TMDB_API_KEY=...
python3 $SCRIPT spy
```

If `TMDB_API_KEY` is unset, stop and ask the user for it — don't guess or proceed.

## Plot cache

Wikipedia plot lookups (`wikipedia_plot`, `wikipedia_plot_from_imdb`) are memoized to `.plot-cache/` beside the script via `diskcache`, no expiry. Measured cold 1.36s → warm 0.0003s.

`diskcache` is optional: if it's not importable the decorator degrades to a no-op and everything still works, just slower. Install with `pip install diskcache`. Bust the cache with `rm -rf ~/.claude/skills/movie-recs/.plot-cache`.

This mostly pays off on `--plot-query`, which fetches one plot per surviving candidate.

## NotebookLM (cross-session memory)

Two notebooks, both created once and reused across all invocations. At the start of each invocation, read `~/.claude/skills/movie-recs/notebooks.json` to resolve the two UUIDs (`{"recs": "...", "plots": "..."}`); if the file is missing or any value is empty, recreate both notebooks and rewrite the file before proceeding. Free tier caps are 50 sources/notebook, 100 notebooks/account — bundling plots keeps one notebook viable for hundreds of films.

### `movie-recs` — session memory

After every successful `python3 $SCRIPT …` run with non-empty results, append one text source:

```
mcp__notebooklm-mcp__source_add(
  notebook_id=<recs_id>,
  source_type="text",
  title=f"{YYYY-MM-DD} — {query-slug}",
  text="""# {query}
- **Date:** {ISO date}
- **Command:** `python3 movie_recs.py {flags}`
- **Top picks:**
  1. **{title}** ({year}) — {one-line why}
  2. …"""
)
```

Limit to top 5. Skip the append if results were empty or the script errored — keeps the notebook signal-rich.

**Query-back** (opt-in): when the user's request implies history awareness ("what did you recommend last month?", "more like the spy one you suggested", "have I already seen everything you'd pick for heist films?"), call `mcp__notebooklm-mcp__notebook_query` on `movie-recs` first and use the answer to dedupe (skip titles from the last 14 days) or seed `--similar-to`. Otherwise the default flow runs the script, appends, and stops — no round-trip.

### `movie-plots` — semantic search

A layer **above** `diskcache`, not a replacement. `diskcache` keeps runtime memoization (warm 0.0003s, cold 1.36s × N); this notebook adds semantic queries `diskcache` cannot answer ("plots where the protagonist is betrayed", "films set on a spaceship").

**Sync trigger — opportunistic, agent-mediated, bundled.** When `--html` is used, the rendered HTML has one `<article class="movie">` per card containing:

- `<h2>{title} <span class="year">({year})</span> …</h2>` — title + year
- `<p class="long-plot">{OMDb long plot, html-escaped}</p>` — full plot, only if `OMDB_API_KEY` is set
- `<p class="overview">{TMDB short blurb, html-escaped}</p>` — always present (≤300 chars)

Read the HTML, decode HTML entities (`html.unescape`), bundle **5–10 films per source** to stay under the 50-source cap:

```
mcp__notebooklm-mcp__source_add(
  notebook_id=<plots_id>,
  source_type="text",
  title=f"{YYYY-MM-DD} — {query-slug} — plots ({N} films)",
  text="""# Plots from {query} ({YYYY-MM-DD})

## {title} ({year})
TMDB id: {id} | IMDb: {imdb_id}

{plot text}

## {title} ({year})
…"""
)
```

Skip sync on non-HTML mode, `--quick`, or error paths (`--no-google` does NOT skip sync when paired with `--html`). Source titles are stable (`{date} — {query-slug} — plots (N films)`) so the agent can dedupe re-syncs.

**Wikipedia upgrade (opt-in, only on user request).** Trigger phrases: "detailed plot", "wiki plot", "wikipedia plot", "full plot", "plot summary", or any phrasing that signals the user wants more than a blurb. When matched, also fetch richer plots by calling `wikipedia_plot_from_imdb(imdb_id)` per film — extract IMDb IDs from the HTML's `<a class="imdb badge" href="https://www.imdb.com/title/tt…/">` URLs. Run all N calls **in parallel** as concurrent `Bash` invocations of `python3 -c "from movie_recs import wikipedia_plot_from_imdb; print(wikipedia_plot_from_imdb('tt…'))"` (warm cache ~0.3ms, cold ~1.5s × N / parallelism). Bundle into a separate source titled `{date} — {query-slug} — plots (wikipedia, N/M films)` so it's distinguishable from the TMDB-overview source. Films that return `None` are skipped silently — the partial source still goes in (don't fail the whole sync for one miss). When the Wikipedia upgrade fires, skip the TMDB-overview sync for this run; the wiki source supersedes it. `diskcache` makes warm-cache re-runs cheap, so the user can ask again tomorrow without re-fetching.

**Query trigger — opt-in by user request.** When the user asks a plot-semantic question ("plots with betrayal", "films where the protagonist is alone"), call `mcp__notebooklm-mcp__notebook_query` on `movie-plots` first and use the answer to seed `--plot-query` / `--similar-to`. Otherwise the existing TMDB similar-to/keyword system handles taste matching.

**Upgrade path if cap is hit** (documented, not built now): split into `movie-plots-A` … `movie-plots-Z` by title initial letter. 26 buckets × ~250–500 films each → ~13k films before any pressure. Free tier has 100 notebooks, plenty of headroom.

**Failure mode**: if `source_add` or `notebook_query` errors (quota, auth, network), log once and continue. The `diskcache` runtime path keeps working even if NotebookLM is unreachable.

## Routing

Read the request, pick one flag. That's the whole job.

| User asks | Command |
|-----------|---------|
| "spy movies" / "assassin films" | `python3 $SCRIPT spy` (also: `espionage`, `assassin`) |
| "...but modern" | `python3 $SCRIPT spy --since 1990` |
| "movies like Inception" | `python3 $SCRIPT --similar-to "Inception"` |
| "shows like The Spy" | `python3 $SCRIPT --similar-to "The Spy" --year 2019` |
| ...and no cartoons | add `--no-animated` |
| "best Liam Neeson films" | `python3 $SCRIPT --actor "Liam Neeson"` |
| "that film where a boy hides an alien" | `python3 $SCRIPT --plot-query "a young boy hides an alien"` |
| two-angled plot: "courtroom thriller AND innocent man proves himself" | `python3 $SCRIPT --plot-queries "courtroom thriller" "innocent man proves himself"` |
| "best Hungarian movies" / "Hungarian classics" / "Norwegian cinema" | `python3 $SCRIPT --plot-query "best Hungarian cinema loved by Hungarians" --nationality HU` (ISO 3166-1 alpha-2: HU, NO, JP, FR, …) |
| bypass the Gemini default and use the legacy TMDB-keyword-pool + Claude rerank pipeline | add `--no-google` |
| any of the above, but nice to look at | add `--html` → writes `watch_{slug}.html` (slug from query), print path as clickable terminal link |
| write HTML to a specific path | add `--out /path/to/file.html` to override the auto-slug |

Other flags: `--reviews N` (default 3, HTML only), `--no-omdb` (skip OMDb even if the key is set), `--nationality CC` (ISO 3166-1 alpha-2 — keeps only films with that country in `origin_country` or matching `original_language`; works with any mode).

Output is plain text (`Title (Year) — Rating/10` + overview) unless `--html`.

## Keyword coverage

`KEYWORDS` in the script is hardcoded to `spy`, `espionage`, `assassin` — anything else exits with `unknown keyword`. For a category outside those three, resolve the TMDB keyword id yourself and add it to the `KEYWORDS` dict:

```bash
curl -s "https://api.themoviedb.org/3/search/keyword?query=heist&api_key=$TMDB_API_KEY"
```

Prefer `--similar-to` with a representative film — it needs no keyword id and usually gives better results than a keyword tag.

## What --plot-query actually does (default: Gemini)

`--plot-query` is the default ranker. The script calls Gemini (3.5 Flash Lite) with your query, asks for ~25 film titles, parses the numbered-list response, then resolves each title via TMDB `/search/movie`. Hallucinated titles that TMDB can't find are dropped — the count is printed on the CLI as `[gemini] N of M titles not found on TMDB (dropped): [...]`.

**Cost:** one Gemini API call + ~25 TMDB searches. Measured ~17s end-to-end vs ~55s for the legacy pipeline, with materially better results.

**Gotchas:**
- Gemini occasionally returns TV specials or shorts as film titles (`Veep: The Complete Series` showed up once and was correctly dropped after TMDB search failed)
- `GEMINI_API_KEY` is required — exit with a clear error if unset
- Ranking is Gemini's order, not LLM-judged relevance

## Multi-query: --plot-queries "a" "b" "c"

Pass each phrasing as a separate arg. The script runs one Gemini call per phrasing and **unions** the results, deduping by TMDB id. Use when one sentence underspecifies — e.g. "political satire, losers" + "idiots in power" + "dumb politicians, sharp satire" — three angles that fetch different films under one umbrella.

**Cost:** `len(queries) × one Gemini call + len(queries) × ~25 TMDB searches`. Three phrasings on this skill: ~25s. Query order matters — list your broadest phrasing first; that wins ties in the dedupe.

## --no-google: legacy pipeline

Adds `--no-google` to fall back to the older TMDB-keyword-pool + LLM rerank path. Use this when:

- you want LLM-judged relevance scoring (not Gemini's text response ordering)
- `GEMINI_API_KEY` is unavailable
- Gemini is returning off-target results for a specific query and the keyword pipeline hits harder

It works like the previous pipeline:
- Builds a pool: top rated + popular + 5 pages of sci-fi + 2 pages per matched keyword
- Multi-gram matching prefers longest phrases ("time travel" beats "time"); each word is also tried in common de-inflected forms
- Fetches Wikipedia plot per survivor, scores with Claude (the local LLM via `ANTHROPIC_API_KEY`)
- Floor at `>4` drops films the LLM scored 1-4 as "incidental mention" or "side element"

## HTML output

`--html` writes a self-contained `watch_{slug}.html` (auto-named from your query, override with `--out`): posters, provider badges (flatrate only — no rent/buy noise), a sidebar of 10 streaming-provider checkboxes that filter the grid client-side, TMDB reviews, keywords, and links out to IMDb, Letterboxd, JustWatch, and RT. Titles are HTML-escaped.

**Provider filter default: unchecked** — all 10 cards render on page load. Check a provider to narrow to its catalog.

Rotten Tomatoes scores appear on **films only** — OMDb doesn't carry RT ratings for TV series, so those cards skip the badge. Long plots come through for both.

Without `--html`, results are printed to the terminal as a plain text shortlist. The IMDb titles are wrapped in `OSC 8` ANSI hyperlinks so iTerm2, Terminal.app (recent), kitty, and Ghostty users can ctrl-click to open the IMDb page; older terminals print the title only.

## Reference

`tmdb-api.json` (2.9 MB) is the full TMDB OpenAPI spec, bundled for offline parameter lookup. Nothing loads it at runtime — grep it only when adding a filter the script doesn't expose yet.

## Skip-list

The script already has: pagination, provider filtering, reviews, OMDb/Wikipedia enrichment, Gemini + LLM scoring, a diskcache-backed plot cache, multi-query union (Gemini and legacy), thin-list CF rescue, `--google`/`--no-google`/`--quick` modes. Don't add further caching layers, retry/backoff beyond the existing one-shot retry, multi-language output, or fuzzy title matching unless asked. Don't rewrite the script inline — extend it in place, and keep the `__main__` smoke asserts passing (`python3 $SCRIPT --help` runs them).

NotebookLM persistence (`## NotebookLM`) is intentionally additive and agent-side — it lives in `SKILL.md` and `notebooks.json`, not in `movie_recs.py`. Don't move NotebookLM calls into the script.

