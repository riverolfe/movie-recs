#!/usr/bin/env python3
"""Recommend movies from a TMDB keyword. Stdlib only (urllib).

Set TMDB_API_KEY in your shell, then:
  python movie_recs.py [keyword]
  python movie_recs.py --html [keyword]                                 → writes watch_{slug}.html
  python movie_recs.py --since 1980 [keyword]                           → filter by release year
  python movie_recs.py --similar-to "The Spy" --year 2019 --no-animated → TV series recommendations
  python movie_recs.py --actor "Liam Neeson"                            → actor filmography
  python movie_recs.py --plot-query "a young boy fights aliens"         → rank popular pool by plot text
  python movie_recs.py --plot-query "best Hungarian cinema" --nationality HU   → filter to a single country's films

Optional: set OMDB_API_KEY for a longer plot paragraph (OMDb `plot=full`) per movie.
Keywords: spy (default), espionage, assassin, time-travel.
"""
import argparse
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass, field
from typing import Any, List, Optional

from movie_recs_html import (
    TOP_PROVIDERS,
    _html_template,
    _keywords_block,
    _long_plot_block,
    _movie_card,
    _provider_badges,
    _review_block,
    _sidebar_html,
    poster_img,
)
from movie_recs_plot import (
    STOPWORDS as _STOPWORDS,
    keywords_in_query,
    score_plot as _score_plot,
    search as _plot_search,
)

API = "https://api.themoviedb.org/3"
IMDB_URL = "https://www.imdb.com/title/{}/"

KEYWORDS = {"spy": 470, "espionage": 158431, "assassin": 1701, "time-travel": 4379}



@dataclass
class AuthorDetails:
    """Reviewer's profile metadata — currently only their personal 1-10 rating."""
    rating: Optional[float] = None

    @classmethod
    def from_api(cls, data: Optional[dict]) -> "AuthorDetails":
        if not data:
            return cls()
        return cls(rating=data.get("rating"))


@dataclass
class ProviderEntry:
    """One streaming service offering a movie (e.g., Netflix under flatrate)."""
    provider_name: str

    @classmethod
    def from_api(cls, data: dict) -> "ProviderEntry":
        return cls(provider_name=data["provider_name"])


@dataclass
class Provider:
    """Watch providers for a title in a region, split by acquisition type."""
    flatrate: List[ProviderEntry] = field(default_factory=list)
    rent: List[ProviderEntry] = field(default_factory=list)
    buy: List[ProviderEntry] = field(default_factory=list)

    @classmethod
    def from_api(cls, data: Optional[dict]) -> "Provider":
        if not data:
            return cls()
        return cls(
            flatrate=[ProviderEntry.from_api(p) for p in data.get("flatrate", [])],
            rent=[ProviderEntry.from_api(p) for p in data.get("rent", [])],
            buy=[ProviderEntry.from_api(p) for p in data.get("buy", [])],
        )

    def has_flatrate(self) -> bool:
        return bool(self.flatrate)


@dataclass
class Review:
    """A user-submitted TMDB review with optional rating."""
    author: str = "?"
    content: str = ""
    author_details: Optional[AuthorDetails] = None

    @classmethod
    def from_api(cls, data: dict) -> "Review":
        return cls(
            author=data.get("author", "?"),
            content=data.get("content") or "",
            author_details=AuthorDetails.from_api(data.get("author_details")),
        )


@dataclass
class Movie:
    """TMDB movie or TV series summary. Movies use `title`/`release_date`; TV shows use `name`/`first_air_date`."""
    id: int
    title: Optional[str] = None  # movies have `title`; TV shows have `name`
    name: Optional[str] = None
    release_date: Optional[str] = None
    first_air_date: Optional[str] = None
    vote_average: float = 0.0
    vote_count: int = 0
    overview: str = ""
    poster_path: Optional[str] = None
    genre_ids: List[int] = field(default_factory=list)

    @property
    def display_title(self) -> str:
        """Display title for both movies and TV shows."""
        return self.title or self.name or "?"

    @property
    def display_year(self) -> str:
        """4-digit year from `release_date` or `first_air_date`."""
        return (self.release_date or self.first_air_date or "")[:4]

    @property
    def is_tv(self) -> bool:
        """True when this is a TV series (has `name` but no `title`)."""
        return self.title is None and self.name is not None

    @classmethod
    def from_api(cls, data: dict) -> "Movie":
        return cls(
            id=data["id"],
            title=data.get("title"),
            name=data.get("name"),
            release_date=data.get("release_date"),
            first_air_date=data.get("first_air_date"),
            vote_average=float(data.get("vote_average") or 0),
            vote_count=int(data.get("vote_count") or 0),
            overview=data.get("overview") or "",
            poster_path=data.get("poster_path"),
            genre_ids=list(data.get("genre_ids") or []),
        )


@dataclass
class Row:
    """One movie card's worth of pre-fetched data, ready for HTML rendering."""
    movie: Movie
    media_type: str
    poster_html: str
    imdb_url: Optional[str]
    providers: Provider
    reviews: List[Review]
    keywords: List[str] = field(default_factory=list)
    long_plot: Optional[str] = None  # OMDb `plot=full` if fetched, else None
    rt_score: Optional[str] = None  # OMDb Rotten Tomatoes score (e.g. "85%"), else None
    rt_url: str = ""               # RT search URL (empty if omdb_key not set)
    lb_url: str = ""               # Letterboxd search URL (always populated)
    jw_url: str = ""               # JustWatch search URL (always populated)


def get(path: str, params: dict, *, key: str) -> Any:
    """Issue a GET against the TMDB v3 API; exit with a readable message on auth/network failure."""
    params["api_key"] = key
    qs = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(f"{API}{path}?{qs}", timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"TMDB {e.code}: {e.reason}. Check TMDB_API_KEY.")
    except urllib.error.URLError as e:
        sys.exit(f"TMDB unreachable: {e.reason}")


