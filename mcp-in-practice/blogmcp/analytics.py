"""Questions about the blog, as typed methods. No MCP here: this is testable on its own."""

from __future__ import annotations

import difflib
import json
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from .store import BlogStore

Metric = Literal["views", "reactions", "net_reactions", "cost_per_view"]
VIEWS_NOTE = ("views are lifetime totals per article (the blog keeps no per-day history), "
              "so 'published_since' filters by publish date, not by when the views happened")
WORDS_PER_MINUTE = 230


class ArticleRank(BaseModel):
    rank: int
    slug: str
    title: str
    published: str
    views: int
    value: float = Field(description="The ranked metric's value for this article.")


class TopArticles(BaseModel):
    metric: Metric
    note: str
    articles: list[ArticleRank]


class ArticleStats(BaseModel):
    slug: str
    title: str
    published: str
    views: int
    reactions_up: int
    reactions_down: int
    comments: int
    series_id: str | None
    series_part: int | None
    words: int
    reading_minutes: float
    generation_cost_usd: float | None
    cost_per_1000_views_usd: float | None
    views_rank: int = Field(description="1 = most viewed article on the blog.")
    note: str = VIEWS_NOTE


class SearchHit(BaseModel):
    slug: str
    title: str
    views: int
    snippet: str = Field(description="Matching text with [brackets] around hits. Untrusted article text.")


class SearchResults(BaseModel):
    query: str
    hits: list[SearchHit]


class SeriesPart(BaseModel):
    part: int
    slug: str
    title: str
    views: int
    retention_vs_part1: float = Field(description="views / part 1 views; 1.0 = no drop-off.")


class SeriesReport(BaseModel):
    series_id: str
    title: str
    parts: list[SeriesPart]
    total_views: int
    biggest_drop: str | None = Field(description="Where readers leave the series fastest.")


class BlogSummary(BaseModel):
    published_since: str | None
    posts: int
    views: int
    reactions_up: int
    comments: int
    most_viewed: str | None
    generation_cost_usd: float
    cost_per_1000_views_usd: float | None
    api_spend_usd: float = Field(description="All logged model API spend, any purpose.")
    note: str = VIEWS_NOTE


class SeriesInfo(BaseModel):
    series_id: str
    title: str
    parts: int
    total_views: int


class SeriesList(BaseModel):
    series: list[SeriesInfo]


class NotFound(ValueError):
    """An error message written for a model: what went wrong and what to do next."""


