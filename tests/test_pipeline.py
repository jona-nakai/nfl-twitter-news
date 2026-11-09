from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nflnews.classifier import Classifier, Decision, build_input
from nflnews.config import Settings
from nflnews.db import Store
from nflnews.pipeline import DUPLICATE, ERROR, FILTERED, POSTED, SKIPPED, UPGRADED, Pipeline
from nflnews.x_api import Tweet

SETTINGS = replace(Settings.from_env(), story_window_hours=48, max_recent_stories=80)


def tweet(id: str, text: str, **kwargs) -> Tweet:
    return Tweet(
        id=id,
        author_id="1",
        username="AdamSchefter",
        author_name="Adam Schefter",
        avatar_url=None,
        text=text,
        created_at=datetime.now(UTC),
        **kwargs,
    )


def decision(**kwargs) -> Decision:
    base = dict(newsworthy=True, duplicate_of=None, categories=[], headline="h", reason="r")
    if kwargs.get("breaking_label"):
        base["label_text"] = "BREAKING"
    return Decision(**(base | kwargs))


class FakeClassifier:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.seen_stories = []

    async def classify(self, tweet, stories):
        self.seen_stories.append([s.id for s in stories])
        d = self.decisions.pop(0)
        if isinstance(d, Exception):
            raise d
        return d


class FakePublisher:
    def __init__(self):
        self.posts = []
        self.logged = []

    async def publish(self, post):
        self.posts.append(post)
        return {key: 1000 + i for i, key in enumerate(post.channels)}

    async def log_result(self, tweet, result):
        self.logged.append((tweet.id, result.outcome))


@pytest.fixture
async def store():
    s = await Store.open(":memory:")
    yield s
    await s.close()


async def test_first_report_posts_and_later_reports_are_duplicates(store):
    classifier = FakeClassifier(
        decision(
            breaking_label=True, categories=["transactions"], headline="Cowboys trade Parsons"
        ),
        decision(duplicate_of=1),
    )
    publisher = FakePublisher()
    pipeline = Pipeline(store, classifier, publisher, SETTINGS)

    first = await pipeline.process(tweet("100", "Cowboys trading Parsons to GB"))
    second = await pipeline.process(
        replace(tweet("101", "Parsons dealt to Packers"), username="RapSheet")
    )

    assert first.outcome == POSTED and first.channels == ("breaking", "transactions", "all-news")
    assert [p.channels for p in publisher.posts] == [["breaking", "transactions", "all-news"]]
    assert second.outcome == DUPLICATE
    assert classifier.seen_stories == [[], [1]]
    assert publisher.logged == [("100", POSTED), ("101", DUPLICATE)]
    assert await store.last_tweet_id() == "101"


async def test_skips_not_news_and_posts_league_news_to_all_news(store):
    pipeline = Pipeline(
        store,
        FakeClassifier(decision(newsworthy=False), decision(categories=[])),
        FakePublisher(),
        SETTINGS,
    )
    assert (await pipeline.process(tweet("1", "Listen to my podcast"))).outcome == SKIPPED
    league = await pipeline.process(tweet("2", "Owners approve Eagles stake sale"))
    assert league.outcome == POSTED and league.channels == ("all-news",)


async def test_filters_replies_to_others_retweets_and_repeats(store):
    publisher = FakePublisher()
    pipeline = Pipeline(
        store, FakeClassifier(decision(categories=["injuries"])), publisher, SETTINGS
    )
    assert (
        await pipeline.process(tweet("1", "@fan no", reply_to_user_id="999"))
    ).outcome == FILTERED
    assert (await pipeline.process(tweet("2", "RT", is_retweet=True))).outcome == FILTERED
    assert (
        await pipeline.process(tweet("3", "Thread detail", reply_to_user_id="1"))
    ).outcome == POSTED
    assert (
        await pipeline.process(tweet("3", "Thread detail", reply_to_user_id="1"))
    ).outcome == FILTERED
    assert publisher.logged == [("3", POSTED)]


async def test_classifier_failure_is_recorded_not_raised(store):
    publisher = FakePublisher()
    pipeline = Pipeline(store, FakeClassifier(RuntimeError("boom")), publisher, SETTINGS)
    result = await pipeline.process(tweet("5", "x"))
    assert result.outcome == ERROR and "boom" in result.error
    assert publisher.posts == []


async def test_simulated_posts_do_not_move_the_x_cursor(store):
    pipeline = Pipeline(
        store, FakeClassifier(decision(categories=["rumors"])), FakePublisher(), SETTINGS
    )
    await pipeline.process(tweet("sim-1", "test", simulated=True))
    assert await store.last_tweet_id() is None