def shortlist(movies: list, n: int = 10) -> list:
    """Top-N movies by vote_average. Rating-only sort; popularity-weight when recall > precision."""
    return sorted(movies, key=lambda m: m.get("vote_average") or 0, reverse=True)[:n]


def discover(keyword: str, key: str, min_votes: int = 200, since: Optional[int] = None) -> list:
    """Find films tagged with a keyword (spy/espionage/assassin), filtered by minimum votes and optional release date."""
    params = {
        "with_keywords": KEYWORDS[keyword],
        "sort_by": "vote_average.desc",
        "vote_count.gte": min_votes,
    }
    if since:
        params["primary_release_date.gte"] = f"{since}-01-01"
    return get("/discover/movie", params, key=key)["results"]


def recommendations_for(title: str, key: str) -> list:
    """Return TMDB's collaborative-filtering recommendations for a movie title."""
    res = get("/search/movie", {"query": title}, key=key).get("results", [])
    if not res:
        return []
    movie_id = res[0]["id"]
    return get(f"/movie/{movie_id}/recommendations", {}, key=key).get("results", [])


def recommendations_for_tv(title: str, year: int, key: str, exclude_animated: bool = False) -> list:
    """Return CF recommendations for a TV series, optionally dropping Animation (genre 16)."""
    res = get("/search/tv", {"query": title, "first_air_date_year": year}, key=key).get("results", [])
    if not res:
        return []
    tv_id = res[0]["id"]
    recs = get(f"/tv/{tv_id}/recommendations", {}, key=key).get("results", [])
    if exclude_animated:
        # TMDB genre id 16 = Animation. /recommendations exposes no genre filter, so drop client-side.
        recs = [r for r in recs if 16 not in (r.get("genre_ids") or [])]
    return recs


def movies_by_actor(name: str, key: str, min_votes: int = 200) -> list:
    """Return an actor's filmography (cast), filtered to films with enough votes to rate reliably."""
    res = get("/search/person", {"query": name}, key=key).get("results", [])
    if not res:
        return []
    person_id = res[0]["id"]
    credits = get(f"/person/{person_id}/movie_credits", {}, key=key)
    cast = [m for m in credits.get("cast", []) if (m.get("vote_count") or 0) >= min_votes]
    return sorted(cast, key=lambda m: m.get("vote_average", 0), reverse=True)


def print_movies(movies: list, key: str = None) -> None:
    """Print the shortlist to stdout with clickable IMDb titles via ANSI hyperlinks.

    ANSI escape: ESC[8;;URLESC\\textESC[8;;ESC\\. iTerm2, recent Terminal.app, kitty, Ghostty
    render these as ctrl-click links; older terminals show the text as-is. IMDb link comes from
    the cached title on the movie dict if present, else a TMDB lookup is performed inline.
    """
    for m in movies:
        movie = Movie.from_api(m)
        title_text = f"{movie.display_title} ({movie.display_year}) — {movie.vote_average:.1f}/10"
        imdb_url = get_imdb_url("movie" if not movie.is_tv else "tv", movie.id, key) if key else None
        if imdb_url:
            title_text = f"\033]8;;{imdb_url}\033\\{title_text}\033]8;;\033\\"
        print(title_text)
        if movie.overview:
            print(f"  {movie.overview[:200]}")
        print()


def slug_for_query(query: str, max_len: int = 60) -> str:
    """Turn a query string into a filesystem-safe slug. Strips punctuation, lowercases, joins with -.

    Two safeguards: drop empty parts (lots of "the a of" gets dropped) and cap overall length so
    flag-heavy calls ("--plot-queries 'a' 'b' 'c'") don't generate 200-char filenames.
    """
    parts = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
    slug = "-".join(parts)
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]  # chunk at last word boundary
    return slug or "recommendations"


def get_providers(media_type: str, item_id: int, key: str, region: str = "US") -> Provider:
    """Watch providers for a title in a region; returns empty Provider on any failure."""
    # One movie without provider data must not sink the shortlist.
    try:
        data = get(f"/{media_type}/{item_id}/watch/providers", {"watch_region": region}, key=key)
        return Provider.from_api((data.get("results") or {}).get(region))
    except SystemExit:
        return Provider()


def get_reviews(media_type: str, item_id: int, key: str, n: int = 2) -> List[Review]:
    """Top-N reviews for a title, sorted by author rating (TMDB's only available proxy)."""
    try:
        results = get(f"/{media_type}/{item_id}/reviews", {}, key=key).get("results", [])
    except SystemExit:
        return []
    # TMDB /reviews exposes no like/helpful count; sort by the reviewer's own rating as the best proxy.
    sorted_results = sorted(
        results,
        key=lambda r: (r.get("author_details") or {}).get("rating") or 0,
        reverse=True,
    )[:n]
    return [Review.from_api(r) for r in sorted_results]


def get_keywords(media_type: str, item_id: int, key: str) -> List[str]:
    """Plot keywords (tags) for a title, e.g. ['spy', 'cia', 'cold war']."""
    try:
        data = get(f"/{media_type}/{item_id}/keywords", {}, key=key)
    except SystemExit:
        return []
    # /movie/keywords returns {"keywords": [...]}; /tv/keywords returns {"results": [...]} — handle both.
    items = data.get("keywords") or data.get("results") or []
    return [k["name"] for k in items if k.get("name")]


def get_config(key: str) -> dict:
    """TMDB image configuration (base URLs + available poster sizes)."""
    return get("/configuration", {}, key=key)


def get_imdb_id(media_type: str, item_id: int, key: str) -> Optional[str]:
    """Resolve a TMDB title to its bare IMDb ID (`tt1234567`), or None."""
    try:
        ids = get(f"/{media_type}/{item_id}/external_ids", {}, key=key)
    except SystemExit:
        return None
    return ids.get("imdb_id")


def get_imdb_url(media_type: str, item_id: int, key: str) -> Optional[str]:
    """Resolve a TMDB title to its IMDB URL, or None if the title has no imdb_id."""
    imdb_id = get_imdb_id(media_type, item_id, key)
    return IMDB_URL.format(imdb_id) if imdb_id else None


