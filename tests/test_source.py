import asyncio
import time
from dataclasses import replace

import pytest

from nflnews.config import Settings
from nflnews.db import Store
from nflnews.x_api import Account, TweetSource, XAPIError

SETTINGS = replace(Settings.from_env(), x_accounts=("AdamSchefter",), poll_seconds=0.01)
SCHEFTER = Account("51263592", "AdamSchefter", "Adam Schefter", None)


def recent_id(offset: int = 0) -> str:
    """A post id minted now, so backfill windows treat it as recent."""
    return str(((int(time.time() * 1000) - 1288834974657) << 22) + offset)


class FakeX:
    def __init__(self, pages=(), stream_error=None):
        self.pages = list(pages)
        self.searches = []
        self.stream_error = stream_error
        self.lookups = 0

    async def lookup_users(self, usernames):
        self.lookups += 1
        return [SCHEFTER]

    async def search_recent(self, query, *, since_id=None, start_time=None):
        self.searches.append((since_id, start_time))
        return [self.pages.pop(0)] if self.pages else [{"meta": {"result_count": 0}}]

    async def sync_stream_rule(self, value):
        if self.stream_error:
            raise self.stream_error

    async def stream(self):
        raise AssertionError("unreachable in these tests")
        yield


@pytest.fixture
async def store():
    s = await Store.open(":memory:")
    yield s
    await s.close()


async def run_briefly(source, seconds=0.1):
    task = asyncio.create_task(source.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_poll_mode_emits_new_posts_and_advances_since_id(store):
    a, b = recent_id(1), recent_id(2)
    page = {
        "data": [
            {"id": b, "author_id": SCHEFTER.id, "text": "second"},
            {"id": a, "author_id": SCHEFTER.id, "text": "first"},
        ]
    }
    x = FakeX([page])
    seen = []

    async def on_tweet(t):
        seen.append(t.text)

    source = TweetSource(x, store, replace(SETTINGS, x_mode="poll"), on_tweet)
    await run_briefly(source)
    assert seen == ["first", "second"]
    assert x.searches[0][0] is None and x.searches[0][1] is not None  # first run: from now on
    assert x.searches[1] == (b, None)


async def test_stream_403_falls_back_to_polling(store):
    x = FakeX(stream_error=XAPIError(403, "client-not-enrolled"))

    async def on_tweet(t):
        pass

    source = TweetSource(x, store, replace(SETTINGS, x_mode="stream"), on_tweet)
    await run_briefly(source)
    assert source.mode == "poll" and x.searches


async def test_account_lookups_are_cached(store):
    async def on_tweet(t):
        pass

    for _ in range(2):
        source = TweetSource(FakeX(), store, replace(SETTINGS, x_mode="poll"), on_tweet)
        await run_briefly(source, 0.02)
    x = FakeX()
    source = TweetSource(x, store, replace(SETTINGS, x_mode="poll"), on_tweet)
    await run_briefly(source, 0.02)
    assert x.lookups == 0 and source.accounts == {SCHEFTER.id: SCHEFTER}


async def test_running_out_of_credits_is_retried_not_fatal(store, monkeypatch):
    class BrokeX(FakeX):
        async def lookup_users(self, usernames):
            raise XAPIError(402, '{"detail":"credits depleted"}')

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("nflnews.x_api.asyncio.sleep", fake_sleep)

    async def on_tweet(t):
        pass

    source = TweetSource(BrokeX(), store, replace(SETTINGS, x_mode="poll"), on_tweet)
    with pytest.raises(asyncio.CancelledError):
        await source.run()
    assert sleeps == [300, 300]
    assert "credits" in source.problem


async def test_start_from_catches_up_once_before_going_live(store):
    from datetime import UTC, datetime, timedelta

    start = datetime.now(UTC) - timedelta(hours=24)
    old = {"data": [{"id": recent_id(1), "author_id": SCHEFTER.id, "text": "Yesterday's news"}]}
    x = FakeX(pages=[old])
    seen = []

    async def on_tweet(tweet):
        seen.append(tweet.text)

    source = TweetSource(x, store, replace(SETTINGS, x_mode="poll"), on_tweet, start)
    await run_briefly(source)
    assert seen == ["Yesterday's news"]
    assert x.searches[0] == (None, start)
    assert all(s[1] != start for s in x.searches[1:])  # live polling after the catch-up
