"""X (Twitter) API v2: account lookup, filtered stream, recent search, and the tweet source
that feeds new posts from the tracked accounts into the pipeline."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .config import FatalError, Settings
from .db import Store

log = logging.getLogger(__name__)

API_BASE = "https://api.x.com/2"
TWEET_FIELDS = (
    "id,text,author_id,created_at,referenced_tweets,in_reply_to_user_id,note_tweet,entities,"
    "attachments"
)
# Pulls in quoted posts and thread parents so the classifier has context, and attached media so
# it can spot "BREAKING NEWS" graphics. Deliberately no author expansion: user objects are
# billed separately, and we already know our accounts.
EXPANSIONS = "referenced_tweets.id,attachments.media_keys"
MEDIA_FIELDS = "type,url,preview_image_url"
RULE_TAG = "nflnews"
TWITTER_EPOCH_MS = 1288834974657


@dataclass(frozen=True)
class Account:
    id: str
    username: str
    name: str
    avatar_url: str | None


@dataclass(frozen=True)
class Tweet:
    id: str
    author_id: str
    username: str
    author_name: str
    avatar_url: str | None
    text: str
    created_at: datetime
    is_retweet: bool = False
    reply_to_user_id: str | None = None
    quoted_text: str | None = None
    parent_text: str | None = None
    image_url: str | None = None  # first photo, or a video's thumbnail
    simulated: bool = False

    @property
    def url(self) -> str:
        if self.simulated:
            return f"https://x.com/{self.username}"
        return f"https://x.com/{self.username}/status/{self.id}"

    @property
    def is_self_reply(self) -> bool:
        return self.reply_to_user_id == self.author_id

    @property
    def is_reply_to_other(self) -> bool:
        return self.reply_to_user_id is not None and self.reply_to_user_id != self.author_id


class XAPIError(Exception):
    def __init__(self, status: int, body: str, retry_after: float | None = None):
        super().__init__(f"X API returned HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body
        self.retry_after = retry_after


class StreamUnavailable(Exception):
    """Filtered stream can't be used with these credentials; fall back to polling."""


def snowflake_time(tweet_id: str) -> datetime:
    return datetime.fromtimestamp(((int(tweet_id) >> 22) + TWITTER_EPOCH_MS) / 1000, tz=UTC)


def build_query(usernames: Iterable[str]) -> str:
    froms = " OR ".join(f"from:{u}" for u in usernames)
    return f"({froms}) -is:retweet"


def parse_tweets(payload: Mapping, accounts: Mapping[str, Account]) -> list[Tweet]:
    """Parse a stream event or search page into Tweets from tracked accounts, oldest first."""
    data = payload.get("data")
    if not data:
        return []
    items = data if isinstance(data, list) else [data]
    included = {t["id"]: t for t in payload.get("includes", {}).get("tweets", [])}
    images = {
        m["media_key"]: m.get("url") or m.get("preview_image_url")
        for m in payload.get("includes", {}).get("media", [])
    }

    tweets = []
    for raw in items:
        account = accounts.get(raw.get("author_id", ""))
        if account is None:
            continue
        refs = {r["type"]: r["id"] for r in raw.get("referenced_tweets", [])}
        quoted = included.get(refs.get("quoted", ""))
        parent = included.get(refs.get("replied_to", ""))
        created = raw.get("created_at")
        media_keys = (raw.get("attachments") or {}).get("media_keys", [])
        tweets.append(
            Tweet(
                id=raw["id"],
                author_id=account.id,
                username=account.username,
                author_name=account.name,
                avatar_url=account.avatar_url,
                text=tweet_text(raw, drop_status_ids={refs.get("quoted")}),
                created_at=(
                    datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if created
                    else snowflake_time(raw["id"])
                ),
                is_retweet="retweeted" in refs,
                reply_to_user_id=raw.get("in_reply_to_user_id"),
                quoted_text=tweet_text(quoted) if quoted else None,
                parent_text=tweet_text(parent) if parent else None,
                image_url=next((images[k] for k in media_keys if images.get(k)), None),
            )
        )
    tweets.sort(key=lambda t: int(t.id))
    return tweets