def omdb_fetch(imdb_id: str, omdb_key: str) -> Optional[dict]:
    """Fetch one OMDb record for an IMDb ID, or None on any failure / OMDb error.

    One call covers everything we want from OMDb: `plot=full` swaps only the Plot field,
    so the Ratings array (and the RT score inside it) still comes back on the same request.
    """
    qs = urllib.parse.urlencode({"i": imdb_id, "plot": "full", "apikey": omdb_key})
    try:
        with urllib.request.urlopen(f"https://www.omdbapi.com/?{qs}", timeout=10) as r:
            data = json.load(r)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    return None if data.get("Response") == "False" else data


def omdb_long_plot(imdb_id: str, omdb_key: str) -> Optional[str]:
    """OMDb's full plot paragraph for a movie by IMDb ID. None on failure or OMDb error."""
    return (omdb_fetch(imdb_id, omdb_key) or {}).get("Plot")


def omdb_rt_score(record: Optional[dict]) -> Optional[str]:
    """Rotten Tomatoes score (e.g. '85%') from an omdb_fetch record, or None if absent."""
    for rating in (record or {}).get("Ratings", []):
        if rating.get("Source") == "Rotten Tomatoes":
            return rating.get("Value")
    return None


try:
    import diskcache as _diskcache

    _CACHE = _diskcache.Cache(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".plot-cache"))
    # Wikipedia plots change on a slower cadence than we hit them — cache forever.
    _plot_cache = _CACHE.memoize(expire=None)
except ImportError:
    _CACHE = None

    def _plot_cache(func):
        return func


# TMDB's daily export is the only bulk keyword catalog; ~92k rows, ~1MB gzipped, cached forever.
_KEYWORD_EXPORT = "https://files.tmdb.org/p/exports/keyword_ids_{date}.json.gz"


