from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .pings import ordered

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    headline TEXT NOT NULL,
    channels TEXT NOT NULL,
    first_tweet_id TEXT NOT NULL,
    first_author TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS stories_created_at ON stories (created_at);
CREATE TABLE IF NOT EXISTS tweets (
    id TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    outcome TEXT NOT NULL,
    story_id INTEGER REFERENCES stories (id),
    detail TEXT,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tweets_processed_at ON tweets (processed_at);
CREATE TABLE IF NOT EXISTS llm_usage (
    at TEXT NOT NULL,
    model TEXT NOT NULL,
    cost REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_usage_at ON llm_usage (at);
CREATE TABLE IF NOT EXISTS posts (
    story_id INTEGER NOT NULL REFERENCES stories (id),
    channel TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (story_id, channel)
);
"""

LAST_TWEET_KEY = "x:last_tweet_id"


@dataclass(frozen=True)
class Story:
    id: int
    created_at: datetime
    headline: str
    channels: tuple[str, ...]
    first_author: str


class Store:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @classmethod
    async def open(cls, path: Path | str) -> Store:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    async def get(self, key: str) -> str | None:
        async with self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    async def get_json(self, key: str) -> Any:
        value = await self.get(key)
        return json.loads(value) if value is not None else None

    async def set_json(self, key: str, value: Any) -> None:
        await self.set(key, json.dumps(value))

    async def last_tweet_id(self) -> str | None:
        return await self.get(LAST_TWEET_KEY)

    async def bump_last_tweet_id(self, tweet_id: str) -> None:
        current = await self.last_tweet_id()
        if current is None or int(tweet_id) > int(current):
            await self.set(LAST_TWEET_KEY, tweet_id)

    async def has_tweet(self, tweet_id: str) -> bool:
        async with self._conn.execute("SELECT 1 FROM tweets WHERE id = ?", (tweet_id,)) as cur:
            return await cur.fetchone() is not None

    async def record_tweet(
        self,
        *,
        tweet_id: str,
        author: str,
        created_at: datetime,
        text: str,
        outcome: str,
        story_id: int | None = None,
        detail: str | None = None,
    ) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO tweets "
            "(id, author, created_at, text, outcome, story_id, detail, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tweet_id,
                author,
                created_at.isoformat(),
                text,
                outcome,
                story_id,
                detail,
                datetime.now(UTC).isoformat(),
            ),
        )
        await self._conn.commit()

    async def add_story(
        self,
        *,
        created_at: datetime,
        headline: str,
        channels: tuple[str, ...],
        first_tweet_id: str,
        first_author: str,
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO stories (created_at, headline, channels, first_tweet_id, first_author) "
            "VALUES (?, ?, ?, ?, ?)",
            (created_at.isoformat(), headline, ",".join(channels), first_tweet_id, first_author),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def delete_story(self, story_id: int) -> None:
        await self._conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))
        await self._conn.commit()

    async def get_story(self, story_id: int) -> Story | None:
        async with self._conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)) as cur:
            row = await cur.fetchone()
        return _story(row) if row else None

    async def recent_stories(self, *, hours: int, limit: int) -> list[Story]:
        """Stories first reported in the last `hours`, oldest first (newest `limit` kept)."""
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        async with self._conn.execute(
            "SELECT * FROM stories WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
            (since, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_story(r) for r in reversed(rows)]

    async def add_story_channel(self, story_id: int, channel: str) -> None:
        story = await self.get_story(story_id)
        if story is None or channel in story.channels:
            return
        await self._conn.execute(
            "UPDATE stories SET channels = ? WHERE id = ?",
            (",".join(ordered((*story.channels, channel))), story_id),
        )
        await self._conn.commit()

    async def add_post(self, story_id: int, channel: str, message_id: int) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO posts (story_id, channel, message_id) VALUES (?, ?, ?)",
            (story_id, channel, message_id),
        )
        await self._conn.commit()

    async def add_llm_cost(self, model: str, cost: float) -> None:
        await self._conn.execute(
            "INSERT INTO llm_usage (at, model, cost) VALUES (?, ?, ?)",
            (datetime.now(UTC).isoformat(), model, cost),
        )
        await self._conn.commit()

    async def llm_spend_since(self, since: datetime) -> float:
        async with self._conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS total FROM llm_usage WHERE at >= ?",
            (since.isoformat(),),
        ) as cur:
            row = await cur.fetchone()
        return row["total"]

    async def outcome_counts(self, *, hours: int) -> dict[str, int]:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        async with self._conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM tweets WHERE processed_at >= ? GROUP BY outcome",
            (since,),
        ) as cur:
            rows = await cur.fetchall()
        return {r["outcome"]: r["n"] for r in rows}


def _story(row: aiosqlite.Row) -> Story:
    return Story(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        headline=row["headline"],
        channels=tuple(c for c in row["channels"].split(",") if c),
        first_author=row["first_author"],
    )
