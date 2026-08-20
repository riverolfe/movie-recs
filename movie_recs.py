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
import html as html_lib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, List, Optional

API = "https://api.themoviedb.org/3"

# ponytail: keyword IDs hardcoded — verify at https://www.themoviedb.org/keyword/<id>
KEYWORDS = {"spy": 470, "espionage": 158431, "assassin": 1701, "time-travel": 4379}

# ponytail: top US streaming providers for the filter sidebar; matched case-insensitively
# as a substring against each movie's flatrate provider names
TOP_PROVIDERS = [
    "Netflix", "Amazon Prime Video", "Disney Plus", "HBO Max", "Apple TV Plus",
    "Hulu", "Paramount Plus", "Peacock", "Starz", "Showtime",
]


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
        # ponytail: bare HTTPError reads as "HTTP Error 401" — translate to a hint about the API key
        sys.exit(f"TMDB {e.code}: {e.reason}. Check TMDB_API_KEY.")
    except urllib.error.URLError as e:
        sys.exit(f"TMDB unreachable: {e.reason}")


def shortlist(movies: list, n: int = 10) -> list:
    """Top-N movies by vote_average. Rating-only sort; popularity-weight when recall > precision."""
    return sorted(movies, key=lambda m: m.get("vote_average", 0), reverse=True)[:n]


def discover(keyword: str, key: str, min_votes: int = 200, since: Optional[int] = None) -> list:
    """Find films tagged with a keyword (spy/espionage/assassin), filtered by minimum votes and optional release date."""
    # ponytail: dropped genre filter — /search/keyword surfaced non-action spy films (Casablanca, North by Northwest)
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
    # ponytail: /recommendations (CF-style) over /similar (keyword neighbors) — better recall
    res = get("/search/movie", {"query": title}, key=key).get("results", [])
    if not res:
        return []
    movie_id = res[0]["id"]
    return get(f"/movie/{movie_id}/recommendations", {}, key=key).get("results", [])


def recommendations_for_tv(title: str, year: int, key: str, exclude_animated: bool = False) -> list:
    """Return CF recommendations for a TV series, optionally dropping Animation (genre 16)."""
    # ponytail: TV search takes first_air_date_year — pass-through to /search/tv; falls back to /search/movie if no TV match
    res = get("/search/tv", {"query": title, "first_air_date_year": year}, key=key).get("results", [])
    if not res:
        return []
    tv_id = res[0]["id"]
    recs = get(f"/tv/{tv_id}/recommendations", {}, key=key).get("results", [])
    if exclude_animated:
        # ponytail: TMDB genre id 16 = Animation — filter client-side; /recommendations has no genre filter
        recs = [r for r in recs if 16 not in (r.get("genre_ids") or [])]
    return recs


def movies_by_actor(name: str, key: str, min_votes: int = 200) -> list:
    """Return an actor's filmography (cast), filtered to films with enough votes to rate reliably."""
    # ponytail: /person/{id}/movie_credits returns cast + crew in one call; filter min_votes to suppress 1-vote obscurities
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
    # ponytail: route through Movie so TV results (name/first_air_date) render like the HTML path
    for m in movies:
        movie = Movie.from_api(m)
        title_text = f"{movie.display_title} ({movie.display_year}) — {movie.vote_average:.1f}/10"
        # ponytail: resolve IMDb URL lazily — most dicts already have it from the upstream
        # recommendations/recommendations_for path; otherwise one tiny TMDB lookup per row
        imdb_url = m.get("_imdb_url") or (get_imdb_url("movie" if not movie.is_tv else "tv", movie.id, key) if key else None)
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
    import re as _re
    parts = [w for w in _re.split(r"[^a-z0-9]+", query.lower()) if w]
    slug = "-".join(parts)
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]  # chunk at last word boundary
    return slug or "recommendations"


def get_providers(media_type: str, item_id: int, key: str, region: str = "US") -> Provider:
    """Watch providers for a title in a region; returns empty Provider on any failure."""
    # ponytail: skip-and-continue — one movie without provider data shouldn't sink the shortlist
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
    # ponytail: TMDB /reviews exposes no like/helpful count — sort by the reviewer's own rating (1-10) as the best proxy
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
    # ponytail: /movie/keywords returns {"keywords": [...]}; /tv/keywords returns {"results": [...]} — handle both
    items = data.get("keywords") or data.get("results") or []
    return [k["name"] for k in items if k.get("name")]


def get_config(key: str) -> dict:
    """TMDB image configuration (base URLs + available poster sizes)."""
    return get("/configuration", {}, key=key)


def get_imdb_url(media_type: str, item_id: int, key: str) -> Optional[str]:
    """Resolve a TMDB title to its IMDB URL, or None if the title has no imdb_id."""
    try:
        ids = get(f"/{media_type}/{item_id}/external_ids", {}, key=key)
    except SystemExit:
        return None
    imdb_id = ids.get("imdb_id")
    return f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None


def get_imdb_id(media_type: str, item_id: int, key: str) -> Optional[str]:
    """Resolve a TMDB title to its bare IMDb ID (`tt1234567`), or None."""
    try:
        ids = get(f"/{media_type}/{item_id}/external_ids", {}, key=key)
    except SystemExit:
        return None
    return ids.get("imdb_id")


def _imdb_id_from_url(url: Optional[str]) -> Optional[str]:
    """Extract `tt1234567` from an IMDB title URL, or None."""
    if not url:
        return None
    parts = url.rstrip("/").split("/")
    return parts[-1] if parts[-1].startswith("tt") else None


def omdb_long_plot(imdb_id: str, omdb_key: str) -> Optional[str]:
    """Fetch OMDb's full plot for a movie by IMDb ID. None on failure or OMDb error."""
    qs = urllib.parse.urlencode({"i": imdb_id, "plot": "full", "apikey": omdb_key})
    try:
        with urllib.request.urlopen(f"http://www.omdbapi.com/?{qs}", timeout=10) as r:
            data = json.load(r)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    if data.get("Response") == "False":
        return None
    return data.get("Plot")


def omdb_rt_score(imdb_id: str, omdb_key: str) -> Optional[str]:
    """Fetch the Rotten Tomatoes score from OMDb's Ratings array (e.g. '85%'). None on miss."""
    qs = urllib.parse.urlencode({"i": imdb_id, "apikey": omdb_key})
    try:
        with urllib.request.urlopen(f"http://www.omdbapi.com/?{qs}", timeout=10) as r:
            data = json.load(r)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    if data.get("Response") == "False":
        return None
    for rating in data.get("Ratings", []):
        if rating.get("Source") == "Rotten Tomatoes":
            return rating.get("Value")
    return None


# ponytail: Wikipedia plots are immutable enough to cache forever and cost 2-3 API calls + 0.3s sleep each.
# diskcache.memoize handles the key/serialize/expire triad; degrades to a no-op decorator when the
# package is absent so the script stays stdlib-only-runnable. Cache lives beside the script; delete to bust.
try:
    import diskcache as _diskcache

    _CACHE = _diskcache.Cache(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".plot-cache"))
    # ponytail: plots are immutable enough to cache forever (Wikipedia revisions are slower than our hit rate)
    _plot_cache = _CACHE.memoize(expire=None)
except ImportError:
    _CACHE = None

    def _plot_cache(func):
        return func


# ponytail: TMDB's daily export is the only bulk keyword catalog — the bundled OpenAPI spec has none
# (3 concrete id/name pairs, all genres). ~92k keywords, ~1MB gzipped, cached with expire=None.
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

    import datetime, gzip, io
    catalog: dict = {}
    today = datetime.date.today()
    for back in range(1, 5):  # ponytail: day-1 is the newest reliably-published export
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
                continue  # ponytail: skip malformed rows, don't sink the whole catalog
        break

    if catalog and _CACHE is not None:
        _CACHE.set("keyword_catalog", catalog, expire=None)  # never expires
    return catalog