def keyword_catalog(force: bool = False) -> dict:
    """Return {name: id} for all ~92k TMDB keywords. Cached forever; {} if unavailable.

    The export is published daily and the current day's file may not exist yet, so we walk back
    up to 4 days. Cached under a fixed key — the catalog is append-mostly, so a stale copy is fine.
    """
    if _CACHE is not None and not force:
        cached = _CACHE.get("keyword_catalog")
        if cached:
            return cached

    catalog: dict = {}
    today = datetime.date.today()
    for back in range(1, 5):  # Day-1 is the newest reliably-published export.
        stamp = (today - datetime.timedelta(days=back)).strftime("%m_%d_%Y")
        try:
            req = urllib.request.Request(
                _KEYWORD_EXPORT.format(date=stamp),
                headers={"User-Agent": "movie-recs/1.0 (personal project)"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                blob = r.read()
        except (urllib.error.URLError, urllib.error.HTTPError):
            continue
        try:
            text = gzip.GzipFile(fileobj=io.BytesIO(blob)).read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                catalog[row["name"].lower()] = row["id"]
            except (json.JSONDecodeError, KeyError):
                continue  # Skip a malformed row rather than sink the whole catalog.
        break

    if catalog and _CACHE is not None:
        _CACHE.set("keyword_catalog", catalog, expire=None)  # never expires
    return catalog


@_plot_cache
def wikipedia_plot(title: str) -> Optional[str]:
    """Fetch the full Plot section from Wikipedia for a movie title. None on miss."""
    headers = {"User-Agent": "movie-recs/1.0 (personal project)"}
    try:
        search_qs = urllib.parse.urlencode({"action": "query", "list": "search",
                                           "srsearch": title, "format": "json", "srlimit": 10})
        req = urllib.request.Request(f"https://en.wikipedia.org/w/api.php?{search_qs}", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            hits = json.load(r).get("query", {}).get("search", [])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    if not hits:
        return None
    # Prefer film articles — bare titles like "Super 8" or "It" hit non-film pages first.
    # Match "(film)", "(YYYY film)", or a trailing " film" (Wikipedia's three disambiguation styles).
    film_pattern = re.compile(r"\(\s*\d{0,4}\s*film\s*\)|\bfilm\b", re.IGNORECASE)
    film_hits = [h for h in hits if film_pattern.search(h["title"])]
    # Try up to 3 candidates (film hits first, then any hit). Disambiguation pages like
    # "Super 8 (film)" have no Plot section, so fall through to "Super 8 (2011 film)".
    candidates = (film_hits + [h for h in hits if h not in film_hits])[:3]
    plot_section = re.compile(r"==\s*Plot\s*==\s*\n", re.IGNORECASE)
    extract = re.compile(r"==\s*Plot\s*==\s*\n(.*?)(?=\n==[^=])", re.DOTALL | re.IGNORECASE)

    for hit in candidates:
        # Wikipedia's anonymous rate limit is ~50 req/min; we make 2 calls per candidate.
        time.sleep(0.3)
        try:
            parse_qs = urllib.parse.urlencode({"action": "parse", "page": hit["title"],
                                               "prop": "wikitext", "format": "json"})
            req = urllib.request.Request(f"https://en.wikipedia.org/w/api.php?{parse_qs}", headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                wikitext = json.load(r).get("parse", {}).get("wikitext", {}).get("*", "")
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError):
            continue
        # Strip HTML comments so `== Plot == <!-- WP:FILMPLOT note -->` matches — without this,
        # the Plot regex bails on any article following Wikipedia's plot-length guidance.
        wikitext = re.sub(r"<!--.*?-->", "", wikitext, flags=re.DOTALL)
        if not wikitext or not plot_section.search(wikitext):
            continue
        m = extract.search(wikitext)
        if not m:
            continue
        plot = m.group(1)
        plot = re.sub(r"\[\[([^|\]\n]+(?:\|[^\]\n]+)?)\]\]", lambda x: x.group(1).split("|")[-1], plot)
        plot = re.sub(r"'''(.+?)'''", r"\1", plot)
        plot = re.sub(r"''(.+?)''", r"\1", plot)
        plot = re.sub(r"\{\{[^{}]*\}\}", "", plot)  # drop templates like {{cn}}
        return plot.strip()
    return None


def _get_json_with_retry(url: str, headers: dict, max_retries: int = 1, deadline_s: float = 5.0) -> Optional[Any]:
    """GET a URL as JSON, retrying once on 429 with Retry-After backoff. Bails if total time exceeds deadline_s."""
    start = time.monotonic()
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                # Cap Retry-After at remaining budget so a single slow movie can't blow the whole deadline.
                remaining = deadline_s - (time.monotonic() - start)
                if remaining <= 0:
                    return None
                wait = min(int(e.headers.get("Retry-After", 2)), int(remaining))
                time.sleep(wait)
                continue
            return None
        except (urllib.error.URLError, json.JSONDecodeError):
            return None
    return None


@_plot_cache
def wikipedia_plot_from_imdb(imdb_id: str) -> Optional[str]:
    """Resolve IMDb ID → Wikidata QID → Wikipedia article title → wikitext → Plot section.

    Avoids title-search ambiguity (e.g. 'It', 'Up', 'Alien') by going directly from the canonical
    IMDb identifier. 3 API calls: Wikidata search by P345, Wikidata sitelinks, Wikipedia parse.
    Each call retries once on 429 with Retry-After backoff and bails at a 5s deadline; 0.1s politeness
    sleep between calls keeps us under Wikipedia/Wikidata's ~50 req/min anon limit.

    Section name covers Plot / Synopsis / Premise / Storyline / Plot summary — most films use one
    of these. Returns None on any failure (rate limit exhausted, missing Wikidata link, no plot section).
    """
    if not imdb_id:
        return None
    headers = {"User-Agent": "movie-recs/1.0 (personal project)"}

    # Step 1: Wikidata search by IMDb ID (P345 = film/TV IMDb ID property)
    qs1 = urllib.parse.urlencode({
        "action": "query", "list": "search",
        "srsearch": f"haswbstatement:P345={imdb_id}",
        "format": "json", "srlimit": 1,
    })
    data = _get_json_with_retry(f"https://www.wikidata.org/w/api.php?{qs1}", headers)
    if not data:
        return None
    hits = data.get("query", {}).get("search", [])
    if not hits:
        return None
    qid = hits[0]["title"]  # e.g. "Q25188"
    time.sleep(0.1)  # Stay under Wikipedia/Wikidata's ~50 req/min anon limit.

    # Step 2: Resolve Wikidata QID → English Wikipedia article title (reuses QID from step 1 — option 3)
    qs2 = urllib.parse.urlencode({
        "action": "wbgetentities", "ids": qid,
        "props": "sitelinks", "sitefilter": "enwiki", "format": "json",
    })
    data = _get_json_with_retry(f"https://www.wikidata.org/w/api.php?{qs2}", headers)
    if not data:
        return None
    wiki_title = (
        data.get("entities", {})
        .get(qid, {})
        .get("sitelinks", {})
        .get("enwiki", {})
        .get("title")
    )
    if not wiki_title:
        return None
    time.sleep(0.1)

    # Step 3: Fetch wikitext and extract the Plot-style section (reuses wiki_title from step 2 — option 3)
    qs3 = urllib.parse.urlencode({
        "action": "parse", "page": wiki_title,
        "prop": "wikitext", "format": "json",
    })
    data = _get_json_with_retry(f"https://en.wikipedia.org/w/api.php?{qs3}", headers)
    if not data:
        return None
    wikitext = data.get("parse", {}).get("wikitext", {}).get("*", "")
    # Strip HTML comments so `== Plot == <!-- WP:FILMPLOT note -->` matches.
    wikitext = re.sub(r"<!--.*?-->", "", wikitext, flags=re.DOTALL)
    if not wikitext:
        return None

    # Match Plot, Synopsis, Premise, Storyline, or "Plot summary" — section names vary by article.
    m = re.search(
        r"==\s*(?:Plot|Synopsis|Premise|Storyline|Plot summary)\s*==\s*\n(.*?)(?=\n==[^=])",
        wikitext, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None

    plot = m.group(1)
    plot = re.sub(r"\[\[([^|\]\n]+(?:\|[^\]\n]+)?)\]\]", lambda x: x.group(1).split("|")[-1], plot)
    plot = re.sub(r"'''(.+?)'''", r"\1", plot)
    plot = re.sub(r"''(.+?)''", r"\1", plot)
    plot = re.sub(r"\{\{[^{}]*\}\}", "", plot)
    return plot.strip()


def _llm_score_plot(plot: str, query: str) -> int:
    """Score plot-query relevance 0-10 via Claude. Returns -1 if API key/SDK missing or call fails.

    Lazy-imports anthropic so the script keeps stdlib-only when the package is absent.
    Falls back to _score_plot on failure; use this only for semantic matches token-overlap misses
    (synonyms, intent — e.g. "kid battles extraterrestrials" → E.T. plot).

    Token usage is accumulated in module-level _LLM_USAGE; read with get_llm_usage() and reset with reset_llm_usage().
    """
    global _LLM_USAGE
    try:
        import anthropic  # type: ignore  # noqa: F401
    except ImportError:
        return -1
    # Mirror the SDK's env resolution so we can short-circuit before instantiating the client.
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return -1
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-5",
            # Thinking is on by default on Opus 5, so the cap has to cover the reasoning too —
            # 500 truncated mid-thought and took the integer with it.
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": (
                    f"Score 0-10 how central the query is to the plot.\n"
                    f"Query: {query}\n\n"
                    f"Plot (first 1500 chars): {plot[:1500]}\n\n"
                    "0-2: query terms appear incidentally or thematically but the plot is about something else.\n"
                    "3-5: a side element of the plot, not the engine.\n"
                    "6-8: a central mechanism or major arc.\n"
                    "9-10: the entire film is built around the query.\n"
                    "Reply with ONLY a single integer 0-10, no explanation."
                ),
            }],
        )
        _LLM_USAGE["input"] += response.usage.input_tokens
        _LLM_USAGE["output"] += response.usage.output_tokens
        # Parse defensively — the model occasionally adds a word before or after the number.
        # Read the text block by type, not by index: with thinking on, content[0] is a
        # ThinkingBlock (which has .thinking, not .text) and indexing would AttributeError.
        # stop_reason is the other half: max_tokens means the budget ate the answer, refusal
        # means there is no score at all. Both fall back to token-overlap.
        if response.stop_reason != "end_turn":
            return -1
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        match = re.search(r"\d+", text)
        if not match:
            return -1
        return max(0, min(10, int(match.group())))
    except anthropic.RateLimitError as e:
        # Warn explicitly when rate-limited so a silent fallback doesn't degrade scores unnoticed.
        warnings.warn(f"LLM scoring rate-limited, falling back to token-overlap: {e}", stacklevel=2)
        return -1
    except Exception:
        return -1