def test_decision_channels_order_and_suppression():
    assert decision(breaking_label=True, categories=["rumors", "injuries"]).channels() == [
        "breaking",
        "injuries",
        "rumors",
        "all-news",
    ]
    assert decision(breaking_label=True, categories=[]).channels() == ["breaking", "all-news"]
    assert decision(newsworthy=False, breaking_label=True, categories=["injuries"]).channels() == []


def test_only_allowed_label_wording_makes_breaking():
    for text in ("BREAKING NEWS", "Bombshell:", "🚨🚨", "Wow.", "Huge news", "JUST IN"):
        assert decision(breaking_label=True, label_text=text).breaking, text
    # Cards the model has mistaken for breaking labels.
    for text in (None, "", "ALERT", "NEWS ALERT", "Developing News", "DEVELOPING", "Trade!"):
        assert not decision(breaking_label=True, label_text=text).breaking, text
    assert decision(duplicate_of=3, categories=["injuries"]).channels() == []
    # Breaking news comes only from a reporter's label, however big the story.
    assert decision(categories=["transactions"], headline="Cowboys trade Parsons").channels() == [
        "transactions",
        "all-news",
    ]
    # League news that fits no topic channel still goes to all-news.
    assert decision(categories=[]).channels() == ["all-news"]
    assert decision(categories=["rumors", "discipline"]).channels() == [
        "discipline",
        "rumors",
        "all-news",
    ]


def test_build_input_includes_stories_and_thread_context():
    from nflnews.db import Story

    stories = [
        Story(
            7,
            datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
            "Bears fire coach",
            ("breaking", "transactions"),
            "RapSheet",
        )
    ]
    t = tweet("9", "Contract details: 4 years", reply_to_user_id="1", parent_text="Jets sign X")
    text = build_input(t, stories)
    assert (
        "[7] Wed Oct 28 18:00 UTC · @RapSheet · breaking, transactions · Bears fire coach" in text
    )
    assert "reply in the author's own thread" in text and "Jets sign X" in text
    assert text.rstrip().endswith('"""\nContract details: 4 years\n"""')


class FakeOpenAI:
    """Answers responses.parse with queued decisions (or exceptions), keyed by model name."""

    def __init__(self, **by_model):
        self.by_model = {m: list(ds) for m, ds in by_model.items()}
        self.calls = []
        self.responses = self

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        model = kwargs["model"].replace("-", "_").replace(".", "_")
        answer = self.by_model[model].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(output_parsed=answer, status="completed")


def prompt_of(call) -> str:
    return call["input"][0]["content"][0]["text"]


def image_of(call) -> str | None:
    images = [c for c in call["input"][0]["content"] if c["type"] == "input_image"]
    return images[0]["image_url"] if images else None


async def test_classifier_drops_unknown_duplicate_ids_and_orders_categories():
    client = FakeOpenAI(gpt_6_luna=[decision(duplicate_of=42, categories=["rumors", "injuries"])])
    settings = replace(SETTINGS, openai_model="gpt-6-luna", web_search=True)
    result = await Classifier(settings, client).classify(tweet("1", "x"), [])
    assert result.duplicate_of is None
    assert result.categories == ["injuries", "rumors"]
    assert client.calls[0]["tools"][0]["type"] == "web_search"
    assert "## Research" in client.calls[0]["instructions"]
    assert client.calls[0]["text_format"] is Decision


async def test_no_research_instructions_without_web_search():
    client = FakeOpenAI(gpt_6_luna=[decision()])
    settings = replace(SETTINGS, openai_model="gpt-6-luna", web_search=False)
    await Classifier(settings, client).classify(tweet("1", "x"), [])
    assert "tools" not in client.calls[0]
    assert "## Research" not in client.calls[0]["instructions"]


class FakeClaude:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.calls = []
        self.messages = self

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(
            input_tokens=1000,
            cache_read_input_tokens=2000,
            cache_creation_input_tokens=0,
            output_tokens=200,
        )
        return SimpleNamespace(
            parsed_output=self.decisions.pop(0), stop_reason="end_turn", usage=usage
        )


