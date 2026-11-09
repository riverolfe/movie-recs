"""Pure query matching and ranking orchestration for movie_recs.

The backend is the loaded movie_recs module (or __main__ for direct CLI use). It
provides HTTP, cache, and enrichment services; this module owns the query scoring
and ranking flow without importing the CLI module.
"""
from __future__ import annotations

import itertools
import re
from typing import List

STOPWORDS = {"a", "an", "the", "to", "has", "have", "had", "is", "are", "was", "were",
                "be", "been", "being", "in", "on", "at", "by", "for", "of", "with",
                "and", "or", "but", "if", "then", "so", "as", "it", "its", "this",
                "that", "from", "his", "her", "their", "they", "he", "she", "we", "you"}

def keywords_in_query(query: str, catalog: dict, limit: int = 4) -> List[int]:
    """Match a free-text query against the keyword catalog, longest phrase first.

    Scans contiguous word n-grams (longest first) so "time travel" beats "time", and drops
    n-grams that are entirely stopwords. Returns up to `limit` keyword ids, best match first.

    Each word is tried both verbatim and de-inflected ("traveling" → "travel", "dimensions" →
    "dimension") because catalog entries are singular base forms while queries are prose.
    """
    if not catalog:
        return []

    def variants(word: str) -> List[str]:
        """Word plus plausible base forms, longest-shot last. Order matters — verbatim wins."""
        out = [word]
        # Crude de-inflection: traveling→travel, dimensions→dimension, twists→twist, etc.
        # Not a stemmer — we generate candidates and let the catalog decide which exists.
        for suffix, repl in (("lling", "l"), ("ing", ""), ("ies", "y"), ("es", ""), ("s", "")):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                out.append(word[: len(word) - len(suffix)] + repl)
        return out

    words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
    found: List[int] = []
    seen: set = set()
    used: set = set()  # Word positions already claimed by a longer match.
    # 3-grams down to 1-grams: longest match wins ("multiple dimensions" beats bare "dimensions").
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            span = range(i, i + size)
            # Skip spans overlapping an earlier (longer) hit; otherwise "time travel"
            # also yields bare "time" and dilutes the pool with a near-useless keyword.
            if any(p in used for p in span):
                continue
            gram_words = words[i:i + size]
            if all(w in STOPWORDS for w in gram_words):
                continue
            # Cartesian product over per-word variants — 3 words × ~2 variants = 8 lookups,
            # all dict hits, so cost is nil next to one API call.
            for combo in itertools.product(*(variants(w) for w in gram_words)):
                kid = catalog.get(" ".join(combo))
                if kid and kid not in seen:
                    seen.add(kid)
                    found.append(kid)
                    used.update(span)
                    if len(found) >= limit:
                        return found
                    break  # One id per n-gram position — don't also match its variants.
    return found

def score_plot(plot: str, query: str) -> int:
    """Score a plot text against a query: phrase substring bonus + content-token overlap (stemmed)."""
    plot_lower = plot.lower()
    query_lower = query.lower()
    # Phrase substring match is the strongest signal; falls back to stemmed content-token overlap.
    # Stemmer is "drop trailing 's'" — handles alien/aliens, boy/boys; full Porter is overkill here.
    stem = lambda w: w[:-1] if w.endswith("s") and len(w) > 3 else w
    score = 10 if query_lower in plot_lower else 0
    # Tokenize to [a-z0-9]+ so punctuation can't glue a query word into a shape no plot uses:
    # "alien," must still match "alien", or a comma silently costs the film a match (and the
    # stopword check misses "the," the same way).
    query_content = {stem(w) for w in re.findall(r"[a-z0-9]+", query_lower) if w not in STOPWORDS}
    plot_stems = {stem(w) for w in re.findall(r"[a-z0-9]+", plot_lower)}
    score += len(query_content & plot_stems) * 2
    return score


def search(queries: List[str], key: str, *, quick: bool, backend) -> list:
    """Build, score, rescue, and rank the candidate pool for one or more queries."""
    backend.reset_llm_usage()
    pool, _catalog = backend._build_plot_pool(queries, key)
    if not pool:
        return []

    prefilter = [
        movie for movie in pool
        if any(backend._score_plot(movie.get("overview", ""), query) > 0 for query in queries)
    ]
    use_llm = (not quick) and bool(
        backend.os.environ.get("ANTHROPIC_API_KEY") or backend.os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
    floor = backend.QUICK_FLOOR if quick else backend.STRICT_FLOOR
    scored = []
    for movie in prefilter:
        text = backend._quick_text(movie, key) if quick else backend._full_text(movie, key)
        if use_llm:
            scores = [backend._llm_score_plot(text, query) for query in queries]
            scores = [score if score >= 0 else backend._score_plot(text, query)
                      for score, query in zip(scores, queries)]
        else:
            scores = [backend._score_plot(text, query) for query in queries]
        best = max(scores)
        if best > floor:
            scored.append({**movie, "_score": best})

    rescued = backend.thin_rescue(scored, queries, key, quick=quick, seeds=prefilter)
    return backend.apply_blockbuster_quota(rescued)
