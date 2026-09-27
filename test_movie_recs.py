#!/usr/bin/env python3
"""Checks for movie_recs. Stdlib only, no network, no Anthropic tokens.

Run bare:          python3 test_movie_recs.py
Run under pytest:  pytest test_movie_recs.py

Plain asserts on purpose — pytest collects these as-is, and the bare runner below
means the skill stays testable without installing anything.

Kept deliberately small: each test below guards a branch or an invariant that has
actually broken, or that the surrounding code makes easy to break. Markup that
merely repeats what the template already guarantees is not tested.
"""
import contextlib
import io
import json
import os
import sys
import types
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import movie_recs as m


@contextlib.contextmanager
def patched(**attrs):
    """Temporarily set attributes on the movie_recs module; restores on exit."""
    old = {k: getattr(m, k) for k in attrs}
    for k, v in attrs.items():
        setattr(m, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(m, k, v)


@contextlib.contextmanager
def quiet():
    """Capture stdout+stderr so diagnostics don't pollute the test output."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield buf


class FakeResp:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@contextlib.contextmanager
def fake_urlopen(handler):
    """Swap urllib.request.urlopen for `handler`; counts calls in `.calls`."""
    box = types.SimpleNamespace(calls=[])

    def stub(req, timeout=10):
        url = req.get_full_url() if hasattr(req, "get_full_url") else req
        box.calls.append(url)
        return handler(url)

    real = urllib.request.urlopen
    urllib.request.urlopen = stub
    try:
        yield box
    finally:
        urllib.request.urlopen = real


OMDB_BODY = (b'{"Plot":"A full paragraph.","Response":"True",'
             b'"Ratings":[{"Source":"Rotten Tomatoes","Value":"91%"}]}')


# --- plot cache -------------------------------------------------------------

def test_plot_cache_memoizes():
    """A second call must hit the cache. Without it every run re-fetches every plot.

    Only meaningful with diskcache installed — without it `_plot_cache` is a no-op
    and the cache is inert, so say so loudly rather than passing vacuously.
    """
    if "diskcache" not in sys.modules:
        print("    (not exercised: diskcache not installed — the plot cache is inert here)")
        return
    calls = []

    @m._plot_cache
    def probe(x):
        calls.append(x)
        return x.upper()

    probe("_probe_")
    probe("_probe_")
    assert len(calls) == 1, f"plot cache not memoizing (called {len(calls)}x)"


# --- blockbuster quota ------------------------------------------------------

def test_blockbuster_quota():
    """The cap, the no-drop guarantee, ordering, and the two backfill edges."""
    bb = [{"title": f"big{i}", "vote_count": 20000, "_score": 10 - i} for i in range(6)]
    sm = [{"title": f"sml{i}", "vote_count": 500, "_score": 5 - i} for i in range(8)]
    q = m.apply_blockbuster_quota(bb + sm)
    assert sum(1 for x in q[:10] if x["vote_count"] >= 8000) == 3, "at most 3 blockbusters in top 10"
    assert len(q) == 14, "quota reorders, never drops"
    assert [x["title"] for x in q[:3]] == ["big0", "big1", "big2"], "score order kept within head"
    short = m.apply_blockbuster_quota(bb + sm[:2])
    assert len(short) == 8 and short[:1][0]["title"] == "big0", "backfill when small pool is thin"
    assert len(m.apply_blockbuster_quota(bb)) == 6, "all-blockbuster pool survives"
    assert m.apply_blockbuster_quota([]) == [], "empty pool is not fatal"
    # The quota never drops, only reorders — so the backfill is only observable in the
    # ordering. When the small pool is too thin to fill the head, the head must be
    # topped up from the demoted blockbusters and then sorted; without that the tail
    # is left unsorted and 3 of the 10 slots sit empty.
    bb6 = [{"title": f"b{i}", "vote_count": 20000, "_score": 10 - i} for i in range(6)]
    sm4 = [{"title": f"s{i}", "vote_count": 500, "_score": 9 - i} for i in range(4)]
    filled = m.apply_blockbuster_quota(bb6 + sm4)
    assert len(filled) == 10
    assert [x["_score"] for x in filled] == sorted((x["_score"] for x in filled), reverse=True), \
        "backfilled head must come back fully sorted"


def test_quota_preserves_films_that_look_identical():
    """Dedup is by object identity, not dict equality: two entries with identical
    fields compare equal, so the tail copy would silently vanish. The duplicate has
    to sit past the head boundary while its twin lands inside it — only the tail is
    filtered, so that's the one arrangement where the two implementations differ."""
    ranked = [{"id": i, "title": f"F{i}", "vote_count": 500, "_score": 10 - i} for i in range(12)]
    ranked[7] = dict(ranked[0])  # same values, separate object
    out = m.apply_blockbuster_quota(ranked)
    assert len(out) == 12, "quota must not drop a film that merely looks like another"


def test_quota_runs_when_rescue_is_skipped():
    """Regression: the quota used to live inside thin_rescue's rescue branch only, so
    it never fired whenever the shortlist was already long enough to skip rescue —
    which is the normal case. This drives the real plot_queries_search (rather than
    calling the quota itself, which would pass either way) and checks its output.

    Eight blockbusters are ranked above eight small films, so without the quota the
    top ten is all blockbusters. The identical overviews make every film score the
    same, so the ordering that survives to the top is the input order.
    """
    query = "innocent man proves himself in court"
    overview = f"A film about {query} and its consequences for everyone involved."
    films = [{"id": i, "title": f"B{i}", "vote_count": 20000, "overview": overview} for i in range(8)]
    films += [{"id": 100 + i, "title": f"S{i}", "vote_count": 500, "overview": overview} for i in range(8)]

    # Force the offline token-overlap path: no key, no SDK, no Wikipedia, no catalog fetch.
    env = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        with patched(get=lambda path, params, *, key: {"results": films},
                     keyword_catalog=lambda force=False: {},
                     get_imdb_id=lambda *a, **kw: None,
                     wikipedia_plot=lambda _: None,
                     wikipedia_plot_from_imdb=lambda _: None), quiet():
            out = m.plot_queries_search([query], "fakekey")
    finally:
        for k, v in env.items():
            if v is not None:
                os.environ[k] = v
    assert len(out) >= 10, f"expected a full shortlist, got {len(out)}"
    assert sum(1 for x in out[:10] if (x.get("vote_count") or 0) >= 8000) <= 3, \
        f"quota not applied on the non-rescue path: {out[:10]}"


# --- scoring ----------------------------------------------------------------

def test_score_plot_ignores_punctuation():
    """Regression: "alien," scored 0 against plots containing "alien", which pushed
    films below STRICT_FLOOR and silently dropped them from the shortlist."""
    plot = "A young boy hides a friendly alien in his closet and the alien goes home."
    assert m._score_plot(plot, "boy hides alien") == m._score_plot(
        plot, "a boy hides an alien, all alone"
    ) > 4


def test_llm_score_plot_reads_the_text_block():
    """Regression: with thinking on, content[0] is a ThinkingBlock, so indexing .text
    raised AttributeError — swallowed by the bare except, degrading every film to
    token-overlap with no warning. stop_reason is the other half: max_tokens and
    refusal both mean there is no score to parse."""
    class Block:
        def __init__(self, type, **kw):
            self.type = type
            self.__dict__.update(kw)

    def install(stop_reason, content):
        class Resp:
            pass
        r = Resp()
        r.stop_reason = stop_reason
        r.content = content

        class Usage:
            input_tokens, output_tokens = 10, 2
        r.usage = Usage()

        class Messages:
            @staticmethod
            def create(**kw):
                return r

        fake = types.ModuleType("anthropic")
        fake.Anthropic = lambda **kw: types.SimpleNamespace(messages=Messages)
        fake.RateLimitError = type("RateLimitError", (Exception,), {})
        sys.modules["anthropic"] = fake

    prev = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "fake"
    m._LLM_USAGE = {"input": 0, "output": 0}
    try:
        install("end_turn", [Block("thinking", thinking=""), Block("text", text=" 7 ")])
        assert m._llm_score_plot("p", "q") == 7, "answer parsed out of the text block"
        assert m.get_llm_usage()["input"] > 0, "usage still accumulates"
        install("max_tokens", [Block("text", text="7")])
        assert m._llm_score_plot("p", "q") == -1, "max_tokens means no score, falls back"
        install("refusal", [Block("text", text="7")])
        assert m._llm_score_plot("p", "q") == -1, "refusal means no score, falls back"
    finally:
        sys.modules.pop("anthropic", None)
        if prev is not None:
            os.environ["ANTHROPIC_API_KEY"] = prev


# --- pool and ranking -------------------------------------------------------

def test_plot_queries_scores_against_every_query():
    """Offline mode. Three films, three different outcomes — the point being film C,
    which matches the SECOND phrasing and not the first, so it only survives if the
    score really is a max across queries. Scoring on the first query alone would cull
    it; a min would cull film A, which matches the first weakly and the second weakly
    but both enough to clear the floor.

    get_imdb_id is patched to a valid id so _full_text reaches the Wikipedia plot;
    otherwise it falls back to the TMDB overview, which overlaps each phrasing on
    only 1-2 tokens and everything would sit at or under STRICT_FLOOR. Ids are
    per-film so thin_rescue can't smuggle one film's plot into another's text.
    """
    plots = {
        1: "the protagonist is an innocent man wrongfully convicted by a lawyer",  # both
        2: "a spy assassin thriller about betrayal and loose ends",               # neither
        3: "a clever lawyer in court outmaneuvers the prosecution with a closing argument",  # 2nd only
    }

    def fake_get(path, params, *, key):
        return {"results": [
            {"id": i, "title": f"Film {i}", "overview": plots[i], "vote_count": 100}
            for i in sorted(plots)
        ]}

    with patched(
        get=fake_get,
        keyword_catalog=lambda force=False: {},
        get_imdb_id=lambda media_type, item_id, key: f"tt000000{item_id}",
        wikipedia_plot=lambda _: None,
        wikipedia_plot_from_imdb=lambda imdb: plots[int(imdb[-1])],
    ), quiet():
        result = m.plot_queries_search(
            ["innocent man wrongfully convicted", "clever lawyer in court"], "fakekey")
    titles = sorted(x["title"] for x in result)
    assert titles == ["Film 1", "Film 3"], \
        f"Film 3 only matches the 2nd phrasing and must survive on max; got {titles}"


# --- thin rescue ------------------------------------------------------------

def _rescue_patches(neighbour_overview="innocent man proves himself in court"):
    """Module patches for a rescue that pulls one CF neighbour, id 99.

    The neighbour's overview is its scoring text, so passing an off-topic string
    makes it a low-scoring neighbour the floor should reject.
    """
    def rec_get(path, params, *, key):
        if "/recommendations" in path:
            return {"results": [{"id": 99, "title": "Neighbour", "vote_count": 100,
                                 "overview": neighbour_overview}]}
        if "/search/movie" in path:
            return {"results": [{"id": 1, "title": "Seed"}]}
        return {"results": []}
    return dict(
        get=rec_get,
        wikipedia_plot=lambda _: None,
        wikipedia_plot_from_imdb=lambda _: neighbour_overview,
    )


def test_thin_rescue_expands_a_thin_list():
    with patched(**_rescue_patches()), quiet():
        rescued = m.thin_rescue([{"id": 1, "title": "Seed", "vote_count": 100, "_score": 5}],
                                ["innocent man proves himself"], "fakekey")
    assert 99 in {x["id"] for x in rescued}


def test_thin_rescue_culls_off_topic_neighbours():
    """CF neighbours are only a hint — the same score floor applies to them, so a
    neighbour that doesn't match the query is dropped rather than padding the list."""
    with patched(**_rescue_patches("a gentle comedy about gardening in autumn")), quiet():
        rescued = m.thin_rescue([{"id": 1, "title": "Seed", "vote_count": 100, "_score": 5}],
                                ["innocent man proves himself"], "fakekey")
    assert 99 not in {x["id"] for x in rescued}, "off-topic neighbour must be culled"


def test_thin_rescue_reachable_with_zero_survivors():
    """Regression: seeds came only from `scored`, so a floor that culled everything
    left nothing to expand from and the rescue silently did nothing — on exactly the
    queries that needed it most."""
    with patched(**_rescue_patches()), quiet() as buf:
        rescued = m.thin_rescue([], ["innocent man proves himself"], "fakekey",
                                seeds=[{"id": 1, "title": "Seed", "overview": "x"}])
    assert 99 in {x["id"] for x in rescued}, "prefilter seeds make the rescue reachable"
    assert "1 seeds" in buf.getvalue(), "log reports the real seed count"


# --- text extraction --------------------------------------------------------

def test_quick_text_skips_wikipedia_and_prefers_omdb():
    """The quick/full split: quick never touches Wikipedia and prefers OMDb's longer
    plot; full goes to Wikipedia first. Neither may silently call the other."""
    calls = {"wiki": 0}

    def track_wp(_):
        calls["wiki"] += 1
        return None

    def track_wpimdb(_):
        calls["wiki"] += 1
        return "a cached wikipedia plot"

    omdb_hit = "a detailed courtroom scenario plays out over many minutes"
    film = {"id": 1, "overview": "innocent man proves himself"}
    with patched(wikipedia_plot=track_wp, wikipedia_plot_from_imdb=track_wpimdb,
                 get_imdb_id=lambda *a, **kw: "tt0000001",
                 omdb_long_plot=lambda *a, **kw: omdb_hit):
        assert m._quick_text(film, "x") == omdb_hit, "OMDb long plot wins when available"
        assert calls["wiki"] == 0, "quick mode must not call Wikipedia"
        assert m._full_text(film, "x") == "a cached wikipedia plot", "full mode prefers Wikipedia"
        assert calls["wiki"] >= 1, "full mode uses the Wikipedia fetcher"

    # No IMDb id at all: OMDb can't be consulted, the overview is the text.
    with patched(get_imdb_id=lambda *a, **kw: None,
                 omdb_long_plot=lambda *a, **kw: pytest_fail("OMDb called without an IMDb id")):
        assert m._quick_text(film, "x") == "innocent man proves himself"


def pytest_fail(msg):
    raise AssertionError(msg)


def test_quick_text_makes_no_request_without_an_omdb_key():
    """Regression: the key was read inside the call, so an unset key still built a
    request with apikey=None — one guaranteed-to-fail round-trip per candidate film."""
    def handler(url):
        raise AssertionError(f"OMDb called with no key: {url}")

    prev = os.environ.pop("OMDB_API_KEY", None)
    try:
        with fake_urlopen(handler) as box, patched(get_imdb_id=lambda *a, **kw: "tt0000001"):
            text = m._quick_text({"id": 1, "overview": "ov"}, "k")
    finally:
        if prev is not None:
            os.environ["OMDB_API_KEY"] = prev
    assert box.calls == [], "no OMDb request when the key is unset"
    assert text == "ov", "falls back to the overview"


# --- HTML -------------------------------------------------------------------

def test_render_html_makes_one_omdb_request_per_movie():
    """Regression: render_html fetched the long plot and the RT score separately, so
    every card cost two identical round-trips. Also covers the OMDb-is-down path,
    where both fields are simply absent."""
    films = [{"id": 1, "title": "T", "release_date": "2020-01-01",
              "vote_average": 7.0, "vote_count": 10}]
    out = os.path.join(os.environ.get("TMPDIR", "/tmp"), "movie_recs_test.html")
    base = dict(get_config=lambda key: {"images": {"secure_base_url": "https://img.test/"}},
                get_providers=lambda *a, **kw: m.Provider(),
                get_reviews=lambda *a, **kw: [],
                get_keywords=lambda *a, **kw: [],
                get_imdb_id=lambda *a, **kw: "tt0000001")

    with patched(**base), fake_urlopen(lambda url: FakeResp(OMDB_BODY)) as box:
        m.render_html(films, "k", out_path=out, omdb_key="k")
    assert len(box.calls) == 1, f"expected 1 OMDb request per movie, made {len(box.calls)}"
    html = open(out, encoding="utf-8").read()
    assert "A full paragraph." in html, "long plot rendered"
    assert "91%" in html, "RT score rendered"

    # OMDb down: the page must still render, just without the two OMDb fields.
    with patched(**base), fake_urlopen(lambda url: (_ for _ in ()).throw(urllib.error.URLError("down"))):
        m.render_html(films, "k", out_path=out, omdb_key="k")
    assert "<!DOCTYPE" in open(out, encoding="utf-8").read()
    os.unlink(out)


def test_html_card_escapes_and_filters_providers():
    """XSS escaping is the security-relevant part; the rest guards the sidebar filter
    wiring, which spans Row -> card -> template -> sidebar."""
    row = m.Row(
        movie=m.Movie(id=1, title="<script>", release_date="2020-01-01",
                      vote_average=7.5, vote_count=100, overview="x", poster_path="/abc.jpg"),
        media_type="movie",
        poster_html='<img class="poster" src="x" alt="">',
        imdb_url="https://www.imdb.com/title/tt123/",
        providers=m.Provider(flatrate=[m.ProviderEntry(provider_name="Netflix")],
                             rent=[m.ProviderEntry(provider_name="Apple TV")], buy=[]),
        reviews=[],
    )
    card = m._movie_card(row)
    doc = m._html_template(card, m._sidebar_html())
    assert "&lt;script&gt;" in doc and "<script>" not in card, "XSS escaping"
    assert "Netflix" in card and "Apple TV" not in card, "flatrate-only on the card"
    assert 'data-providers="Netflix"' in card, "filter data attribute"
    assert "Top 3" not in card, "review count isn't hardcoded — --reviews can change it"
    assert doc.count('class="provider-filter"') == 1
    for p in m.TOP_PROVIDERS:
        assert p in doc, f"sidebar missing provider {p}"


# --- gemini -----------------------------------------------------------------

GEMINI_RESPONSE = json.dumps({
    "candidates": [{"content": {"parts": [{"text":
        "1. Idiocrazy (2006)\n"
        "2. Bulworth\n"
        "3. The Death of Stalin (2017) - Soviet satire\n"
        "4. In the Loop\n"
        "5. Some Hallucinated Garbage (2030)\n"
    }]}}]
}).encode()

FAKE_RESULTS = {
    "Idiocrazy": {"id": 1, "title": "Idiocrazy", "vote_count": 100},
    "Bulworth": {"id": 2, "title": "Bulworth", "vote_count": 100},
    "The Death of Stalin": {"id": 3, "title": "The Death of Stalin", "vote_count": 100},
    "In the Loop": {"id": 4, "title": "In the Loop", "vote_count": 100},
}


def test_gemini_recommendations_parses_and_resolves():
    """The parser is the fiddly part: numbered prefixes, "(YYYY)" suffixes, trailing
    prose after a dash, and titles TMDB can't resolve (dropped, not fatal)."""
    from urllib.parse import parse_qs

    def handler(url):
        if "generativelanguage" in url:
            return FakeResp(GEMINI_RESPONSE)
        hit = FAKE_RESULTS.get(parse_qs(url.split("?", 1)[1]).get("query", [""])[0])
        return FakeResp(json.dumps({"results": [hit]} if hit else {"results": []}).encode())

    prev = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "fakekey"
    try:
        with fake_urlopen(handler), quiet():
            result = m.gemini_recommendations("whatever", "fakekey", n=5)
    finally:
        if prev is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = prev
    titles = [x["title"] for x in result]
    assert titles == ["Idiocrazy", "Bulworth", "The Death of Stalin", "In the Loop"], titles


# --- keyword matching -------------------------------------------------------

def test_keywords_in_query():
    """Longest-n-gram-wins, single-word fallback, stopword rejection, empty catalog.

    The catalog deliberately contains the stopwords "the" and "of" — real TMDB
    keywords include plenty of function words, and the stopword guard is the only
    thing stopping them from being returned as a match.
    """
    cat = {"time travel": 4379, "time": 999, "dimension": 1234, "the": 111, "of": 222}
    assert m.keywords_in_query("time traveling, plot twists", cat) == [4379], \
        "longest phrase wins over the bare word"
    assert m.keywords_in_query("a dimension of the mind", cat) == [1234], "1-gram fallback"
    assert m.keywords_in_query("the a of", cat) == [], "stopword grams match nothing"
    assert m.keywords_in_query("a film of the year", cat) == [], \
        "a stopword mid-phrase must not match either"
    assert m.keywords_in_query("anything", {}) == [], "empty catalog is not fatal"


# --- shortlist / printing ---------------------------------------------------

def test_shortlist_tolerates_missing_and_null_votes():
    """Two different gaps: the key absent, and the key present but null. `.get(k, 0)`
    handles the first and returns None for the second, which then breaks the sort."""
    got = m.shortlist([{"title": "A", "vote_average": 8.0}, {"title": "B"},
                       {"title": "C", "vote_average": None}])
    assert [x["title"] for x in got] == ["A", "B", "C"]


def test_print_movies_renders_tv_rows():
    """TV results carry name/first_air_date, not title/release_date — the only place
    the two response shapes meet."""
    with quiet() as buf:
        m.print_movies([{"id": 1, "name": "Malcolm", "first_air_date": "2000-01-09",
                         "vote_average": 8.0}])
    assert "Malcolm (2000)" in buf.getvalue()


# --- bare runner ------------------------------------------------------------

if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
            print(f"ok    {_name}")
        except Exception:
            failures += 1
            print(f"FAIL  {_name}")
            import traceback
            traceback.print_exc()
    print(f"\n{failures} failed")
    sys.exit(1 if failures else 0)