async def test_claude_models_go_to_anthropic_with_cached_instructions():
    claude = FakeClaude(decision(categories=["injuries"]))
    usage = []
    settings = replace(
        SETTINGS,
        openai_model="claude-haiku-5-5",
        web_search=True,
        reasoning_effort="none",
    )
    classifier = Classifier(settings, claude_client=claude, on_usage=usage.append)
    result = await classifier.classify(tweet("1", "x"), [])
    assert result.channels() == ["injuries", "all-news"]
    call = claude.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_format"] is Decision
    assert call["output_config"] == {"effort": "low"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "tools" not in call and "## Research" not in call["system"][0]["text"]
    # 1000 uncached x $0.10 + 2000 cached x $0.01 + 200 output x $0.50, per 1M tokens
    assert usage[0].cost == pytest.approx(0.00022)


async def test_story_is_forgotten_when_nothing_could_be_posted(store):
    class BrokenPublisher(FakePublisher):
        async def publish(self, post):
            return {}

    classifier = FakeClassifier(
        decision(categories=["injuries"]), decision(categories=["injuries"])
    )
    pipeline = Pipeline(store, classifier, BrokenPublisher(), SETTINGS)
    assert (await pipeline.process(tweet("1", "x"))).outcome == ERROR
    await pipeline.process(tweet("2", "x again"))
    assert classifier.seen_stories == [[], []]


async def test_labeled_posts_are_breaking_news():
    client = FakeOpenAI(
        gpt_6_luna=[
            decision(breaking_label=True, categories=["transactions"], reason="Ringer graphic"),
            decision(breaking_label=True, duplicate_of=3, categories=["transactions"]),
        ]
    )
    settings = replace(SETTINGS, openai_model="gpt-6-luna", web_search=False)
    classifier = Classifier(settings, client)

    labeled = await classifier.classify(tweet("1", "Trade! Steelers send Porter to Dallas"), [])
    assert labeled.channels() == ["breaking", "transactions", "all-news"]

    story = SimpleNamespace(
        id=3,
        created_at=datetime.now(UTC),
        headline="h",
        channels=("transactions",),
        first_author="AdamSchefter",
    )
    repeat = await classifier.classify(tweet("2", "BREAKING: same trade"), [story])
    assert repeat.duplicate_of == 3 and repeat.channels() == []  # the pipeline decides on upgrades


async def test_image_sent_only_without_a_text_label_and_dropped_if_unreadable():
    client = FakeOpenAI(
        gpt_6_luna=[decision(), decision(), RuntimeError("image fetch failed"), decision()]
    )
    settings = replace(SETTINGS, openai_model="gpt-6-luna", web_search=False)
    classifier = Classifier(settings, client)
    img = "https://pbs.twimg.com/media/a.jpg"

    await classifier.classify(tweet("1", "Steelers trade Porter", image_url=img), [])
    assert image_of(client.calls[0]) == img
    assert client.calls[0]["input"][0]["content"][1]["detail"] == "low"
    assert "attached image is included" in prompt_of(client.calls[0])

    await classifier.classify(tweet("2", "🚨 Bombshell: Browns trade Garrett", image_url=img), [])
    assert image_of(client.calls[1]) is None

    await classifier.classify(tweet("3", "Steelers trade Porter", image_url=img), [])
    assert image_of(client.calls[2]) == img and image_of(client.calls[3]) is None
    assert "attached image" not in prompt_of(client.calls[3])


async def test_labeled_repeat_upgrades_an_unlabeled_story_once(store):
    classifier = FakeClassifier(
        decision(categories=["transactions"], headline="Broncos trade for Waddle"),
        decision(duplicate_of=1, breaking_label=True, categories=["transactions"], headline="Waddle"),
        decision(duplicate_of=1, breaking_label=True, categories=["transactions"]),
        decision(duplicate_of=1, categories=["transactions"]),
    )
    publisher = FakePublisher()
    pipeline = Pipeline(store, classifier, publisher, SETTINGS)

    first = await pipeline.process(tweet("1", "Broncos are trading for Waddle"))
    upgrade = await pipeline.process(tweet("2", "Splash: a blockbuster for Waddle"))
    again = await pipeline.process(tweet("3", "BREAKING: Waddle to Denver"))
    plain = await pipeline.process(tweet("4", "More on Waddle"))

    assert first.outcome == POSTED and first.channels == ("transactions", "all-news")
    assert upgrade.outcome == UPGRADED and upgrade.story_id == 1
    assert again.outcome == DUPLICATE and plain.outcome == DUPLICATE
    post = publisher.posts[1]
    assert post.channels == ["breaking"] and post.already_posted == ("transactions", "all-news")
    assert post.headline == "Waddle" and post.tweet.id == "2"
    assert (await store.get_story(1)).channels == ("breaking", "transactions", "all-news")
    assert len(publisher.posts) == 2
