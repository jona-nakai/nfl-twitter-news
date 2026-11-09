from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .classifier import Classifier, Decision
from .config import BREAKING, Settings
from .db import Store
from .x_api import Tweet

log = logging.getLogger(__name__)

POSTED = "posted"
DUPLICATE = "duplicate"
UPGRADED = "upgraded"  # a later, labeled report moved an earlier story into breaking news
SKIPPED = "skipped"
FILTERED = "filtered"
ERROR = "error"


@dataclass(frozen=True)
class StoryPost:
    story_id: int
    tweet: Tweet
    channels: list[str]
    breaking: bool
    headline: str
    # Channels the story already went to; their subscribers have been pinged.
    already_posted: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    outcome: str
    decision: Decision | None = None
    story_id: int | None = None
    channels: tuple[str, ...] = ()
    error: str | None = None


class Publisher(Protocol):
    async def publish(self, post: StoryPost) -> dict[str, int]:
        """Post the story to each channel; return channel key -> message id."""

    async def log_result(self, tweet: Tweet, result: Result) -> None: ...


class Pipeline:
    """Processes posts one at a time, oldest first, so the first reporter to break a story
    wins and everyone after them is recognized as a duplicate."""

    def __init__(
        self, store: Store, classifier: Classifier, publisher: Publisher, settings: Settings
    ):
        self._store = store
        self._classifier = classifier
        self._publisher = publisher
        self._settings = settings
        self._queue: asyncio.Queue[Tweet] = asyncio.Queue()
        self._lock = asyncio.Lock()

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    async def submit(self, tweet: Tweet) -> None:
        await self._queue.put(tweet)

    async def run(self) -> None:
        while True:
            tweet = await self._queue.get()
            try:
                await self.process(tweet)
            except Exception:
                log.exception("Failed to process post %s", tweet.id)

    async def preview(self, tweet: Tweet) -> Decision:
        """Classify without recording or posting anything."""
        return await self._classifier.classify(tweet, await self._recent_stories())

    async def process(self, tweet: Tweet) -> Result:
        async with self._lock:
            if await self._store.has_tweet(tweet.id):
                return Result(FILTERED)
            result = await self._decide_and_publish(tweet)
            await self._store.record_tweet(
                tweet_id=tweet.id,
                author=tweet.username,
                created_at=tweet.created_at,
                text=tweet.text,
                outcome=result.outcome,
                story_id=result.story_id,
                detail=result.error
                or (result.decision.model_dump_json() if result.decision else None),
            )
            if not tweet.simulated:
                await self._store.bump_last_tweet_id(tweet.id)
        # How long after the reporter posted it this post was handled (seconds-precise).
        delay = (datetime.now(UTC) - tweet.created_at).total_seconds()
        log.info(
            "@%s %s: %s -> %s %s (%.0fs after posting)",
            tweet.username,
            tweet.id,
            tweet.text[:80].replace("\n", " "),
            result.outcome,
            ",".join(result.channels),
            delay,
        )
        if result.outcome != FILTERED:
            try:
                await self._publisher.log_result(tweet, result)
            except Exception:
                log.exception("Failed to write admin log entry")
        return result

    async def _decide_and_publish(self, tweet: Tweet) -> Result:
        # Retweets are excluded by the X query already; replies to other people are chatter.
        # Replies in the author's own thread often carry follow-up details, so keep those.
        if tweet.is_retweet or tweet.is_reply_to_other:
            return Result(FILTERED)
        try:
            decision = await self._classifier.classify(tweet, await self._recent_stories())
        except Exception as e:
            log.exception("Classifier failed for post %s", tweet.id)
            return Result(ERROR, error=f"{type(e).__name__}: {e}")

        if decision.duplicate_of is not None:
            if decision.labeled and (upgraded := await self._upgrade(tweet, decision)):
                return upgraded
            return Result(DUPLICATE, decision, decision.duplicate_of)
        channels = decision.channels()
        if not channels:
            return Result(SKIPPED, decision)

        story_id = await self._store.add_story(
            created_at=tweet.created_at,
            headline=decision.headline,
            channels=tuple(channels),
            first_tweet_id=tweet.id,
            first_author=tweet.username,
        )
        post = StoryPost(story_id, tweet, channels, decision.breaking, decision.headline)
        try:
            messages = await self._publisher.publish(post)
        except Exception as e:
            log.exception("Publishing story %s failed", story_id)
            messages, error = {}, f"{type(e).__name__}: {e}"
        else:
            error = "couldn't post to any channel"
        if not messages:
            # Forget the story so a later report of the same news can still get posted.
            await self._store.delete_story(story_id)
            return Result(ERROR, decision, None, tuple(channels), error)
        for channel, message_id in messages.items():
            await self._store.add_post(story_id, channel, message_id)
        return Result(POSTED, decision, story_id, tuple(messages))

    async def _upgrade(self, tweet: Tweet, decision: Decision) -> Result | None:
        """A reporter labeled as breaking a story we posted without that label: post it to
        breaking news now, pinging only subscribers who weren't pinged for it yet."""
        story = await self._store.get_story(decision.duplicate_of)
        if story is None or BREAKING in story.channels:
            return None
        post = StoryPost(story.id, tweet, [BREAKING], True, story.headline, story.channels)
        try:
            messages = await self._publisher.publish(post)
        except Exception:
            log.exception("Upgrading story %s to breaking failed", story.id)
            return None
        if BREAKING not in messages:
            return None
        await self._store.add_story_channel(story.id, BREAKING)
        await self._store.add_post(story.id, BREAKING, messages[BREAKING])
        return Result(UPGRADED, decision, story.id, (BREAKING,))

    async def _recent_stories(self):
        return await self._store.recent_stories(
            hours=self._settings.story_window_hours, limit=self._settings.max_recent_stories
        )
