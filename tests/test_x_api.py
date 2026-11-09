from datetime import UTC, datetime

import httpx

from nflnews.x_api import Account, XClient, build_query, parse_tweets, snowflake_time

SCHEFTER = Account("51263592", "AdamSchefter", "Adam Schefter", "https://img/schefter.jpg")
ACCOUNTS = {SCHEFTER.id: SCHEFTER}


def test_build_query():
    assert (
        build_query(["AdamSchefter", "RapSheet"])
        == "(from:AdamSchefter OR from:RapSheet) -is:retweet"
    )


def test_snowflake_time():
    # Snowflake ids carry their creation time (ms since X's 2010 epoch) in the upper bits.
    assert snowflake_time("1212092628029698048").replace(microsecond=0) == datetime(
        2019, 12, 31, 19, 26, 16, tzinfo=UTC
    )


def test_parse_stream_event_with_long_text_links_and_quote():
    event = {
        "data": {
            "id": "1850000000000000002",
            "author_id": SCHEFTER.id,
            "created_at": "2026-10-28T19:01:02.000Z",
            "text": "short version https://t.co/abc",
            "note_tweet": {
                "text": "Trade: Cowboys &amp; Packers https://t.co/story https://t.co/pic https://t.co/q",
                "entities": {
                    "urls": [
                        {
                            "url": "https://t.co/story",
                            "expanded_url": "https://espn.com/story",
                            "display_url": "espn.com/story",
                        },
                        {
                            "url": "https://t.co/pic",
                            "expanded_url": "https://x.com/i/1",
                            "display_url": "pic.x.com/xyz",
                            "media_key": "3_1",
                        },
                        {
                            "url": "https://t.co/q",
                            "expanded_url": "https://x.com/AdamSchefter/status/1849999999999999999",
                            "display_url": "x.com/AdamSchefter/…",
                        },
                    ]
                },
            },
            "referenced_tweets": [{"type": "quoted", "id": "1849999999999999999"}],
        },
        "includes": {
            "tweets": [{"id": "1849999999999999999", "text": "Earlier: talks heating up"}]
        },
        "matching_rules": [{"id": "1", "tag": "nflnews"}],
    }
    [tweet] = parse_tweets(event, ACCOUNTS)
    assert tweet.text == "Trade: Cowboys & Packers https://espn.com/story"
    assert tweet.quoted_text == "Earlier: talks heating up"
    assert tweet.username == "AdamSchefter"
    assert tweet.created_at == datetime(2026, 10, 28, 19, 1, 2, tzinfo=UTC)
    assert tweet.url == "https://x.com/AdamSchefter/status/1850000000000000002"
    assert not tweet.is_retweet and not tweet.is_reply_to_other


def test_parse_search_page_sorts_and_skips_unknown_authors():
    page = {
        "data": [
            {
                "id": "300",
                "author_id": SCHEFTER.id,
                "text": "newer",
                "in_reply_to_user_id": SCHEFTER.id,
                "referenced_tweets": [{"type": "replied_to", "id": "200"}],
            },
            {"id": "250", "author_id": "999", "text": "someone else"},
            {
                "id": "200",
                "author_id": SCHEFTER.id,
                "text": "older",
                "in_reply_to_user_id": "12345",
            },
        ],
        "includes": {"tweets": [{"id": "200", "text": "older"}]},
    }
    tweets = parse_tweets(page, ACCOUNTS)
    assert [t.id for t in tweets] == ["200", "300"]
    assert tweets[0].is_reply_to_other
    assert tweets[1].is_self_reply and tweets[1].parent_text == "older"


def test_parse_empty_payloads():
    assert parse_tweets({}, ACCOUNTS) == []
    assert parse_tweets({"meta": {"result_count": 0}}, ACCOUNTS) == []


async def test_search_recent_follows_pagination():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if "next_token" not in request.url.params:
            return httpx.Response(200, json={"data": [{"id": "2"}], "meta": {"next_token": "abc"}})
        return httpx.Response(200, json={"data": [{"id": "1"}], "meta": {}})

    http = httpx.AsyncClient(base_url="https://api.x.com/2", transport=httpx.MockTransport(handler))
    pages = await XClient("token", http).search_recent("from:x", since_id="1")
    assert [p["data"][0]["id"] for p in pages] == ["2", "1"]
    assert calls[0]["since_id"] == "1" and calls[1]["next_token"] == "abc"


async def test_sync_stream_rule_replaces_stale_rule():
    posts = []
    rules = [
        {"id": "10", "value": "(from:Old) -is:retweet", "tag": "nflnews"},
        {"id": "11", "value": "unrelated", "tag": "other"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": rules})
        posts.append(request.read().decode())
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(base_url="https://api.x.com/2", transport=httpx.MockTransport(handler))
    await XClient("token", http).sync_stream_rule("(from:New) -is:retweet")
    assert '"add"' in posts[0] and "from:New" in posts[0]
    assert '"delete"' in posts[1] and '"10"' in posts[1] and '"11"' not in posts[1]


def test_parse_picks_up_the_first_image_or_video_thumbnail():
    page = {
        "data": [
            {
                "id": "11",
                "author_id": SCHEFTER.id,
                "text": "Trade",
                "attachments": {"media_keys": ["3_1", "3_2"]},
            },
            {
                "id": "12",
                "author_id": SCHEFTER.id,
                "text": "Clip",
                "attachments": {"media_keys": ["7_1"]},
            },
            {"id": "13", "author_id": SCHEFTER.id, "text": "Plain"},
        ],
        "includes": {
            "media": [
                {"media_key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/media/a.jpg"},
                {"media_key": "3_2", "type": "photo", "url": "https://pbs.twimg.com/media/b.jpg"},
                {
                    "media_key": "7_1",
                    "type": "video",
                    "preview_image_url": "https://pbs.twimg.com/t.jpg",
                },
            ]
        },
    }
    photo, video, plain = parse_tweets(page, ACCOUNTS)
    assert photo.image_url == "https://pbs.twimg.com/media/a.jpg"
    assert video.image_url == "https://pbs.twimg.com/t.jpg"
    assert plain.image_url is None
