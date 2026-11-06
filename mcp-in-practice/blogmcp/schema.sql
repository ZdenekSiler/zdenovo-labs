-- Copied verbatim from the blog's own database (backend/data/blog.db), 2026-11-06.

CREATE TABLE series (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            description TEXT,
            created_at  TEXT NOT NULL
        , outline TEXT);

CREATE TABLE posts (
            slug    TEXT PRIMARY KEY,
            title   TEXT NOT NULL,
            date    TEXT NOT NULL,
            summary TEXT NOT NULL,
            tags    TEXT NOT NULL,
            content TEXT NOT NULL,
            image   TEXT
        , sources TEXT NOT NULL DEFAULT '[]', views INTEGER NOT NULL DEFAULT 0, ai_comments INTEGER NOT NULL DEFAULT 1, reactions INTEGER NOT NULL DEFAULT 0, series_id TEXT REFERENCES series(id), series_order INTEGER, reactions_down INTEGER NOT NULL DEFAULT 0, related_posts TEXT NOT NULL DEFAULT '[]', category TEXT, gen_cost_usd REAL);

CREATE TABLE comments (
            id         TEXT PRIMARY KEY,
            post_slug  TEXT NOT NULL,
            author     TEXT NOT NULL,
            body       TEXT NOT NULL,
            created_at TEXT NOT NULL
        , is_generated INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'published', parent_id TEXT REFERENCES comments(id));

CREATE TABLE api_costs (
            id            TEXT PRIMARY KEY,
            created_at    TEXT NOT NULL,
            step          TEXT NOT NULL,
            model         TEXT NOT NULL,
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read    INTEGER NOT NULL DEFAULT 0,
            cache_write   INTEGER NOT NULL DEFAULT 0,
            web_searches  INTEGER NOT NULL DEFAULT 0,
            cost_usd      REAL NOT NULL DEFAULT 0,
            ref_kind      TEXT,
            ref_id        TEXT
        );

CREATE VIRTUAL TABLE posts_fts USING fts5(
            slug UNINDEXED,
            title,
            summary,
            tags,
            content,
            content='posts',
            content_rowid='rowid'
        );

CREATE TRIGGER posts_fts_insert
        AFTER INSERT ON posts BEGIN
            INSERT INTO posts_fts(rowid, slug, title, summary, tags, content)
            VALUES (new.rowid, new.slug, new.title, new.summary, new.tags, new.content);
        END;

CREATE TRIGGER posts_fts_update
        AFTER UPDATE ON posts BEGIN
            INSERT INTO posts_fts(posts_fts, rowid, slug, title, summary, tags, content)
            VALUES ('delete', old.rowid, old.slug, old.title, old.summary, old.tags, old.content);
            INSERT INTO posts_fts(rowid, slug, title, summary, tags, content)
            VALUES (new.rowid, new.slug, new.title, new.summary, new.tags, new.content);
        END;

CREATE TRIGGER posts_fts_delete
        AFTER DELETE ON posts BEGIN
            INSERT INTO posts_fts(posts_fts, rowid, slug, title, summary, tags, content)
            VALUES ('delete', old.rowid, old.slug, old.title, old.summary, old.tags, old.content);
        END;