def keywords_in_query(query: str, catalog: dict, limit: int = 4) -> List[int]:
    """Match a free-text query against the keyword catalog, longest phrase first.

    Scans contiguous word n-grams (longest first) so "time travel" beats "time", and drops
    n-grams that are entirely stopwords. Returns up to `limit` keyword ids, best match first.

    Each word is tried both verbatim and de-inflected ("traveling" → "travel", "dimensions" →
    "dimension") because catalog entries are singular base forms while queries are prose.
    """
    if not catalog:
        return []
    import itertools
    import re

    def variants(word: str) -> List[str]:
        """Word plus plausible base forms, longest-shot last. Order matters — verbatim wins."""
        out = [word]
        # ponytail: crude de-inflection covering the endings that actually break lookups —
        # traveling→travel, travelling→travel, dimensions→dimension, twists→twist, stories→story.
        # Not a stemmer: we generate candidates and let the catalog decide which is real.
        for suffix, repl in (("lling", "l"), ("ing", ""), ("ies", "y"), ("es", ""), ("s", "")):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                out.append(word[: len(word) - len(suffix)] + repl)
        return out

    words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
    found: List[int] = []
    seen: set = set()
    used: set = set()  # ponytail: word positions already claimed by a longer match
    # ponytail: 3-grams down to 1-grams — longest match wins ("multiple dimensions" over "dimensions")
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            span = range(i, i + size)
            # ponytail: skip spans overlapping an earlier (longer) hit — otherwise "time travel"
            # also yields bare "time", diluting the pool with a near-useless keyword
            if any(p in used for p in span):
                continue
            gram_words = words[i:i + size]
            if all(w in _STOPWORDS for w in gram_words):
                continue
            # ponytail: cartesian product over per-word variants — 3 words x ~2 variants = 8 lookups,
            # all dict hits, so cost is nil next to one API call
            for combo in itertools.product(*(variants(w) for w in gram_words)):
                kid = catalog.get(" ".join(combo))
                if kid and kid not in seen:
                    seen.add(kid)
                    found.append(kid)
                    used.update(span)
                    if len(found) >= limit:
                        return found
                    break  # ponytail: one id per n-gram position — don't also match its variants
    return found


@_plot_cache
def wikipedia_plot(title: str) -> Optional[str]:
    """Fetch the full Plot section from Wikipedia for a movie title. None on miss."""
    # ponytail: 2-call flow — search resolves canonical title (e.g. "Slow Horses" → "Slow Horses (TV series)"),
    # then parse pulls wikitext and we extract the "==Plot==" section
    import re
    import time
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
    # ponytail: prefer film articles — bare titles like "Super 8" or "It" hit non-film pages first;
    # match "(film)", "(YYYY film)", or " film" suffix (Wikipedia's three disambiguation styles)
    film_pattern = re.compile(r"\(\s*\d{0,4}\s*film\s*\)|\bfilm\b", re.IGNORECASE)
    film_hits = [h for h in hits if film_pattern.search(h["title"])]
    # ponytail: try up to 3 candidates (film hits first, then any hit); disambiguation pages like
    # "Super 8 film" have no Plot section so we fall through to "Super 8 (2011 film)"
    candidates = (film_hits + [h for h in hits if h not in film_hits])[:3]
    plot_section = re.compile(r"==\s*Plot\s*==\s*\n", re.IGNORECASE)
    extract = re.compile(r"==\s*Plot\s*==\s*\n(.*?)(?=\n==[^=])", re.DOTALL | re.IGNORECASE)

    for hit in candidates:
        # ponytail: 0.3s sleep — Wikipedia's anon rate limit is ~50 req/min; we make 2 calls per candidate
        time.sleep(0.3)
        try:
            parse_qs = urllib.parse.urlencode({"action": "parse", "page": hit["title"],
                                               "prop": "wikitext", "format": "json"})
            req = urllib.request.Request(f"https://en.wikipedia.org/w/api.php?{parse_qs}", headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                wikitext = json.load(r).get("parse", {}).get("wikitext", {}).get("*", "")
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError):
            continue
        # ponytail: strip HTML comments so `== Plot == <!-- WP:FILMPLOT note -->` matches — without this,
        # the Plot regex bails on any article that follows Wikipedia's plot-length guidance
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


_STOPWORDS = {"a", "an", "the", "to", "has", "have", "had", "is", "are", "was", "were",
                "be", "been", "being", "in", "on", "at", "by", "for", "of", "with",
                "and", "or", "but", "if", "then", "so", "as", "it", "its", "this",
                "that", "from", "his", "her", "their", "they", "he", "she", "we", "you"}


def _get_json_with_retry(url: str, headers: dict, max_retries: int = 1, deadline_s: float = 5.0) -> Optional[Any]:
    """GET a URL as JSON, retrying once on 429 with Retry-After backoff. Bails if total time exceeds deadline_s."""
    import time
    start = time.monotonic()
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                # ponytail: cap Retry-After at remaining budget so a single slow movie can't blow the whole deadline
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
    import re, time
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
    time.sleep(0.1)  # ponytail: be polite — anon rate limit is ~50 req/min

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
    # ponytail: strip HTML comments so `== Plot == <!-- WP:FILMPLOT note -->` matches — without this,
    # the Plot regex bails on any article that follows Wikipedia's plot-length guidance
    wikitext = re.sub(r"<!--.*?-->", "", wikitext, flags=re.DOTALL)
    if not wikitext:
        return None

    # ponytail: try Plot, Synopsis, Premise, Storyline, or "Plot summary" — they vary by article
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


def _score_plot(plot: str, query: str) -> int:
    """Score a plot text against a query: phrase substring bonus + content-token overlap (stemmed)."""
    plot_lower = plot.lower()
    query_lower = query.lower()
    # ponytail: phrase substring match is the strongest signal; falls back to stemmed content-token overlap
    # ponytail: stemmer is "drop trailing 's'" — handles alien/aliens, boy/boys; full Porter is overkill here
    stem = lambda w: w[:-1] if w.endswith("s") and len(w) > 3 else w
    score = 10 if query_lower in plot_lower else 0
    # ponytail: drop stopwords — "a", "to", "the" would otherwise match every plot
    query_content = {stem(w) for w in query_lower.split() if w not in _STOPWORDS}
    plot_stems = {stem(w) for w in plot_lower.split()}
    score += len(query_content & plot_stems) * 2
    return score


def _llm_score_plot(plot: str, query: str) -> int:
    """Score plot-query relevance 0-10 via Claude. Returns -1 if API key/SDK missing or call fails.

    Lazy-imports anthropic so the script keeps stdlib-only when the package is absent.
    Ponytail: scoring is the bottleneck — token-overlap matches literal strings, this catches synonyms
    and intent (e.g. "kid battles extraterrestrials" → E.T. plot). Falls back to _score_plot on failure.

    Token usage is accumulated in module-level _LLM_USAGE; read with get_llm_usage() and reset with reset_llm_usage().
    """
    global _LLM_USAGE
    try:
        import anthropic  # type: ignore  # noqa: F401
    except ImportError:
        return -1
    # ponytail: SDK resolves ANTHROPIC_API_KEY → ANTHROPIC_AUTH_TOKEN → profile in that order;
    # we mirror the first two so we can short-circuit before instantiating the client
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return -1
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=500,  # ponytail: generous cap so thinking + integer reply never truncates mid-thought
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
        # ponytail: parse defensively — even Opus 5 occasionally adds a word before/after the number
        import re
        text = response.content[0].text.strip()
        match = re.search(r"\d+", text)
        if not match:
            return -1
        return max(0, min(10, int(match.group())))
    except anthropic.RateLimitError as e:
        # ponytail: explicit warning when rate-limited — silent fallback hides a real problem
        # (the user's score will degrade to token-overlap without explanation)
        import warnings
        warnings.warn(f"LLM scoring rate-limited, falling back to token-overlap: {e}", stacklevel=2)
        return -1
    except Exception:
        return -1