# Module-level usage accumulator — reset between searches via reset_llm_usage().
_LLM_USAGE = {"input": 0, "output": 0}


def reset_llm_usage() -> None:
    """Zero the token counters before a search."""
    _LLM_USAGE["input"] = 0
    _LLM_USAGE["output"] = 0


def get_llm_usage() -> dict:
    """Return a copy of the current cumulative input/output token totals."""
    return {"input": _LLM_USAGE["input"], "output": _LLM_USAGE["output"]}


def plot_query_search(query: str, key: str, *, quick: bool = False) -> list:
    """Rank movies from a curated candidate pool by plot-query relevance.

    Strategy: pre-filter on TMDB overview (free), then re-rank with LLM-scored Wikipedia plot text.
    Falls back to token-overlap scoring when the Anthropic SDK or key is unavailable. With
    `quick=True`, skip Wikipedia fetches and LLM scoring; use TMDB overview (+ OMDb long plot on
    HTML path) for token-overlap scoring only.
    """
    return plot_queries_search([query], key, quick=quick)


def plot_queries_search(queries: List[str], key: str, *, quick: bool = False) -> list:
    """Multi-query plot search; implementation lives in movie_recs_plot."""
    return _plot_search(queries, key, quick=quick, backend=sys.modules[__name__])


def _full_text(m: dict, key: str) -> str:
    """Wikipedia plot (cached) when available, else TMDB overview. Used in full mode."""
    media_type = "movie"  # plot_query pool is always movies.
    imdb_id = get_imdb_id(media_type, m["id"], key)
    title = m.get("title") or ""
    plot = wikipedia_plot_from_imdb(imdb_id) if imdb_id else (wikipedia_plot(title) if title else None)
    return plot or m.get("overview", "") or ""


def _quick_text(m: dict, key: str) -> str:
    """OMDb long plot if available, else TMDB overview. Used in quick mode.

    OMDb's `plot=full` returns a longer paragraph than TMDB's 1-2 sentence overview — better signal
    for token-overlap without paying the Wikipedia-fetch latency. Falls back to overview if OMDb
    is unset or returns nothing.
    """
    media_type = "movie"
    imdb_id = get_imdb_id(media_type, m["id"], key)
    omdb_key = os.environ.get("OMDB_API_KEY")
    # Guard on the key, not just the id — an unset key still builds a request (apikey=None)
    # that can only come back empty, costing a round-trip per film.
    long_plot = omdb_long_plot(imdb_id, omdb_key) if (omdb_key and imdb_id) else None
    return long_plot or m.get("overview", "") or ""


# When the floor leaves fewer than THIN_RESCUE_MIN_RESULTS cards, expand the pool via CF
# recommendations of the top THIN_RESCUE_SEEDS, then re-score the new films against the
# original queries. Catches cold-keyword-query cases (e.g. narrow war-film phrasing).
THIN_RESCUE_MIN_RESULTS = 5
THIN_RESCUE_SEEDS = 3
# Full mode uses the LLM-tightened 4 (incidental-mention cutoff). Quick mode can't gate on
# token-overlap against long OMDb plots — overlap rarely exceeds 0 on vocabulary-mismatched
# queries — so QUICK_FLOOR=-1 keeps every prefilter survivor and trusts the prefilter.
STRICT_FLOOR = 4
QUICK_FLOOR = -1


def thin_rescue(scored: list, queries: List[str], key: str, *, quick: bool = False,
                seeds: Optional[list] = None) -> list:
    """If `scored` has fewer than THIN_RESCUE_MIN_RESULTS cards, pull recommendations for the top
    THIN_RESCUE_SEEDS, score the new films against the queries, and return a merged list.

    Re-scoring is intentional: a seed's CF neighbors aren't assumed to match the query. We just use
    CF as a hint to widen the pool; the same floor applies. The caller applies the blockbuster quota
    to whatever comes back, so this function only has to hand over a ranked list.

    `seeds` is the caller's broader prefilter, used only when `scored` is empty: with no survivors
    there is nothing to expand from, and the rescue would do nothing on exactly the queries that
    needed it most.
    """
    if len(scored) >= THIN_RESCUE_MIN_RESULTS:
        return scored
    # Seed from the survivors, falling back to the wider prefilter when the floor culled
    # everything, so rescue is reachable in the zero-survivor case.
    pool = scored[:THIN_RESCUE_SEEDS] or list(seeds or [])[:THIN_RESCUE_SEEDS]
    # Print a one-line marker so users can see in CLI runs whether rescue fired.
    # stderr, so this diagnostic never lands in piped stdout.
    print(f"[thin_rescue] entering with {len(scored)} cards, {len(pool)} seeds",
          file=sys.stderr, flush=True)
    seen_ids = {m["id"] for m in scored}
    new_candidates: List[dict] = []
    for seed in pool:
        for r in recommendations_for(seed.get("title", ""), key):
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            new_candidates.append(r)

    # Match the main multi-query path: a rescue candidate survives if it matches any phrasing.
    # Score all queries and keep the best, rather than silently dropping films that only match a
    # later query in the user's list.
    use_llm = (not quick) and bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    floor = QUICK_FLOOR if quick else STRICT_FLOOR
    rescued: List[dict] = []
    for m in new_candidates:
        text = _quick_text(m, key) if quick else _full_text(m, key)
        scores = [_llm_score_plot(text, q) for q in queries] if use_llm else []
        if use_llm:
            scores = [s if s >= 0 else _score_plot(text, q) for s, q in zip(scores, queries)]
        else:
            scores = [_score_plot(text, q) for q in queries]
        score = max(scores)
        if score > floor:
            rescued.append({**m, "_score": score})

    # Union + re-sort. Quota is applied by the caller so rescue can't flood the top with
    # blockbusters the way the original shortlist would have.
    return sorted(scored + rescued, key=lambda m: m["_score"], reverse=True)


