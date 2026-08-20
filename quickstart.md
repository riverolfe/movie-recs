# movie-recs — quickstart

Skill: `~/.claude/skills/movie-recs/` · script: `movie_recs.py` (stdlib only, ~90 KB)
Full reference: [`SKILL.md`](./SKILL.md). Read this first, then ask the agent.

## 1. Get the three keys

| Service | Why | Where | Required? |
|---|---|---|---|
| **TMDB** v3 key | every request | https://www.themoviedb.org/settings/api → "Create API key (v3 auth)" | **yes** |
| **Gemini** API key | `--plot-query` reranker (~25 titles) | https://aistudio.google.com/app/apikey | **yes for `--plot-query`** |
| **OMDb** key | long plot + RT score on HTML cards | https://www.omdbapi.com/apikey.aspx (free w/ email verify) | no |
| **Anthropic** key | only with `--no-google` (legacy LLM reranker) | `claude.ai` settings or your env | no |

Export them once per shell session:

```bash
export TMDB_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxx
export GEMINI_API_KEY=AIzaSy...
# optional:
export OMDB_API_KEY=xxxxxxx
```

`movie_recs.py` exits with a clear error if a required key is missing — no silent fallback.

## 2. Pick a flag and run

```bash
cd ~/.claude/skills/movie-recs
python3 movie_recs.py spy                          # 3 keywords baked in: spy / espionage / assassin
python3 movie_recs.py --since 1990 spy             # modern only
python3 movie_recs.py --similar-to "Inception"     # recommendation by seed title
python3 movie_recs.py --actor "Liam Neeson"        # filmography
python3 movie_recs.py --plot-query "a boy hides an alien"   # Gemini ranks ~25 candidates
python3 movie_recs.py --plot-queries "satire" "idiots in power"   # union, dedupe by id
```

Add `--html` to any of the above for a self-contained `watch_{slug}.html` (posters, provider filter, reviews, IMDb/RT links).

```bash
python3 movie_recs.py --html --similar-to "Inception" --out ~/inception-like.html
```

Long batch runs (cold plots, multi-query, wiki upgrade) need `timeout=600000` on the Bash call.

## 3. Optional: NotebookLM memory

If `notebooklm-mcp` is wired into your harness, the agent auto-syncs successful runs to two notebooks (`recs`, `plots`) so future requests can dedupe past picks and search plots semantically. No extra config — see `SKILL.md` → "## NotebookLM".

## 4. Sanity check

```bash
python3 movie_recs.py --help        # prints usage; the __main__ smoke asserts run too
```

> ⚠ **Heads-up:** the multi-query dedupe assert at `movie_recs.py:1403` currently fails against live TMDB (it expects one title the test doesn't get back). Unrelated to the rename — flag for a follow-up.

## What's where

```
SKILL.md       — full reference (routing, cache, NotebookLM, quirks)
quickstart.md  — this file
movie_recs.py  — the script
tmdb-api.json  — public TMDB OpenAPI spec (offline parameter lookup)
notebooks.json — UUIDs of the two NotebookLM notebooks
.plot-cache/   — diskcache memo for Wikipedia plot fetches
```

## Skip-list (per SKILL.md)

Don't add: extra caching layers, retry/backoff, multi-language output, fuzzy title matching, parallel NotebookLM calls, or moving NotebookLM persistence into the script.