def tweet_text(raw: Mapping, drop_status_ids: Iterable[str | None] = ()) -> str:
    """Full post text (long posts live in note_tweet) with t.co links expanded, media links
    and links to the quoted post removed, and HTML entities unescaped."""
    note = raw.get("note_tweet") or {}
    if note.get("text"):
        text, entities = note["text"], note.get("entities") or {}
    else:
        text, entities = raw.get("text", ""), raw.get("entities") or {}
    drop = {f"/status/{i}" for i in drop_status_ids if i}
    for url in entities.get("urls", []):
        short = url.get("url")
        if not short:
            continue
        expanded = url.get("expanded_url") or ""
        display = url.get("display_url") or ""
        is_media = bool(url.get("media_key")) or display.startswith(
            ("pic.x.com", "pic.twitter.com")
        )
        if is_media or any(expanded.rstrip("/").endswith(d) for d in drop):
            replacement = ""
        else:
            replacement = expanded or display or short
        text = text.replace(short, replacement)
    return html.unescape(text).strip()


class XClient:
    def __init__(self, bearer_token: str, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {bearer_token}", "User-Agent": "nflnews-bot"},
            timeout=httpx.Timeout(20.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = await self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise _error(resp, resp.text)
        return resp.json()

    async def lookup_users(self, usernames: Iterable[str]) -> list[Account]:
        payload = await self._request(
            "GET",
            "/users/by",
            params={"usernames": ",".join(usernames), "user.fields": "name,profile_image_url"},
        )
        for err in payload.get("errors", []):
            log.warning("X user lookup problem: %s", err.get("detail") or err)
        return [
            Account(
                id=u["id"],
                username=u["username"],
                name=u.get("name") or u["username"],
                avatar_url=u.get("profile_image_url"),
            )
            for u in payload.get("data", [])
        ]

    async def search_recent(
        self, query: str, *, since_id: str | None = None, start_time: datetime | None = None
    ) -> list[dict]:
        """All pages of results newer than since_id / start_time."""
        params = {
            "query": query,
            "max_results": 100,
            "tweet.fields": TWEET_FIELDS,
            "expansions": EXPANSIONS,
            "media.fields": MEDIA_FIELDS,
        }
        if since_id:
            params["since_id"] = since_id
        elif start_time:
            params["start_time"] = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        pages = []
        while True:
            page = await self._request("GET", "/tweets/search/recent", params=params)
            pages.append(page)
            next_token = page.get("meta", {}).get("next_token")
            if not next_token:
                return pages
            params["next_token"] = next_token

    async def credits(self) -> dict:
        """Pay-per-use balance in USD: total_balance, free_balance, free_grants,
        prepaid_balance. Free to call."""
        return (await self._request("GET", "/usage/credits")).get("data", {})

    async def post_usage(self) -> dict:
        """Posts read this billing cycle: project_usage, project_cap, cap_reset_day."""
        return (await self._request("GET", "/usage/tweets")).get("data", {})

    async def sync_stream_rule(self, value: str) -> None:
        """Make the stream rule tagged RULE_TAG match `value`, leaving other rules alone."""
        rules = (await self._request("GET", "/tweets/search/stream/rules")).get("data", [])
        if any(r["value"] == value for r in rules):
            stale = [r["id"] for r in rules if r.get("tag") == RULE_TAG and r["value"] != value]
        else:
            stale = [r["id"] for r in rules if r.get("tag") == RULE_TAG]
            await self._request(
                "POST",
                "/tweets/search/stream/rules",
                json={"add": [{"value": value, "tag": RULE_TAG}]},
            )
            log.info("Added X stream rule: %s", value)
        if stale:
            await self._request(
                "POST", "/tweets/search/stream/rules", json={"delete": {"ids": stale}}
            )

    async def stream(self) -> AsyncIterator[dict]:
        """Yield filtered-stream events. X sends a keep-alive every ~20s, so a 30s read
        timeout means the connection has stalled."""
        timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        params = {
            "tweet.fields": TWEET_FIELDS,
            "expansions": EXPANSIONS,
            "media.fields": MEDIA_FIELDS,
        }
        async with self._http.stream(
            "GET", "/tweets/search/stream", params=params, timeout=timeout
        ) as resp:
            if resp.status_code >= 400:
                raise _error(resp, (await resp.aread()).decode(errors="replace"))
            log.info("Connected to X filtered stream")
            async for line in resp.aiter_lines():
                if line.strip():
                    yield json.loads(line)


def _error(resp: httpx.Response, body: str) -> XAPIError:
    retry_after = None
    reset = resp.headers.get("x-rate-limit-reset")
    if resp.status_code == 429 and reset and reset.isdigit():
        retry_after = max(1.0, int(reset) - time.time())
    return XAPIError(resp.status_code, body, retry_after)


class TweetSource:
    """Feeds new posts from the tracked accounts to `on_tweet`, oldest first.

    Stream mode holds a filtered-stream connection and, after every (re)connect, backfills
    anything missed via recent search. Poll mode just runs recent search on an interval.
    Either way the X bill is per post returned, so idle time is free.
    """

    def __init__(
        self,
        client: XClient,
        store: Store,
        settings: Settings,
        on_tweet: Callable[[Tweet], Awaitable[None]],
        start_from: datetime | None = None,
    ):
        self._client = client
        self._store = store
        self._settings = settings
        self._on_tweet = on_tweet
        # One-time catch-up from this moment on startup (e.g. "the last 24 hours").
        self._start_from = start_from
        self.accounts: dict[str, Account] = {}
        self.mode = settings.x_mode
        self.connected = False
        self.problem: str | None = None

    async def run(self) -> None:
        """Run forever. Only a bad token is fatal: anything else (including running out of
        credits) is retried, so the Discord side keeps working and the feed resumes by itself."""
        attempt = 0
        while True:
            try:
                await self._run()
            except XAPIError as e:
                if e.status == 401:
                    raise FatalError(
                        "X rejected X_BEARER_TOKEN (HTTP 401). Check the token."
                    ) from e
                self._report(e)
                delay = 300 if e.status == 402 else _backoff(attempt, base=30, cap=600)
            except httpx.TransportError as e:
                self.problem = f"can't reach X ({e!r})"
                delay = _backoff(attempt, base=30, cap=600)
            self.connected = False
            log.warning("X feed stopped: %s. Retrying in %.0fs", self.problem, delay)
            attempt += 1
            await asyncio.sleep(delay)

    def _report(self, error: XAPIError) -> None:
        if error.status == 402:
            self.problem = "out of X API credits; add credits at https://console.x.com"
        else:
            self.problem = str(error)

    def _ok(self) -> None:
        self.connected = True
        self.problem = None

    async def _run(self) -> None:
        self.accounts = {a.id: a for a in await self._resolve_accounts()}
        if not self.accounts:
            raise FatalError("None of the X_ACCOUNTS could be found on X")
        query = build_query(a.username for a in self.accounts.values())
        if self._start_from:
            pages = await self._client.search_recent(query, start_time=self._start_from)
            merged = _merge_pages(pages)
            log.info("Catching up on %d posts since %s", len(merged["data"]), self._start_from)
            await self._emit(merged)
            self._start_from = None
        if self.mode == "stream":
            try:
                await self._run_stream(query)
            except StreamUnavailable as e:
                log.warning("Filtered stream unavailable (%s); falling back to polling", e)
                self.mode = "poll"
        await self._run_poll(query)

    async def _resolve_accounts(self) -> list[Account]:
        """Username -> account lookups are cached forever; they cost money and never change."""
        accounts, missing = [], []
        for username in self._settings.x_accounts:
            cached = await self._store.get_json(f"x:user:{username.lower()}")
            if cached:
                accounts.append(Account(**cached))
            else:
                missing.append(username)
        if missing:
            for account in await self._client.lookup_users(missing):
                await self._store.set_json(f"x:user:{account.username.lower()}", account.__dict__)
                accounts.append(account)
        log.info("Tracking X accounts: %s", ", ".join("@" + a.username for a in accounts))
        return accounts

    async def _emit(self, payload: Mapping) -> str | None:
        """Hand off tracked accounts' posts; return the newest post id in the payload."""
        for tweet in parse_tweets(payload, self.accounts):
            await self._on_tweet(tweet)
        data = payload.get("data") or []
        ids = [int(t["id"]) for t in (data if isinstance(data, list) else [data])]
        return str(max(ids)) if ids else None

    async def _backfill(self, query: str) -> None:
        """Catch up on posts made while we were disconnected (bounded by backfill_minutes so a
        long outage doesn't dump stale news). Nothing to catch up on the very first run."""
        last_id = await self._store.last_tweet_id()
        if last_id is None or self._settings.backfill_minutes <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(minutes=self._settings.backfill_minutes)
        if snowflake_time(last_id) >= cutoff:
            pages = await self._client.search_recent(query, since_id=last_id)
        else:
            pages = await self._client.search_recent(query, start_time=cutoff)
        merged = _merge_pages(pages)
        if merged["data"]:
            log.info("Backfilling %d posts", len(merged["data"]))
            await self._emit(merged)

    async def _run_stream(self, query: str) -> None:
        try:
            await self._client.sync_stream_rule(query)
        except XAPIError as e:
            if e.status == 403:
                raise StreamUnavailable(e) from e
            raise
        attempt = 0
        while True:
            try:
                await self._backfill(query)
                async for event in self._client.stream():
                    self._ok()
                    attempt = 0
                    if "data" in event:
                        await self._emit(event)
                    elif "errors" in event:
                        log.warning("X stream message: %s", event["errors"])
                delay = _backoff(attempt, base=1, cap=16)
            except XAPIError as e:
                if e.status in (401, 403):
                    if e.status == 401:
                        raise
                    raise StreamUnavailable(e) from e
                self._report(e)
                if e.status == 429:
                    delay = e.retry_after or _backoff(attempt, base=60, cap=900)
                else:
                    delay = _backoff(attempt, base=5, cap=320)
                log.warning("X stream error: %s; reconnecting in %.0fs", e, delay)
            except (httpx.TransportError, json.JSONDecodeError) as e:
                delay = _backoff(attempt, base=1, cap=16)
                log.warning("X stream dropped (%r); reconnecting in %.0fs", e, delay)
            self.connected = False
            attempt += 1
            await asyncio.sleep(delay)

    async def _run_poll(self, query: str) -> None:
        # X requires start_time to be at least 10s in the past.
        now, margin = datetime.now(UTC), timedelta(seconds=15)
        cutoff = now - max(timedelta(minutes=self._settings.backfill_minutes), margin)
        since_id, start_time = None, now - margin  # first run: only posts from now on
        last_id = await self._store.last_tweet_id()
        if last_id and snowflake_time(last_id) >= cutoff:
            since_id = last_id
        elif last_id:
            start_time = cutoff  # long outage: catch up on the backfill window only
        log.info("Polling X recent search every %.0fs", self._settings.poll_seconds)
        attempt = 0
        while True:
            try:
                pages = await self._client.search_recent(
                    query, since_id=since_id, start_time=start_time
                )
                self._ok()
                attempt = 0
                newest = await self._emit(_merge_pages(pages))
                if newest:
                    since_id, start_time = newest, None
                delay = self._settings.poll_seconds
            except XAPIError as e:
                if e.status == 401:
                    raise
                self.connected = False
                self._report(e)
                delay = e.retry_after or _backoff(
                    attempt, base=self._settings.poll_seconds, cap=300
                )
                attempt += 1
                log.warning("X search error: %s; retrying in %.0fs", e, delay)
            except httpx.TransportError as e:
                self.connected = False
                delay = _backoff(attempt, base=self._settings.poll_seconds, cap=120)
                attempt += 1
                log.warning("X search failed (%r); retrying in %.0fs", e, delay)
            await asyncio.sleep(delay)


def _merge_pages(pages: list[dict]) -> dict:
    data, included, media = [], [], []
    for page in pages:
        data.extend(page.get("data", []))
        included.extend(page.get("includes", {}).get("tweets", []))
        media.extend(page.get("includes", {}).get("media", []))
    return {"data": data, "includes": {"tweets": included, "media": media}}


def _backoff(attempt: int, *, base: float, cap: float) -> float:
    return min(cap, base * 2**attempt)