def _build_plot_pool(queries: List[str], key: str) -> tuple:
    """Build the union candidate pool across all queries. Returns (pool_movies, keyword_catalog).

    The fixed sci-fi + top_rated + popular sources are added once. Per-query keyword sources are
    unioned (no duplicates by id) — separate calls for the same keyword would otherwise waste budget.
    """
    pool: List[dict] = []
    seen: set = set()
    sources: list = [
        ("/movie/top_rated", {"page": 1}),
        ("/movie/popular", {"page": 1}),
    ]
    for page in range(1, 6):  # 5 pages × 20 = 100 candidates; classics (e.g. E.T.) sit on later pages.
        sources.append((f"/discover/movie", {
            "with_genres": 878, "sort_by": "vote_count.desc",
            "vote_count.gte": 500, "page": page,
        }))

    # Widen with films tagged by keywords named in the queries themselves — the fixed sci-fi pool
    # can't surface Primer / Coherence / Predestination for "time travel" queries.
    # vote_average.desc (not vote_count.desc) so cult films compete on rating rather than audience size.
    catalog = keyword_catalog()
    seen_kw_ids: set = set()
    for q in queries:
        for kid in keywords_in_query(q, catalog):
            if kid in seen_kw_ids:
                continue
            seen_kw_ids.add(kid)
            # 320-vote floor: raise if obscure high-rated noise creeps in, lower to reach deeper cuts.
            for page in (1, 2):
                sources.append(("/discover/movie", {
                    "with_keywords": kid, "sort_by": "vote_average.desc",
                    "vote_count.gte": 320, "page": page,
                }))
    for path, params in sources:
        res = get(path, params, key=key).get("results", [])
        for m in res:
            if m["id"] not in seen:
                seen.add(m["id"])
                pool.append(m)
    return pool, catalog


# --google mode: one Gemini REST call returns a ranked list of titles, resolved via TMDB.
# Skips Wikipedia fetches and the per-film LLM scorer — Gemini's ranking is the final ranking.
# Cheaper than the full pipeline (no LLM call per film, no keyword catalog round-trip),
# at the cost of Gemini hallucinating ~5-10% of titles that TMDB can't resolve.
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_HOST = "https://generativelanguage.googleapis.com/v1beta"


def gemini_multi_recommendations(queries: List[str], key: str, per_query: int = 25) -> List[dict]:
    """Multi-query Gemini variant: one call per query, union results, dedupe by id.

    Use --plot-queries when one phrasing underspecifies — e.g. "political satire" + "idiots in power"
    want different films. Each call is independent; cost is len(queries) × one Gemini call. Final
    ordering preserves first-seen (so query order matters: list your broadest phrasing first).
    """
    seen_ids: set = set()
    union: List[dict] = []
    for q in queries:
        for m in gemini_recommendations(q, key, n=per_query):
            if m["id"] in seen_ids:
                continue
            seen_ids.add(m["id"])
            union.append(m)
    return union