# ponytail: module-level usage accumulator — reset between searches via reset_llm_usage()
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
    """Multi-query variant: union the keyword-sourced pools, score each film against every query,
    keep the max. One Wikipedia fetch per film (diskcache handles repeats); one LLM call per (film,
    query) pair.

    Use --plot-queries when one phrasing underspecifies the target — e.g. "lawyer beats the DA"
    and "innocent man proves himself" want different films, and a single phrase picks one side.

    With `quick=True`, skip Wikipedia + LLM entirely — score against TMDB overview + OMDb long plot.
    Floor relaxes from >4 to >1 since token-overlap can't hit 5 on a 1-sentence overview.
    """
    reset_llm_usage()  # ponytail: zero counters so get_llm_usage() reports just this search's spend
    pool, catalog = _build_plot_pool(queries, key)
    if not pool:
        return []

    # ponytail: union overview pre-filter — a film passes if it overlaps *any* query (free).
    # Using a single query's overlap would miss films the others pull in via keyword sources.
    prefilter = [
        m for m in pool
        if any(_score_plot(m.get("overview", ""), q) > 0 for q in queries)
    ]

    # ponytail: LLM-as-judge per (film, query) — keep max. Wikipedia fetched once per film, not per query.
    use_llm = (not quick) and bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    # ponytail: in quick mode token-overlap on TMDB+OMDb text rarely scores >0 — floor=0 keeps every
    # prefilter survivor. The pool prefilter is the only semantic gate; quiet pre-rank ordering shows
    # by overview match strength.
    floor = QUICK_FLOOR if quick else STRICT_FLOOR  # ponytail: -1 keeps every prefilter survivor in quick mode; strict overlap yields 0 too often on OMDb plots to gate on
    scored = []
    for m in prefilter:
        text = _quick_text(m, key) if quick else _full_text(m, key)
        if use_llm:
            scores = [_llm_score_plot(text, q) for q in queries]
            scores = [s if s >= 0 else _score_plot(text, q) for s, q in zip(scores, queries)]
        else:
            scores = [_score_plot(text, q) for q in queries]
        # ponytail: max-of-queries lets the loose query rescue a film the strict one would have scored low.
        # Floor of 4 per the tightened prompt's "1-3 = incidental/side-element" cutoff; multi-query
        # doesn't loosen this — the cheap query half of the pair would otherwise rescue single-courtroom
        # films like The Martian or In Time that scored 3 against "courtroom thriller" alone.
        best = max(scores)
        if best > floor:
            scored.append({**m, "_score": best})
    # ponytail: thin-list rescue — if the floor left a near-empty shortlist, expand via CF neighbors
    # of the top seeds and re-score against the original queries. Pass quick through so rescue uses
    # the same text path.
    return thin_rescue(scored, queries, key, quick=quick)


def _full_text(m: dict, key: str) -> str:
    """Wikipedia plot (cached) when available, else TMDB overview. Used in full mode."""
    media_type = "movie"  # ponytail: plot_query pool is always movies
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
    long_plot = omdb_long_plot(imdb_id, os.environ.get("OMDB_API_KEY")) if imdb_id else None
    return long_plot or m.get("overview", "") or ""


# ponytail: when the floor leaves fewer than this many cards, expand the pool via the highest-scored
# seeds — TMDB's CF recommendations (already implemented as --similar-to) — and re-score the new
# films against the ORIGINAL query. The cold-keyword-query case earlier (e.g. war-film narrow phrasing)
# is exactly what this rescues.
THIN_RESCUE_MIN_RESULTS = 5
THIN_RESCUE_SEEDS = 3
# ponytail: full mode uses the LLM-tightened 4 (incidental-mention cutoff). Quick mode can't gate on
# token-overlap against long OMDb plots (overlap rarely exceeds 0 on vocabulary-mismatched queries),
# so -1 keeps every prefilter survivor and trusts the prefilter as the only semantic filter.
STRICT_FLOOR = 4
QUICK_FLOOR = -1


def thin_rescue(scored: list, queries: List[str], key: str, *, quick: bool = False) -> list:
    """If `scored` has fewer than THIN_RESCUE_MIN_RESULTS cards, pull recommendations for the top
    THIN_RESCUE_SEEDS, score the new films against the queries, and return a merged list.

    Re-scoring is intentional: a seed's CF neighbors aren't assumed to match the query. We just use
    CF as a hint to widen the pool; the same floor applies. Re-running the quota on the final list
    keeps the blockbuster cap in play even after rescue.
    """
    if len(scored) >= THIN_RESCUE_MIN_RESULTS:
        return scored
    # ponytail: print a one-line marker so users can see in CLI runs whether rescue fired.
    print(f"[thin_rescue] entering with {len(scored)} cards, {len(scored[:THIN_RESCUE_SEEDS])} seeds", flush=True)
    seen_ids = {m["id"] for m in scored}
    new_candidates: List[dict] = []
    for seed in scored[:THIN_RESCUE_SEEDS]:
        for r in recommendations_for(seed.get("title", ""), key):
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            new_candidates.append(r)

    # ponytail: rank the rescue candidates against a single representative query — the first one.
    # The main loop's max-of-queries matters when phrasings genuinely diverge ("lawyer beats DA" +
    # "innocent man proves himself" want different films). In rescue the candidates come from CF
    # neighbors of one of our seeds, so they're already similar to the seeds — a single-query score
    # is enough to filter for "matches the user's intent" and saves len(queries) LLM calls per film.
    use_llm = (not quick) and bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    floor = QUICK_FLOOR if quick else STRICT_FLOOR  # ponytail: -1 keeps every prefilter survivor in quick mode; strict overlap yields 0 too often on OMDb plots to gate on
    rescue_query = queries[0]
    rescued: List[dict] = []
    for m in new_candidates:
        text = _quick_text(m, key) if quick else _full_text(m, key)
        if use_llm:
            score = _llm_score_plot(text, rescue_query)
            if score < 0:
                score = _score_plot(text, rescue_query)
        else:
            score = _score_plot(text, rescue_query)
        if score > floor:
            rescued.append({**m, "_score": score})

    # ponytail: union + re-sort. Quota is reapplied at the end so rescue can't flood the top with
    # blockbusters the way the original shortlist would have.
    merged = scored + rescued
    return apply_blockbuster_quota(sorted(merged, key=lambda m: m["_score"], reverse=True))


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
    for page in range(1, 6):  # 5 pages of sci-fi = 100 candidates including classics (E.T. is on page 5)
        sources.append((f"/discover/movie", {
            "with_genres": 878, "sort_by": "vote_count.desc",
            "vote_count.gte": 500, "page": page,
        }))

    # ponytail: widen the pool with films tagged by keywords named in the queries themselves — the
    # fixed sci-fi pool can't surface Primer/Coherence/Predestination for "time travel" queries.
    # ponytail: vote_average.desc (not vote_count.desc) so cult films compete on rating rather than
    # audience size. The 320-vote floor: raise if obscure high-rated noise creeps in, lower to reach deeper cuts.
    catalog = keyword_catalog()
    seen_kw_ids: set = set()
    for q in queries:
        for kid in keywords_in_query(q, catalog):
            if kid in seen_kw_ids:
                continue
            seen_kw_ids.add(kid)
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


