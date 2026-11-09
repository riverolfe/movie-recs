"""HTML presentation helpers for movie_recs. No network or CLI dependencies."""
from __future__ import annotations

import html as html_lib
from typing import List, Optional

# US streaming providers shown in the HTML filter sidebar; matched case-insensitively
# as a substring against each movie's flatrate provider names.
TOP_PROVIDERS = [
    "Netflix", "Amazon Prime Video", "Disney Plus", "HBO Max", "Apple TV Plus",
    "Hulu", "Paramount Plus", "Peacock", "Starz", "Showtime",
]


def poster_img(movie: Movie, base_url: str) -> str:
    """Render a 300px-wide <img> for a movie's poster, or empty string if no poster."""
    path = movie.poster_path
    if not path:
        return ""
    # w300 + no CSS width: image displays at its natural 300px (TMDB medium size).
    return f'<img class="poster" src="{html_lib.escape(base_url)}w300{html_lib.escape(path)}" alt="">'


def _provider_badges(prov: Provider) -> str:
    # Streaming (flatrate) only — rent and buy are not surfaced.
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
    # data-providers drives the sidebar filter — pipe-separated TOP_PROVIDER names
    # that appear in this movie's flatrate list (case-insensitive substring).
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
        f'<div class="reviews"><h3>Reviews</h3>{_review_block(row.reviews)}</div>'
        f'<div class="providers"><strong>Streaming:</strong> {_provider_badges(row.providers)}</div>'
        f"</div></article>"
    )


def _html_template(body: str, sidebar: str = "") -> str:
    """Render the full HTML page with optional sidebar (provider filter)."""
    # CSS variables drive both themes; [data-theme="light"] on <html> overrides.
    # Vanilla JS for the theme toggle and provider filter — no build step, no external deps.
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Watch Recommendations</title>'
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
        # Theme toggle: fixed top-right, minimal text button.
        '.theme-toggle{position:fixed;top:.75rem;right:1rem;background:var(--bg-card);'
        'border:1px solid var(--border);color:var(--text);padding:.35rem .75rem;'
        'border-radius:4px;cursor:pointer;font-size:.8rem;font-family:inherit;z-index:10}'
        '.theme-toggle:hover{border-color:var(--accent)}'
        # Sidebar: sticky, scrolls with page; provider checkboxes.
        '.sidebar{position:sticky;top:1rem;align-self:start;'
        'background:var(--bg-card);border:1px solid var(--border);border-radius:6px;padding:1rem;'
        'font-size:.85rem;max-height:calc(100vh - 2rem);overflow-y:auto}'
        '.sidebar h2{margin:0 0 .75rem;font-size:.85rem;color:var(--text-muted);'
        'text-transform:uppercase;letter-spacing:.05em;font-weight:600}'
        '.provider-filter label{display:flex;align-items:center;gap:.4rem;padding:.25rem 0;cursor:pointer}'
        '.provider-filter input{accent-color:var(--accent)}'
        '.filter-info{font-size:.75rem;color:var(--text-muted);margin-top:.75rem;padding-top:.5rem;'
        'border-top:1px solid var(--border)}'
        # Mobile: sidebar collapses above main.
        '@media (max-width:780px){body{grid-template-columns:1fr}.sidebar{position:static}}'
        # Movie card: simple flat panel, no gradient or glow.
        '.movie{background:var(--bg-card);border:1px solid var(--border);border-radius:6px;'
        'padding:1.25rem;margin-bottom:1.25rem;display:flex;gap:1.25rem}'
        # Poster: fixed width, height auto (preserves 2:3 ratio).
        # align-self:flex-start prevents stretching when the card is taller than the image.
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
        # Theme is set before paint to avoid FOUC; persisted in localStorage.
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
        # Theme toggle button. Single-quote attribute delimiters so JS string literals
        # (which use double quotes) don't terminate the attribute value prematurely.
        '<button class="theme-toggle" onclick=\''
        'var h=document.documentElement,t=h.dataset.theme==="light"?"dark":"light";'
        'h.dataset.theme=t;localStorage.setItem("theme",t);this.textContent=t==="light"?"☀ Light":"🌙 Dark"'
        "'>🌙 Dark</button>"
        # Provider filter reads data-providers on each .movie article.
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