def gemini_recommendations(query: str, key: str, n: int = 25) -> List[dict]:
    """Ask Gemini for `n` film titles matching the query. Returns a list of TMDB movie dicts.

    Two failure modes handled: (a) Gemini invents a title TMDB can't resolve — logged, dropped from the
    pool. (b) Gemini returns more or fewer than n titles — trust what's returned, cap is a target not
    a contract.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY not set. Get one at https://aistudio.google.com/apikey and export it.")

    prompt = (
        f"Recommend exactly {n} films matching this description.\n"
        f"Description: {query}\n\n"
        f"Reply with ONLY a numbered list, one title per line, in this exact format:\n"
        f"1. Title\n"
        f"2. Title (Year)\n"
        f"No commentary, no explanations, no categories. Just the list."
    )
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        # Temperature 0.2 — narrow band where similar prompts stay coherent without collapsing to the same list.
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.2},
    }).encode()

    url = f"{GEMINI_HOST}/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            response = json.load(r)
    except urllib.error.HTTPError as e:
        # Read the body for the API's own message — "model not available to new users"
        # looks the same as "bad key" without surfacing the actual reason.
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            detail = e.reason
        sys.exit(f"Gemini {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Gemini unreachable: {e.reason}")

    text = response["candidates"][0]["content"]["parts"][0]["text"].strip()

    # Parse "1. Title", "1) Title", or "- Title (Year)" lines. Strip numbering and parenthetical year.
    titles: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # drop "1. " or "1) " or "- " prefixes
        line = re.sub(r"^\s*[\-\d]+[\.\)]\s*", "", line)
        # drop "(YYYY)" suffix and any post-year trivia
        line = re.sub(r"\s*\(\d{4}\)\s*", " ", line)
        # drop any parenthetical after the title (e.g. "(a.k.a. Foo)")
        line = re.sub(r"\s*\(.*?\)\s*", " ", line)
        # remaining prose after the title — split on dash or colon and keep leftmost
        line = re.split(r"\s+[-–—:]\s+", line, maxsplit=1)[0]
        titles.append(line.strip())
    titles = [t for t in titles if t]

    # Resolve each title via TMDB /search/movie. Multimatch: take the most-voted result.
    # Hallucinated titles may 404 — drop and warn, don't sink the whole list.
    matched: List[dict] = []
    missing: List[str] = []
    seen_ids: set = set()
    for title in titles:
        res = get("/search/movie", {"query": title}, key=key).get("results", [])
        if not res:
            missing.append(title)
            continue
        # Prefer the most-voted result; ties broken by rating.
        pick = sorted(res, key=lambda m: (m.get("vote_count", 0), m.get("vote_average", 0)), reverse=True)[0]
        if pick["id"] in seen_ids:
            continue
        seen_ids.add(pick["id"])
        matched.append(pick)

    if missing:
        # stderr so this diagnostic doesn't leak via stdout on smoke-assert runs.
        print(f"[gemini] {len(missing)} of {len(titles)} titles not found on TMDB (dropped): {missing[:5]}{'...' if len(missing) > 5 else ''}", file=sys.stderr, flush=True)
    return matched


# A "blockbuster" is just a high vote_count — Marvel / BTTF territory. They're often legitimate
# matches, so we cap rather than exclude them; otherwise they fill the top of the shortlist and
# bury cult favorites that match the query better.
BLOCKBUSTER_VOTES = 8000
BLOCKBUSTER_QUOTA = 3


def apply_blockbuster_quota(ranked: list, votes: int = BLOCKBUSTER_VOTES,
                            quota: int = BLOCKBUSTER_QUOTA, n: int = 10) -> list:
    """Reorder a ranked list so at most `quota` of the top `n` are high-vote blockbusters.

    Stable: relative score order is preserved within both groups, and demoted blockbusters go to the
    tail rather than being dropped — a pool with nothing but blockbusters still returns them.
    """
    big = [m for m in ranked if (m.get("vote_count") or 0) >= votes]
    small = [m for m in ranked if (m.get("vote_count") or 0) < votes]
    # Take `quota` blockbusters + fill the rest of the top-n from small films. If small runs short,
    # leftover blockbusters backfill the head — a cap on competition for slots, not a hard reservation.
    head = big[:quota] + small[: max(0, n - quota)]
    if len(head) < n:
        # Membership is tested on object identity, not dict equality: two films with
        # identical fields compare equal, and the second would silently drop out.
        picked = {id(m) for m in head}
        head += [m for m in big[quota:] if id(m) not in picked][: n - len(head)]
    head.sort(key=lambda m: m.get("_score", 0), reverse=True)
    picked = {id(m) for m in head}
    return head + [m for m in ranked if id(m) not in picked]


def render_html(movies: list, key: str, out_path: str = "watch_recommendations.html", region: str = "US",
                reviews_n: int = 3, omdb_key: Optional[str] = None) -> str:
    """Build a self-contained HTML page of movie cards and write it to `out_path`.

    If `omdb_key` is set, fetches a longer plot paragraph per movie via OMDb.
    """
    config = get_config(key)
    base_url = config["images"]["secure_base_url"]
    rows: List[Row] = []
    for m in movies:
        movie = Movie.from_api(m)
        # `title` → movie, `name` → TV; routes the per-title calls to the right endpoint family.
        media_type = "tv" if movie.is_tv else "movie"
        imdb_id = get_imdb_id(media_type, movie.id, key)
        imdb_url = IMDB_URL.format(imdb_id) if imdb_id else None
        # One OMDb request per movie serves both the long plot and the RT score.
        omdb = omdb_fetch(imdb_id, omdb_key) if (omdb_key and imdb_id) else None
        rows.append(Row(
            movie=movie,
            media_type=media_type,
            poster_html=poster_img(movie, base_url),
            imdb_url=imdb_url,
            providers=get_providers(media_type, movie.id, key, region),
            reviews=get_reviews(media_type, movie.id, key, n=reviews_n),
            keywords=get_keywords(media_type, movie.id, key),
            long_plot=(omdb or {}).get("Plot"),
            rt_score=omdb_rt_score(omdb),
            # Empty string when no omdb_key — the template skips the RT link in that case.
            rt_url=(f"https://www.rottentomatoes.com/search?search={urllib.parse.quote(movie.display_title)}"
                    if omdb_key else ""),
            # Letterboxd and JustWatch expose no public slug lookup; fall back to search URLs.
            lb_url=f"https://letterboxd.com/search/{urllib.parse.quote(movie.display_title)}/",
            jw_url=f"https://www.justwatch.com/us/search?q={urllib.parse.quote(movie.display_title)}",
        ))
    body = "\n".join(_movie_card(r) for r in rows)
    sidebar = _sidebar_html()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_html_template(body, sidebar))
    return out_path


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface, separate from main() so it can be built without running a search."""
    parser = argparse.ArgumentParser(
        description="Recommend movies from TMDB + Wikipedia + OMDb + Claude.",
    )
    parser.add_argument("keyword", nargs="?", default="spy",
                        help="Keyword: " + "|".join(KEYWORDS) + " (default: spy)")
    parser.add_argument("--html", action="store_true", help="Write HTML output to watch_{slug}.html (override with --out)")
    parser.add_argument("--since", type=int, help="Filter keyword results by release year (>=)")
    parser.add_argument("--similar-to", metavar="TITLE", help="Find recommendations for a movie or TV title")
    parser.add_argument("--year", type=int, help="Release year for TV series lookup (used with --similar-to)")
    parser.add_argument("--no-animated", action="store_true", help="Exclude Animation genre (TV)")
    parser.add_argument("--no-omdb", action="store_true", help="Skip OMDb long-plot + RT score lookups")
    parser.add_argument("--out", metavar="PATH", help="HTML output path (default: watch_{slug}.html)")
    parser.add_argument("--reviews", type=int, default=3, help="Number of reviews per card (default: 3)")
    parser.add_argument("--actor", metavar="NAME", help="Actor name for filmography search")
    parser.add_argument("--plot-query", metavar="QUERY",
                        help="Free-text plot query (ranked by Wikipedia plot + LLM score)")
    # Pass each as a separate --plot-queries arg, e.g. --plot-queries "foo" "bar" — argparse's
    # nargs="+" appends in order; mutually exclusive with --plot-query (enforced in main()).
    parser.add_argument("--plot-queries", metavar="QUERY", nargs="+",
                        help="Multiple plot queries: union the pools, score each film against every one, keep the max")
    parser.add_argument("--quick", action="store_true",
                        help="Skip Wikipedia fetches and LLM scoring; use TMDB overview + OMDb long plot with token-overlap (fast, lower quality)")
    # --google is the DEFAULT for --plot-query when this flag is absent. Use --no-google to fall
    # back to the legacy TMDB-keyword-pool + LLM rerank pipeline (offline-capable, slower, LLM-judged).
    # --google explicitly is accepted for backwards-compatibility.
    parser.add_argument("--google", action="store_true", default=True,
                        help="(default) Use Gemini to recommend titles; with --plot-queries runs one call per phrasing")
    parser.add_argument("--no-google", dest="google", action="store_false",
                        help="Use the TMDB keyword pool + per-film LLM scoring instead of Gemini")
    # --nationality filters every result mode (keyword/similar/actor/plot) to films whose
    # origin_country list contains this ISO 3166-1 alpha-2 code. Trivial filter on TMDB's existing
    # origin_country field — no extra API calls. Pair with --plot-query for "best films from X".
    parser.add_argument("--nationality", metavar="CC",
                        help="ISO 3166-1 alpha-2 country code (e.g. HU, NO, JP, FR); keeps only films from that country")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Run one search and print it as text or HTML. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    key = os.environ.get("TMDB_API_KEY")
    if not key:
        sys.exit("TMDB_API_KEY not set. Get one at https://www.themoviedb.org/settings/api and export it.")
    omdb_key = None if args.no_omdb or args.google else os.environ.get("OMDB_API_KEY")

    if args.plot_query and args.plot_queries:
        parser.error("--plot-query and --plot-queries are mutually exclusive")
    if args.plot_queries:
        # --plot-queries works with both Gemini (--google default) and legacy (--no-google):
        # Gemini runs one call per query and unions, legacy uses max-of-queries per film.
        if args.google:
            raw = gemini_multi_recommendations(args.plot_queries, key)
        else:
            raw = plot_queries_search(args.plot_queries, key, quick=args.quick)
            if not args.quick:
                usage = get_llm_usage()
                if usage["input"] or usage["output"]:
                    total = usage["input"] + usage["output"]
                    print(f"Tokens: {usage['input']} in / {usage['output']} out / {total} total")
        if not raw:
            sys.exit(f"no movies matched plot queries {args.plot_queries!r}")
    elif args.plot_query:
        # --google is the default: one Gemini call, then TMDB lookup per title, then straight to
        # render. Skip the keyword pool and LLM scoring; --quick semantics are implied.
        # --no-google restores the legacy TMDB-keyword-pool + LLM-rerank pipeline for users who
        # need it offline or want LLM-judged results over Gemini's text-only output.
        if args.google:
            raw = gemini_recommendations(args.plot_query, key)
            if not raw:
                sys.exit(f"gemini returned no resolvable titles for {args.plot_query!r}")
        else:
            raw = plot_query_search(args.plot_query, key, quick=args.quick)
            if not args.quick:
                usage = get_llm_usage()
                if usage["input"] or usage["output"]:
                    total = usage["input"] + usage["output"]
                    print(f"Tokens: {usage['input']} in / {usage['output']} out / {total} total")
        if not raw:
            sys.exit(f"no movies matched plot query {args.plot_query!r}")
    elif args.actor:
        raw = movies_by_actor(args.actor, key)
        if not raw:
            sys.exit(f"no movies found for actor {args.actor!r}")
    elif args.similar_to and args.year:
        raw = recommendations_for_tv(args.similar_to, args.year, key, exclude_animated=args.no_animated)
        if not raw:
            sys.exit(f"no TV recommendations for {args.similar_to!r} ({args.year})")
    elif args.similar_to:
        raw = recommendations_for(args.similar_to, key)
        if not raw:
            sys.exit(f"no recommendations for {args.similar_to!r}")
    else:
        if args.keyword not in KEYWORDS:
            sys.exit(f"unknown keyword: {args.keyword!r}. Known: {list(KEYWORDS)}")
        raw = discover(args.keyword, key, since=args.since)

    # plot_query_search / plot_queries_search already rank by LLM relevance and apply the
    # blockbuster quota — re-running shortlist() would re-sort by vote_average and discard both.
    # Other modes are unranked.
    if args.nationality:
        cc = args.nationality.upper()
        lang = cc.lower()
        # TMDB /search/movie (used by Gemini resolver) doesn't carry origin_country — only
        # original_language. Check both: a film made in Hungary is also released in Hungarian.
        # For English-language films the user can drop --nationality; it's a coarse signal either way.
        def _native(m: dict) -> bool:
            if cc in (m.get("origin_country") or []): return True
            return m.get("original_language", "").lower() == lang
        before = len(raw)
        raw = [m for m in raw if _native(m)]
        print(f"[nationality] {cc}: {before} → {len(raw)} films")
        if not raw:
            sys.exit(f"no films matched --nationality {cc!r}")
    ranked_modes = args.plot_query or args.plot_queries
    movies = raw[:10] if ranked_modes else shortlist(raw)

    if args.html:
        # HTML filename keys off the actual query so re-runs with different prompts don't
        # silently overwrite each other. --out overrides.
        if args.out:
            out_path = args.out
        else:
            slug_src = args.plot_query or (" ".join(args.plot_queries) if args.plot_queries else args.keyword or "recommendations")
            out_path = f"watch_{slug_for_query(slug_src)}.html"
        path = render_html(movies, key, out_path=out_path, reviews_n=args.reviews, omdb_key=omdb_key)
        # Print the file path as a clickable file:// link via ANSI escape so iTerm2/Terminal.app
        # users can ctrl-click to open. The escape is a no-op on older terminals.
        clickable = f"\033]8;;file://{os.path.abspath(path)}\033\\{path}\033]8;;\033\\"
        print(f"wrote {clickable}")
    else:
        print_movies(movies, key=key if key else None)

    return 0


if __name__ == "__main__":
    sys.exit(main())