# ponytail: --google mode asks Gemini for a list of titles (one REST call) and resolves them via TMDB.
# Skips Wikipedia fetches and the per-film LLM scorer — Gemini's ranking is the final ranking. Cheaper
# than the full pipeline (no LLM call per film, no keyword catalog round-trip) at the cost of Gemini
# hallucinating ~5-10% of titles and TMDB rejecting them. Track the count so we can surface it.
# ponytail: --google mode asks Gemini for a list of titles (one REST call) and resolves them via TMDB.
# Skips Wikipedia fetches and the per-film LLM scorer — Gemini's ranking is the final ranking. Cheaper
# than the full pipeline (no LLM call per film, no keyword catalog round-trip) at the cost of Gemini
# hallucinating ~5-10% of titles and TMDB rejecting them. Track the count so we can surface it.
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
        # temperature 0.2 — narrow band where similar prompts stay coherent without collapsing to the same list
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.2},
    }).encode()

    url = f"{GEMINI_HOST}/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            response = json.load(r)
    except urllib.error.HTTPError as e:
        # ponytail: read the body for the API's own message — "model not available to new users"
        # looks the same as "bad key" unless we surface the actual reason
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            detail = e.reason
        sys.exit(f"Gemini {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Gemini unreachable: {e.reason}")

    text = response["candidates"][0]["content"]["parts"][0]["text"].strip()

    # ponytail: parse "1. Title", "1) Title", or " - Title (Year)" lines. Strip numbering/parenthetical year.
    import re
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
    titles = [t for t in titles if t]  # dedup empties

    # ponytail: TMDB /search/movie for each title. Multimatch: take the most-voted result. May 404 for
    # hallucinated titles — drop and warn, don't sink the whole list.
    matched: List[dict] = []
    missing: List[str] = []
    seen_ids: set = set()
    for title in titles:
        res = get("/search/movie", {"query": title}, key=key).get("results", [])
        if not res:
            missing.append(title)
            continue
        # ponytail: prefer the most-voted result; ties broken by rating
        pick = sorted(res, key=lambda m: (m.get("vote_count", 0), m.get("vote_average", 0)), reverse=True)[0]
        if pick["id"] in seen_ids:
            continue
        seen_ids.add(pick["id"])
        matched.append(pick)

    if missing:
        # ponytail: stderr so this diagnostic doesn't leak via stdout on smoke-assert runs
        print(f"[gemini] {len(missing)} of {len(titles)} titles not found on TMDB (dropped): {missing[:5]}{'...' if len(missing) > 5 else ''}", file=sys.stderr, flush=True)
    return matched


# ponytail: a "blockbuster" here is just a high vote_count — Marvel/BTTF territory. They're often
# legitimate matches, so we cap them rather than exclude them; without the cap they took 5 of 10 slots
# and buried Predestination/Donnie Darko, which were in the pool but outranked.
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
    # ponytail: take `quota` blockbusters + fill the rest of the top-n from small films. If small runs
    # short the head is under-full, and leftover blockbusters backfill it — a cap on competition for
    # slots, not a hard reservation. Without this backfill the top 10 silently over-fills with big.
    head = big[:quota] + small[: max(0, n - quota)]
    if len(head) < n:
        backfill = [m for m in big[quota:] if m not in head]
        head += backfill[: n - len(head)]
    head.sort(key=lambda m: m.get("_score", 0), reverse=True)
    tail = [m for m in ranked if m not in head]
    return head + tail


def poster_img(movie: Movie, base_url: str) -> str:
    """Render a 300px-wide <img> for a movie's poster, or empty string if no poster."""
    path = movie.poster_path
    if not path:
        return ""
    # ponytail: w300 + no CSS width — image displays at its natural 300px (TMDB medium size)
    return f'<img class="poster" src="{html_lib.escape(base_url)}w300{html_lib.escape(path)}" alt="">'


def _provider_badges(prov: Provider) -> str:
    # ponytail: green flatrate badges only — drop rent/buy; user wanted streaming-only
    if not prov.has_flatrate():
        return '<span class="muted">no streaming in region</span>'
    return " ".join(
        f'<span class="badge">{html_lib.escape(p.provider_name)}</span>'
        for p in prov.flatrate
    )


def _keywords_block(keywords: List[str]) -> str:
    """Render TMDB plot keywords as small dim chips under the overview."""
    if not keywords:
        return ""
    return (
        '<div class="keywords">'
        + "".join(f'<span class="keyword">{html_lib.escape(k)}</span>' for k in keywords)
        + "</div>"
    )


def _long_plot_block(long_plot: Optional[str]) -> str:
    """Render the OMDb full-plot paragraph under the overview, if present."""
    if not long_plot or long_plot == "N/A":
        return ""
    return f'<p class="long-plot">{html_lib.escape(long_plot)}</p>'


def _review_block(reviews: List[Review]) -> str:
    if not reviews:
        return '<p class="muted">no reviews yet</p>'
    items = []
    for r in reviews:
        author = html_lib.escape(r.author)
        rating = r.author_details.rating if r.author_details else None
        rating_html = f' <span class="rev-rating">{rating:.1f}/10</span>' if rating else ""
        content = html_lib.escape(r.content[:300])
        items.append(
            f'<div class="review"><strong>{author}</strong>{rating_html}'
            f'<p>{content}…</p></div>'
        )
    return "\n".join(items)


def _movie_card(row: Row) -> str:
    m = row.movie
    imdb = row.imdb_url
    imdb_html = f'<a class="imdb badge" href="{html_lib.escape(imdb)}">IMDB ↗</a>' if imdb else ""
    rt_html = ""
    if row.rt_score and row.rt_url:
        rt_html = (
            f'<a class="rt badge" href="{html_lib.escape(row.rt_url)}" title="Rotten Tomatoes">'
            f'🍅 {html_lib.escape(row.rt_score)}</a>'
        )
    lb_html = (
        f'<a class="lb badge" href="{html_lib.escape(row.lb_url)}" title="Letterboxd">'
        f'Letterboxd ↗</a>'
    )
    jw_html = (
        f'<a class="jw badge" href="{html_lib.escape(row.jw_url)}" title="JustWatch">'
        f'JustWatch ↗</a>'
    )
    # ponytail: data-providers drives the sidebar filter — pipe-separated TOP_PROVIDER names
    # that appear in this movie's flatrate list (case-insensitive substring)
    prov_names = "|".join(
        p for p in TOP_PROVIDERS
        if any(p.lower() in f.provider_name.lower() for f in row.providers.flatrate)
    )
    return (
        f'<article class="movie" data-providers="{html_lib.escape(prov_names)}">'
        f'{row.poster_html}'
        f'<div class="info">'
        f'<h2>{html_lib.escape(m.display_title)} <span class="year">({m.display_year})</span> '
        f'<span class="rating">{m.vote_average:.1f}/10</span> '
        f'<span class="votes">{m.vote_count:,} votes</span> '
        f'{imdb_html}{rt_html}{lb_html}{jw_html}</h2>'
        f'<p class="overview">{html_lib.escape(m.overview[:300])}</p>'
        f'{_keywords_block(row.keywords)}'
        f'{_long_plot_block(row.long_plot)}'
        f'<div class="reviews"><h3>Top 3 reviews</h3>{_review_block(row.reviews)}</div>'
        f'<div class="providers"><strong>Streaming:</strong> {_provider_badges(row.providers)}</div>'
        f"</div></article>"
    )