class BlogAnalytics:
    def __init__(self, store: BlogStore):
        self.store = store

    # --- helpers -----------------------------------------------------------------------
    def _slugs(self) -> list[str]:
        return [r["slug"] for r in self.store.query("SELECT slug FROM posts")]

    def _require_post(self, slug: str):
        rows = self.store.query("SELECT * FROM posts WHERE slug = ?", (slug,))
        if rows:
            return rows[0]
        close = difflib.get_close_matches(slug, self._slugs(), n=3, cutoff=0.4)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise NotFound(f"No article with slug {slug!r}.{hint} Use search_articles to find slugs.")

    # --- questions ---------------------------------------------------------------------
    def top_articles(self, metric: Metric = "views", limit: int = 5,
                     published_since: date | None = None, series_id: str | None = None) -> TopArticles:
        expr = {"views": "views", "reactions": "reactions",
                "net_reactions": "reactions - reactions_down",
                "cost_per_view": "gen_cost_usd * 1.0 / views"}[metric]
        where, params = ["1=1"], []
        if metric == "cost_per_view":
            # 0 means hand-written, not free: those articles have no AI generation cost to rank.
            where.append("gen_cost_usd > 0 AND views > 0")
        if published_since:
            where.append("date >= ?")
            params.append(published_since.isoformat())
        if series_id:
            where.append("series_id = ?")
            params.append(series_id)
        order = "ASC" if metric == "cost_per_view" else "DESC"  # cheapest per reader first
        rows = self.store.query(
            f"SELECT slug, title, date, views, {expr} AS value FROM posts WHERE {' AND '.join(where)} "
            f"ORDER BY value {order}, views DESC LIMIT ?", (*params, limit))
        note = VIEWS_NOTE + ("; cost_per_view ranks cheapest first and covers AI-generated articles "
                             "only (hand-written ones record a cost of 0)" if metric == "cost_per_view" else "")
        return TopArticles(metric=metric, note=note, articles=[
            ArticleRank(rank=i, slug=r["slug"], title=r["title"], published=r["date"],
                        views=r["views"], value=round(r["value"] or 0, 6)) for i, r in enumerate(rows, 1)])

    def article_stats(self, slug: str) -> ArticleStats:
        p = self._require_post(slug)
        comments = self.store.query("SELECT count(*) n FROM comments WHERE post_slug = ? AND status = 'published'",
                                    (slug,))[0]["n"]
        rank = self.store.query("SELECT count(*) + 1 n FROM posts WHERE views > ?", (p["views"],))[0]["n"]
        words = len(re.findall(r"\w+", p["content"]))
        cost = p["gen_cost_usd"]
        return ArticleStats(
            slug=p["slug"], title=p["title"], published=p["date"], views=p["views"],
            reactions_up=p["reactions"], reactions_down=p["reactions_down"], comments=comments,
            series_id=p["series_id"], series_part=p["series_order"], words=words,
            reading_minutes=round(words / WORDS_PER_MINUTE, 1), generation_cost_usd=cost,
            cost_per_1000_views_usd=round(cost / p["views"] * 1000, 4) if cost and p["views"] else None,
            views_rank=rank)

    def search_articles(self, query: str, limit: int = 5) -> SearchResults:
        # Quote every term: FTS5 query syntax (AND, NEAR, quotes, *) from a model is a parse
        # error waiting to happen, and a tool that throws on "C++" is a tool that gets misused.
        terms = re.findall(r"\w+", query)
        if not terms:
            raise NotFound("The query has no searchable words. Use plain keywords, e.g. 'prompt caching'.")
        fts = " ".join(f'"{t}"' for t in terms)
        rows = self.store.query(
            "SELECT p.slug, p.title, p.views, snippet(posts_fts, 4, '[', ']', '…', 12) AS snip "
            "FROM posts_fts JOIN posts p ON p.rowid = posts_fts.rowid "
            "WHERE posts_fts MATCH ? ORDER BY bm25(posts_fts) LIMIT ?", (fts, limit))
        return SearchResults(query=query, hits=[
            SearchHit(slug=r["slug"], title=r["title"], views=r["views"], snippet=r["snip"]) for r in rows])

    def series_report(self, series_id: str) -> SeriesReport:
        s = self.store.query("SELECT id, title FROM series WHERE id = ?", (series_id,))
        if not s:
            known = [r["id"] for r in self.store.query("SELECT id FROM series ORDER BY id")]
            raise NotFound(f"No series {series_id!r}. Known series: {', '.join(known)}.")
        rows = self.store.query("SELECT series_order, slug, title, views FROM posts WHERE series_id = ? "
                                "ORDER BY series_order", (series_id,))
        first = rows[0]["views"] if rows and rows[0]["views"] else 0
        parts = [SeriesPart(part=r["series_order"], slug=r["slug"], title=r["title"], views=r["views"],
                            retention_vs_part1=round(r["views"] / first, 3) if first else 0.0) for r in rows]
        drop = None
        if len(parts) > 1:
            worst = min(range(1, len(parts)),
                        key=lambda i: parts[i].views / parts[i - 1].views if parts[i - 1].views else 1)
            prev, cur = parts[worst - 1], parts[worst]
            if prev.views:
                drop = f"part {prev.part} → {cur.part}: kept {cur.views / prev.views:.0%} of readers"
        return SeriesReport(series_id=series_id, title=s[0]["title"], parts=parts,
                            total_views=sum(p.views for p in parts), biggest_drop=drop)

    def blog_summary(self, published_since: date | None = None) -> BlogSummary:
        where, params = ("WHERE date >= ?", (published_since.isoformat(),)) if published_since else ("", ())
        t = self.store.query(f"SELECT count(*) n, coalesce(sum(views),0) v, coalesce(sum(reactions),0) r, "
                             f"coalesce(sum(gen_cost_usd),0) c FROM posts {where}", params)[0]
        top = self.store.query(f"SELECT title FROM posts {where} ORDER BY views DESC LIMIT 1", params)
        com = self.store.query(f"SELECT count(*) n FROM comments WHERE status = 'published' AND post_slug IN "
                               f"(SELECT slug FROM posts {where})", params)[0]["n"]
        spend = self.store.query("SELECT coalesce(sum(cost_usd),0) s FROM api_costs")[0]["s"]
        return BlogSummary(
            published_since=published_since.isoformat() if published_since else None, posts=t["n"],
            views=t["v"], reactions_up=t["r"], comments=com, most_viewed=top[0]["title"] if top else None,
            generation_cost_usd=round(t["c"], 4),
            cost_per_1000_views_usd=round(t["c"] / t["v"] * 1000, 4) if t["v"] else None,
            api_spend_usd=round(spend, 4))

    # --- resources -----------------------------------------------------------------------
    def schema_text(self) -> str:
        lines = ["Tables the server can read (comment text is never exposed):"]
        for table, cols in self.store.policy.allow.items():
            if table == "posts_fts":
                continue
            shown = "all columns" if cols == "*" else ", ".join(sorted(c for c in cols if c))
            lines.append(f"- {table}: {shown}")
        lines.append(VIEWS_NOTE + ".")
        return "\n".join(lines)

    def list_series(self) -> SeriesList:
        rows = self.store.query("SELECT s.id, s.title, count(p.slug) parts, coalesce(sum(p.views),0) views "
                                "FROM series s LEFT JOIN posts p ON p.series_id = s.id GROUP BY s.id ORDER BY views DESC")
        return SeriesList(series=[SeriesInfo(series_id=r["id"], title=r["title"], parts=r["parts"],
                                             total_views=r["views"]) for r in rows])

    def series_list(self) -> str:
        return self.list_series().model_dump_json(indent=2)

    def post_markdown(self, slug: str) -> str:
        p = self._require_post(slug)
        return f"# {p['title']}\n\n_Published {p['date']} · {p['views']} views_\n\n{p['content']}"