def _html_template(body: str, sidebar: str = "") -> str:
    """Render the full HTML page with optional sidebar (provider filter)."""
    # ponytail: simpler background — single muted solid + subtle top gradient, no curtains
    # ponytail: CSS variables drive both themes; [data-theme="light"] overrides on <html>
    # ponytail: vanilla JS for theme toggle + provider filter — no build step, no external deps
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Watch Recommendations</title>'
        # ponytail: theme vars — dark by default; toggle flips data-theme on <html>
        '<style>'
        ':root{'
        '--bg:#15110d;--bg-card:#1f1a14;--text:#ece4d3;--text-muted:#a89880;'
        '--accent:#d4a017;--border:#3a3128;--link:#7cb6ff;--curtain:#2a1d12'
        '}'
        '[data-theme="light"]{'
        '--bg:#faf7f2;--bg-card:#ffffff;--text:#1a1109;--text-muted:#6b5a3e;'
        '--accent:#b8881a;--border:#d4c8b0;--link:#1f6fdc;--curtain:#e8e0d0'
        '}'
        '*{box-sizing:border-box}'
        'html,body{margin:0;padding:0}'
        'body{font:14px/1.5 -apple-system,system-ui,sans-serif;'
        'color:var(--text);background:var(--bg);min-height:100vh;'
        'display:grid;grid-template-columns:220px 1fr;gap:1.5rem;max-width:1100px;margin:0 auto;padding:2rem 1rem}'
        'main{min-width:0}'
        'h1{color:var(--accent);letter-spacing:.05em;margin:0 0 1.5rem;font-size:1.5rem;'
        'border-bottom:1px solid var(--border);padding-bottom:1rem;display:flex;align-items:center;gap:.5rem}'
        # ponytail: theme toggle — fixed top-right, minimal text button
        '.theme-toggle{position:fixed;top:.75rem;right:1rem;background:var(--bg-card);'
        'border:1px solid var(--border);color:var(--text);padding:.35rem .75rem;'
        'border-radius:4px;cursor:pointer;font-size:.8rem;font-family:inherit;z-index:10}'
        '.theme-toggle:hover{border-color:var(--accent)}'
        # ponytail: sidebar — sticky, scrolls with page; provider checkboxes
        '.sidebar{position:sticky;top:1rem;align-self:start;'
        'background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:1rem;'
        'font-size:.85rem;max-height:calc(100vh - 2rem);overflow-y:auto}'
        '.sidebar h2{margin:0 0 .75rem;font-size:.85rem;color:var(--text-muted);'
        'text-transform:uppercase;letter-spacing:.05em;font-weight:600}'
        '.provider-filter label{display:flex;align-items:center;gap:.4rem;padding:.25rem 0;cursor:pointer}'
        '.provider-filter input{accent-color:var(--accent)}'
        '.filter-info{font-size:.75rem;color:var(--text-muted);margin-top:.75rem;padding-top:.5rem;'
        'border-top:1px solid var(--border)}'
        # ponytail: mobile — sidebar collapses above main
        '@media (max-width:780px){body{grid-template-columns:1fr}.sidebar{position:static}}'
        # ponytail: movie card — simple flat panel, no gradient/glow
        '.movie{background:var(--bg-card);border:1px solid var(--border);border-radius:6px;'
        'padding:1.25rem;margin-bottom:1.25rem;display:flex;gap:1.25rem}'
        # ponytail: poster — fixed display width, height auto (preserves 2:3 ratio),
        # align-self:flex-start prevents stretching when card is taller than image
        '.poster{width:180px;height:auto;flex-shrink:0;align-self:flex-start;'
        'border-radius:4px;border:1px solid var(--border)}'
        '.info{flex:1;min-width:0}'
        '.movie h2{margin:0 0 .5rem;color:var(--text);font-size:1.15rem;font-weight:600}'
        '.year{color:var(--text-muted);font-weight:400;font-style:italic;font-size:.95em;margin-left:.25rem}'
        '.rating{background:var(--accent);color:#000;padding:.1rem .5rem;border-radius:3px;'
        'font-size:.8rem;margin-left:.5rem;font-weight:600}'
        '.votes{color:var(--text-muted);font-size:.8rem;margin-left:.5rem}'
        '.badge{display:inline-block;padding:.15rem .55rem;border-radius:3px;font-size:.75rem;'
        'margin-right:.35rem;margin-top:.15rem;background:var(--border);color:var(--text);'
        'border:1px solid var(--border);text-decoration:none;font-weight:600}'
        '.imdb{background:#f5c518;color:#000;border-color:#caa310}'
        '.imdb:hover{background:#ffd028}'
        '.rt{background:#fa320a;color:#fff;border-color:#c82808}'
        '.rt:hover{background:#ff4a2a}'
        '.lb{background:#00e054;color:#14181c;border-color:#00b545}'
        '.lb:hover{background:#00f064}'
        '.jw{background:#fff;color:#14181c;border-color:#dadce0}'
        '.jw:hover{background:#f1f3f4}'
        '.overview{color:var(--text-muted);font-style:italic;margin:.5rem 0}'
        '.keywords{margin:.5rem 0;line-height:1.8}'
        '.keyword{display:inline-block;padding:.1rem .5rem;margin:0 .25rem .25rem 0;'
        'border-radius:10px;font-size:.7rem;background:var(--curtain);color:var(--text-muted);'
        'border:1px solid var(--border)}'
        '.long-plot{color:var(--text-muted);font-size:.9rem;line-height:1.6;margin-top:.5rem}'
        '.reviews{margin-top:1rem}'
        '.reviews h3{font-size:.85rem;margin:0 0 .5rem;color:var(--text-muted);'
        'text-transform:uppercase;letter-spacing:.05em;font-weight:600}'
        '.review{margin-bottom:.75rem;padding-left:.75rem;border-left:3px solid var(--border);'
        'color:var(--text-muted)}'
        '.review strong{color:var(--text)}'
        '.review p{margin:.25rem 0 0}'
        '.rev-rating{color:var(--accent);font-size:.8rem;margin-left:.25rem}'
        '.providers{margin-top:1rem;padding-top:.75rem;border-top:1px dashed var(--border)}'
        '.providers strong{color:var(--text-muted);font-size:.8rem;margin-right:.25rem;'
        'text-transform:uppercase;letter-spacing:.05em;font-weight:600}'
        '.streaming-badge{display:inline-block;padding:.15rem .65rem;border-radius:3px;'
        'font-size:.75rem;margin-right:.35rem;margin-top:.15rem;background:var(--border);'
        'color:var(--text);border:1px solid var(--border)}'
        '.muted{color:var(--text-muted);font-style:italic}'
        '</style>'
        # ponytail: theme is set before paint to avoid FOUC; persists in localStorage
        '<script>'
        '(function(){'
        'var saved=localStorage.getItem("theme");'
        'var prefersLight=window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches;'
        'document.documentElement.dataset.theme=saved||(prefersLight?"light":"dark");'
        '})();'
        '</script>'
        '</head><body>'
        f'{sidebar}'
        '<main>'
        '<h1>🍿 Watch Recommendations</h1>'
        f'{body}'
        '</main>'
        # ponytail: theme toggle button — single-quote attribute delimiters so JS string literals
        # (which use double quotes) don't terminate the attribute value prematurely
        '<button class="theme-toggle" onclick=\''
        'var h=document.documentElement,t=h.dataset.theme==="light"?"dark":"light";'
        'h.dataset.theme=t;localStorage.setItem("theme",t);this.textContent=t==="light"?"☀ Light":"🌙 Dark"'
        "'>🌙 Dark</button>"
        # ponytail: provider filter — reads data-providers attr on each .movie article
        '<script>'
        'function applyProviderFilter(){'
        'var checked=new Set(Array.from(document.querySelectorAll(".provider-filter input:checked")).map(function(i){return i.value}));'
        'var articles=document.querySelectorAll(".movie");var visible=0;'
        'articles.forEach(function(a){'
        'var ps=(a.dataset.providers||"").split("|").filter(Boolean);'
        'var show=checked.size===0||ps.some(function(p){return checked.has(p)});'
        'a.style.display=show?"":"none";if(show)visible++;'
        '});'
        'var info=document.getElementById("filter-count");'
        'if(info)info.textContent=checked.size===0?("showing all "+articles.length):(visible+" of "+articles.length+" shown");'
        '}'
        'document.querySelectorAll(".provider-filter input").forEach(function(i){'
        'i.addEventListener("change",applyProviderFilter);'
        '});'
        'applyProviderFilter();'
        '</script>'
        "</body></html>"
    )


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
        # ponytail: detect media type by presence of `title` (movie) vs `name` (tv) — routes to /movie/ or /tv/ endpoints
        media_type = "tv" if movie.is_tv else "movie"
        imdb_url = get_imdb_url(media_type, movie.id, key)
        imdb_id = _imdb_id_from_url(imdb_url) if imdb_url else None
        long_plot: Optional[str] = None
        if omdb_key and imdb_id:
            long_plot = omdb_long_plot(imdb_id, omdb_key)
        rows.append(Row(
            movie=movie,
            media_type=media_type,
            poster_html=poster_img(movie, base_url),
            imdb_url=imdb_url,
            providers=get_providers(media_type, movie.id, key, region),
            reviews=get_reviews(media_type, movie.id, key, n=reviews_n),
            keywords=get_keywords(media_type, movie.id, key),
            long_plot=long_plot,
            rt_score=omdb_rt_score(imdb_id, omdb_key) if (omdb_key and imdb_id) else None,
            # ponytail: RT search URL — empty string if no omdb_key (rt_html block skipped in template)
            rt_url=(f"https://www.rottentomatoes.com/search?search={urllib.parse.quote(movie.display_title)}"
                    if omdb_key else ""),
            # ponytail: Letterboxd + JustWatch search URLs (no public slug lookup)
            lb_url=f"https://letterboxd.com/search/{urllib.parse.quote(movie.display_title)}/",
            jw_url=f"https://www.justwatch.com/us/search?q={urllib.parse.quote(movie.display_title)}",
        ))
    body = "\n".join(_movie_card(r) for r in rows)
    sidebar = _sidebar_html()
    with open(out_path, "w") as f:
        f.write(_html_template(body, sidebar))
    return out_path


def _sidebar_html() -> str:
    """Sidebar with 10 provider checkboxes (unchecked by default — all cards visible until narrowed)."""
    rows = "".join(
        f'<label><input type="checkbox" value="{html_lib.escape(p)}"> {html_lib.escape(p)}</label>'
        for p in TOP_PROVIDERS
    )
    return (
        '<aside class="sidebar">'
        '<h2>Filter by provider <span class="hint">(check to narrow)</span></h2>'
        f'<div class="provider-filter">{rows}</div>'
        '<div class="filter-info" id="filter-count"></div>'
        '</aside>'
    )


if __name__ == "__main__":
    # ponytail: smoke check — the plot cache must actually memoize (second call hits, no re-fetch).
    # Uses a local decorated function so the check costs no network I/O.
    _calls = []

    @_plot_cache
    def _cache_probe(x: str) -> str:
        _calls.append(x)
        return x.upper()

    _cache_probe("_smoke_")
    _cache_probe("_smoke_")
    if "diskcache" in sys.modules:
        assert len(_calls) == 1, f"plot cache not memoizing (called {len(_calls)}x)"
        _cache_probe.__cache_key__ and _CACHE.delete(_cache_probe.__cache_key__("_smoke_"))

    # ponytail: smoke check — blockbuster quota caps big-vote films in the top 10 without dropping them
    _bb = [{"title": f"big{i}", "vote_count": 20000, "_score": 10 - i} for i in range(6)]
    _sm = [{"title": f"sml{i}", "vote_count": 500, "_score": 5 - i} for i in range(8)]
    _q = apply_blockbuster_quota(_bb + _sm)
    assert sum(1 for m in _q[:10] if m["vote_count"] >= 8000) == 3, "at most 3 blockbusters in top 10"
    assert len(_q) == 14, "quota reorders, never drops"
    assert [m["title"] for m in _q[:3]] == ["big0", "big1", "big2"], "score order kept within head"
    # ponytail: when small films run short the head backfills with blockbusters rather than under-filling
    _short = apply_blockbuster_quota(_bb + _sm[:2])
    assert len(_short) == 8 and _short[:1][0]["title"] == "big0", "backfill when small pool is thin"
    # ponytail: all-blockbuster pool must still return them — quota is a cap, not a requirement
    assert len(apply_blockbuster_quota(_bb)) == 6, "all-blockbuster pool survives"
    assert apply_blockbuster_quota([]) == [], "empty pool is not fatal"

    # ponytail: smoke check — plot_queries_search in offline mode takes max-of-queries per film.
    # Forces the no-LLM path with monkeypatching so the check costs no network or Anthropic tokens.
    import contextlib, io as _io
    _pool = [
        {"id": 1, "title": "Movie A", "overview": "lawyer courtroom thriller innocent", "vote_count": 100},
        {"id": 2, "title": "Movie B", "overview": "spy assassin thriller", "vote_count": 100},
    ]
    def _patched_get(path, params, *, key):
        return {"results": [
            {"id": 1, "title": "Movie A", "overview": "lawyer courtroom thriller innocent",
             "vote_count": 100},
            {"id": 2, "title": "Movie B", "overview": "spy assassin thriller", "vote_count": 100},
        ]}
    def _patched_wp(_): return None
    # ponytail: A's plot matches BOTH query phrasings; B's matches neither; the union prefilter keeps A.
    # Patched get_imdb_id returns a valid IMDb id so _full_text reaches wikipedia_plot_from_imdb;
    # without it the external_ids lookup misses and _full_text falls back to the TMDB overview,
    # which only overlaps each phrasing on 1-2 tokens — max score 4 vs STRICT_FLOOR=4 → A dropped.
    # IMDb ids are per TMDB id so thin_rescue doesn't smuggle A's plot into B's text.
    _A_PLOT = "the protagonist is an innocent man wrongfully convicted by a clever lawyer in court"
    _imdb_for_id = {1: "tt0000001", 2: "tt0000002"}
    _plot_for_imdb = {"tt0000001": _A_PLOT}
    def _patched_get_imdb(media_type, item_id, key):
        return _imdb_for_id.get(item_id)
    def _patched_wpimdb(imdb_id):
        return _plot_for_imdb.get(imdb_id)
    import sys as _sys
    _mod = _sys.modules[__name__]
    _orig_get, _orig_get_imdb, _orig_wp, _orig_wpimdb = _mod.get, _mod.get_imdb_id, _mod.wikipedia_plot, _mod.wikipedia_plot_from_imdb
    _mod.get, _mod.get_imdb_id, _mod.wikipedia_plot, _mod.wikipedia_plot_from_imdb = (
        _patched_get, _patched_get_imdb, _patched_wp, _patched_wpimdb)
    # ponytail: thin_rescue prints [thin_rescue]... when its score floor culls a list; suppress during
    # smoke asserts so --help and other CLI invocations don't print it. (Real CLI runs that hit the
    # marker are outside any redirect context.)
    _smoke_buf = _io.StringIO()
    try:
        # ponytail: queries chosen so A's long plot overlaps each phrasing on >=3 stemmed tokens →
        # max score >= 6 > STRICT_FLOOR(4); B's overview has no overlap on either phrasing.
        with contextlib.redirect_stdout(_smoke_buf):
            result = plot_queries_search(
                ["innocent man wrongfully convicted", "clever lawyer in court"], "fakekey")
        titles = sorted(m["title"] for m in result)
        assert titles == ["Movie A"], f"only A matches both phrasings (max-of-queries rescues B), got {titles}"
    finally:
        _mod.get, _mod.get_imdb_id, _mod.wikipedia_plot, _mod.wikipedia_plot_from_imdb = (
            _orig_get, _orig_get_imdb, _orig_wp, _orig_wpimdb)

    # ponytail: smoke check — thin_rescue leaves a full list alone and expands a thin one
    import contextlib, io
    full = [{"id": i, "title": f"f{i}", "vote_count": 100, "_score": 5} for i in range(7)]
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        kept = thin_rescue(list(full), ["anything"], "fakekey")
    assert [m["id"] for m in kept] == [m["id"] for m in full], "full list returns unchanged"

    # ponytail: thin list triggers rescue — patches out network IO so the test is offline
    thin = [{"id": 1, "title": "Seed", "vote_count": 100, "_score": 5}]
    def _rec_get(path, params, *, key):
        # /movie/{id}/recommendations returns a single neighbor; /search/movie returns the seed.
        if "/recommendations" in path:
            return {"results": [{"id": 99, "title": "Rescued Movie", "overview": "innocent man proves himself in court"}]}
        if "/search/movie" in path:
            return {"results": [{"id": 1, "title": "Seed"}]}
        return {"results": []}
    def _rec_wpimdb(_): return "an innocent man wrongfully accused proves his innocence in court"
    _mod.get = _rec_get
    _mod.wikipedia_plot = lambda _: None
    _mod.wikipedia_plot_from_imdb = _rec_wpimdb
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rescued = thin_rescue(list(thin), ["innocent man proves himself"], "fakekey")
        # neighbor survives the floor (plot mentions all key query words via token-overlap)
        ids = {m["id"] for m in rescued}
        assert 99 in ids, f"rescue added neighbor, got ids {ids}"
    finally:
        _mod.get, _mod.wikipedia_plot, _mod.wikipedia_plot_from_imdb = _orig_get, _orig_wp, _orig_wpimdb

    # ponytail: smoke check — quick mode skips Wikipedia fetches and uses TMDB overview + OMDb plot.
    # Tracks which fetchers were called without making any network requests.
    _calls = {"wiki": 0, "omdb": 0}

    def _track_wp(_): _calls["wiki"] += 1; return None
    def _track_wpimdb(_): _calls["wiki"] += 1; return "stale cached wiki plot"
    def _track_omdb_plot(*a): _calls["omdb"] += 1; return None
    _orig_omdb = _mod.omdb_long_plot
    _mod.wikipedia_plot = _track_wp
    _mod.wikipedia_plot_from_imdb = _track_wpimdb
    _mod.omdb_long_plot = _track_omdb_plot
    _mod.get_imdb_id = lambda *a, **kw: "tt0000001"
    _omdb_hit = "a detailed courtroom scenario plays out over many minutes"
    _film = {"id": 1, "overview": "innocent man proves himself"}
    try:
        # ponytail: with OMDb hitting (tracker's default returns None — substitute one that hits)
        _mod.omdb_long_plot = lambda *a, **kw: _omdb_hit
        text_quick = _mod._quick_text(_film, "x")
        assert _calls["wiki"] == 0, "quick mode must not call Wikipedia"
        assert text_quick == _omdb_hit, "OMDb long plot wins when available"
        # ponytail: OMDb missing (env unset) — quick_text falls back to overview
        _calls["omdb"] = 0
        _mod.omdb_long_plot = lambda *a, **kw: None
        # os.environ.get('OMDB_API_KEY') may be set in this shell; force the "no key" branch
        _prev = os.environ.pop("OMDB_API_KEY", None)
        try:
            # ponytail: with no IMDb id, OMDb won't be called and overview returns directly
            _mod.get_imdb_id = lambda *a, **kw: None
            text_no_imdb = _mod._quick_text(_film, "x")
            assert _calls["omdb"] == 0, "no IMDb id means no OMDb call"
            assert text_no_imdb == "innocent man proves himself", "overview is the fallback text"
        finally:
            if _prev is not None: os.environ["OMDB_API_KEY"] = _prev
        # ponytail: full mode (no quick flag) hits Wikipedia first — restore get_imdb_id too
        _mod.get_imdb_id = lambda *a, **kw: "tt0000001"
        _calls["wiki"] = 0
        _mod.wikipedia_plot = _track_wp
        _mod.wikipedia_plot_from_imdb = _track_wpimdb
        text_full = _mod._full_text(_film, "x")
        assert _calls["wiki"] >= 1, "full mode uses Wikipedia fetcher"
        assert text_full == "stale cached wiki plot", "full mode prefers the cached wikitext"
    finally:
        _mod.wikipedia_plot = _orig_wp
        _mod.wikipedia_plot_from_imdb = _orig_wpimdb
        _mod.omdb_long_plot = _orig_omdb

    # ponytail: smoke check — gemini_recommendations parses Gemini's numbered list, handles year
    # suffixes in parens, drops lines, and resolves via TMDB. Mocked end-to-end (no network).
    import os as _os
    _mod = _sys.modules[__name__]
    _gemini_response = json.dumps({
        "candidates": [{"content": {"parts": [{"text":
            "1. Idiocrazy (2006)\n"
            "2. Bulworth\n"
            "3. The Death of Stalin (2017) - Soviet satire\n"
            "4. In the Loop\n"
            "5. Some Hallucinated Garbage (2030)\n"
        }]}}]
    }).encode()
    _fake_results = {
        0: {"id": 1, "title": "Idiocrazy", "vote_count": 100},  # typo'd, gemini-voted most often
        1: {"id": 2, "title": "Bulworth", "vote_count": 100},
        2: {"id": 3, "title": "The Death of Stalin", "vote_count": 100},
        3: {"id": 4, "title": "In the Loop", "vote_count": 100},
    }
    def _fake_urlopen_gemini(req, timeout=10):
        from urllib.error import HTTPError
        return type("Resp", (), {"read": lambda self: _gemini_response, "__enter__": lambda s: s, "__exit__": lambda *a: None})()

    class _FakeResp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=10):
        # ponytail: dispatch on URL — Gemini calls go to fake_urlopen_gemini body, TMDB calls return results
        from urllib.parse import urlparse
        url = req.get_full_url() if hasattr(req, "get_full_url") else req
        if "generativelanguage" in url:
            return _FakeResp(_gemini_response)
        # /search/movie?query=X — pull 'query' from the URL string itself (urllib.Request has .selector + parse_qs)
        from urllib.parse import parse_qs
        qs = parse_qs(url.split("?", 1)[1])
        query = qs.get("query", [""])[0]
        idx = {"Idiocrazy": 0, "Bulworth": 1, "The Death of Stalin": 2, "In the Loop": 3}.get(query, None)
        if idx is None:
            return _FakeResp(json.dumps({"results": []}).encode())
        return _FakeResp(json.dumps({"results": [_fake_results[idx]]}).encode())

    _prev_gemini = _os.environ.get("GEMINI_API_KEY")  # ponytail: smoke asserts must restore the
                                                      # caller's env, not delete it — a previous version
                                                      # did pop() and silently unset the key for any
                                                      # CLI invocation that ran after the smoke.
    _os.environ["GEMINI_API_KEY"] = "fakekey"
    _orig_urlopen = _mod.urllib.request.urlopen
    _mod.urllib.request.urlopen = _fake_urlopen
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = gemini_recommendations("whatever", "fakekey", n=5)
        # ponytail: 5 lines parsed, "Some Hallucinated Garbage" dropped, 4 returned with TMDB ids
        titles = [m["title"] for m in result]
        assert titles == ["Idiocrazy", "Bulworth", "The Death of Stalin", "In the Loop"], f"parse+resolve failed: {titles}"
    finally:
        _mod.urllib.request.urlopen = _orig_urlopen
        if _prev_gemini is None:
            _os.environ.pop("GEMINI_API_KEY", None)
        else:
            _os.environ["GEMINI_API_KEY"] = _prev_gemini

    # ponytail: smoke check — n-gram keyword matching prefers the longest phrase and skips stopword-only grams
    _fake_catalog = {"time travel": 4379, "time": 999, "dimension": 1234}
    assert keywords_in_query("time traveling, plot twists", _fake_catalog) == [4379], \
        "longest-phrase match ('time travel' must beat bare 'time')"
    assert keywords_in_query("a dimension of the mind", _fake_catalog) == [1234], "1-gram fallback"
    assert keywords_in_query("the a of", _fake_catalog) == [], "stopword-only query matches nothing"
    assert keywords_in_query("anything", {}) == [], "empty catalog is not fatal"

    # ponytail: smoke check — shortlist tolerates missing fields
    assert [m["title"] for m in shortlist([
        {"title": "A", "vote_average": 8.0},
        {"title": "B"},
    ])] == ["A", "B"]

    # ponytail: smoke check — print_movies must not KeyError on TV results (name/first_air_date, no `title`)
    import io, contextlib
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        print_movies([{"id": 1, "name": "Malcolm", "first_air_date": "2000-01-09", "vote_average": 8.0}])
    assert "Malcolm (2000)" in _buf.getvalue(), "TV rows render in text output"

    # ponytail: smoke check — HTML escapes titles, keeps flatrate-only filter, includes poster + IMDB + popcorn emoji
    fake_row = Row(
        movie=Movie(id=1, title="<script>", release_date="2020-01-01",
                    vote_average=7.5, vote_count=100, overview="x",
                    poster_path="/abc.jpg"),
        media_type="movie",
        poster_html='<img class="poster" src="x" alt="">',
        imdb_url="https://www.imdb.com/title/tt123/",
        providers=Provider(
            flatrate=[ProviderEntry(provider_name="Netflix")],
            rent=[ProviderEntry(provider_name="Apple TV")],
            buy=[],
        ),
        reviews=[],
    )
    card_html = _movie_card(fake_row)
    html_doc = _html_template(card_html, _sidebar_html())
    assert "&lt;script&gt;" in html_doc, "XSS escaping"
    assert "Netflix" in card_html and "Apple TV" not in card_html, "flatrate-only filter on card"
    assert '<img class="poster"' in card_html, "poster present"
    assert 'class="imdb' in card_html, "IMDB link present"
    assert 'class="lb' in card_html, "Letterboxd link present"
    assert 'class="jw' in card_html, "JustWatch link present"
    assert 'data-providers="Netflix"' in card_html, "provider filter data attribute on card"
    assert "🍿" in html_doc, "popcorn emoji in header"
    assert "aspect-ratio:2/3" not in html_doc, "poster no longer cropped"
    assert "<!DOCTYPE" in html_doc
    # ponytail: sidebar lists all 10 TOP_PROVIDERS as checkboxes
    assert html_doc.count('class="provider-filter"') == 1
    for p in TOP_PROVIDERS:
        assert p in html_doc, f"sidebar missing provider {p}"

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
    # ponytail: pass each as a separate --plot-queries arg, e.g. --plot-queries "foo" "bar" — argparse's
    # nargs="+" appends in order; mutually exclusive with --plot-query (enforced in main())
    parser.add_argument("--plot-queries", metavar="QUERY", nargs="+",
                        help="Multiple plot queries: union the pools, score each film against every one, keep the max")
    parser.add_argument("--quick", action="store_true",
                        help="Skip Wikipedia fetches and LLM scoring; use TMDB overview + OMDb long plot with token-overlap (fast, lower quality)")
    # ponytail: --google is the DEFAULT for --plot-query when this flag is absent. Use --no-google
    # to fall back to the legacy TMDB-keyword-pool + LLM rerank pipeline (offline-capable, slower,
    # LLM-judged). --google explicitly is accepted for backwards-compatibility.
    parser.add_argument("--google", action="store_true", default=True,
                        help="(default) Use Gemini to recommend titles; with --plot-queries runs one call per phrasing")
    parser.add_argument("--no-google", dest="google", action="store_false",
                        help="Use the TMDB keyword pool + per-film LLM scoring instead of Gemini")
    # ponytail: --nationality filters every result mode (keyword/similar/actor/plot) to films whose
    # origin_country list contains this ISO 3166-1 alpha-2 code. Trivial filter on TMDB's existing
    # origin_country field — no extra API calls. Pair with --plot-query for "best films from X".
    parser.add_argument("--nationality", metavar="CC",
                        help="ISO 3166-1 alpha-2 country code (e.g. HU, NO, JP, FR); keeps only films from that country")
    args = parser.parse_args()

    key = os.environ["TMDB_API_KEY"]
    omdb_key = None if args.no_omdb or args.google else os.environ.get("OMDB_API_KEY")

    if args.plot_query and args.plot_queries:
        # ponytail: parser.error prints usage and exits 2 — clean error reporting vs a bare sys.exit
        parser.error("--plot-query and --plot-queries are mutually exclusive")
    if args.plot_queries:
        # ponytail: --plot-queries works with both Gemini (--google default) and legacy (--no-google).
        # Gemini mode runs one call per query and unions; legacy uses max-of-queries per film.
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
        # ponytail: --google is the default — one Gemini call, then TMDB lookup per title,
        # then straight to render. Skip the keyword pool and LLM scoring; --quick semantics are
        # implied. --no-google restores the legacy TMDB-keyword-pool + LLM-rerank pipeline for users
        # who need it offline or want LLM-judged results over Gemini's text-only output.
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

    # ponytail: plot_query_search / plot_queries_search already rank by LLM relevance and apply the
    # blockbuster quota — re-running shortlist() would re-sort by vote_average and discard both.
    # Other modes are unranked.
    if args.nationality:
        cc = args.nationality.upper()
        lang = cc.lower()
        # ponytail: TMDB /search/movie (used by Gemini resolver) doesn't carry origin_country — only
        # original_language. Check both: a film made in Hungary is also released in Hungarian. For
        # English-language films the user can drop --nationality; it's a coarse signal either way.
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
        # ponytail: HTML filename keys off the actual query so re-runs with different prompts don't
        # silently overwrite each other. --out overrides.
        if args.out:
            out_path = args.out
        else:
            slug_src = args.plot_query or (" ".join(args.plot_queries) if args.plot_queries else args.keyword or "recommendations")
            out_path = f"watch_{slug_for_query(slug_src)}.html"
        path = render_html(movies, key, out_path=out_path, reviews_n=args.reviews, omdb_key=omdb_key)
        # ponytail: print the file path as a clickable file:// link via ANSI escape so iTerm2/Terminal.app
        # users can ctrl-click to open. The escape is no-op on older terminals.
        clickable = f"\033]8;;file://{os.path.abspath(path)}\033\\{path}\033]8;;\033\\"
        print(f"wrote {clickable}")
    else:
        print_movies(movies, key=key if key else None)
