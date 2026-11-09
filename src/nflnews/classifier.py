from __future__ import annotations

import inspect
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from .config import ALL_NEWS, BREAKING, Settings
from .db import Story
from .pings import ordered
from .x_api import Tweet

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).with_name("prompt.md")
RESEARCH_NOTE = """## Research

You have a web search tool. Your knowledge of rosters and contracts may be out of date, so when a decision hinges on who a player is now (current team, position, contract status), search for it. Don't search when the answer is obvious: clearly trivial items and clearly huge items don't need research. Keep searches quick; one or two is usually enough.

Search is for context, not fact-checking. The reporters are trusted, and news this fresh often isn't online yet, so search results may be older than the post or seem to contradict it. Never drop, doubt, or downgrade a post because of that."""

# USD per 1M tokens: (input, cached input, output). Claude cache writes cost 1.25x input.
PRICES = {
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
}
WEB_SEARCH_PRICE = 0.01  # OpenAI, per search call

# Unambiguous words reporters use to label big news. Only decides whether the image is worth
# sending; the model judges whether the post really is labeled breaking, and accepts more wording.
LABEL_WORDS = re.compile(
    r"\b(breaking|bombshell|blockbuster|splash|stunner|shocker)\b|🚨", re.IGNORECASE
)


# Wording that can make a post breaking. The model quotes the label it saw (`label_text`) and
# code checks it here, because the model sometimes counts cards like "ALERT" or "DEVELOPING".
ALLOWED_LABEL = re.compile(
    r"\b(breaking|bombshell|blockbuster|splash|stunner|shocker|huge|massive|big news|wow|whoa"
    r"|just in)\b|🚨|‼️|💣|🤯",
    re.IGNORECASE,
)


class Decision(BaseModel):
    newsworthy: bool = Field(description="False for anything that isn't NFL news worth posting.")
    duplicate_of: int | None = Field(
        description="ID of an already-posted story this post repeats, or null."
    )
    label_text: str | None = Field(
        default=None,
        description="The exact wording that labels this news as breaking, copied from the text "
        "or the attached image, or null if there is none.",
    )
    breaking_label: bool = Field(
        default=False,
        description="True if the reporter labels this confirmed news as breaking in the text "
        "or an attached image.",
    )
    categories: list[Literal["transactions", "injuries", "discipline", "rumors"]] = Field(
        description="Topic channels this story belongs in."
    )
    headline: str = Field(description="Short factual headline.")
    reason: str = Field(description="One short sentence explaining the decision.")

    @property
    def labeled(self) -> bool:
        """The model saw a breaking label, and the wording it quotes is on the allowed list."""
        return self.breaking_label and bool(ALLOWED_LABEL.search(self.label_text or ""))

    @property
    def breaking(self) -> bool:
        """Breaking news is whatever a reporter labels as breaking; there's no judgment call."""
        return self.newsworthy and self.labeled

    def channels(self) -> list[str]:
        """Channels to post to, in posting order. Empty means don't post. Every posted story
        goes to all-news, including news that fits no topic channel."""
        if not self.newsworthy or self.duplicate_of is not None:
            return []
        keys = {*self.categories, ALL_NEWS}
        if self.breaking:
            keys.add(BREAKING)
        return ordered(keys)


@dataclass
class Usage:
    """Tokens and time for one model call."""

    model: str
    input_tokens: int = 0  # uncached
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    seconds: float = 0.0

    @property
    def cost(self) -> float | None:
        """Estimated USD, or None for a model without a known price."""
        return self._cost(self.input_tokens, self.cached_tokens, self.cache_write_tokens)

    @property
    def cold_cost(self) -> float | None:
        """What the call would cost if nothing had been cached, as when posts are far apart."""
        prompt = self.input_tokens + self.cached_tokens + self.cache_write_tokens
        if self.model.startswith("claude-"):
            return self._cost(self.input_tokens, 0, prompt - self.input_tokens)
        return self._cost(prompt, 0, 0)

    def _cost(self, uncached: int, cached: int, cache_writes: int) -> float | None:
        price = PRICES.get(self.model)
        if price is None:
            return None
        inp, cached_price, out = price
        tokens = (
            uncached * inp
            + cached * cached_price
            + cache_writes * inp * 1.25
            + self.output_tokens * out
        )
        return tokens / 1e6 + self.web_searches * WEB_SEARCH_PRICE


class ClassifierError(Exception):
    pass


class Classifier:
    """Model names starting with `claude-` go to Anthropic; everything else goes to OpenAI."""

    def __init__(
        self,
        settings: Settings,
        client: AsyncOpenAI | None = None,
        claude_client=None,
        on_usage: Callable[[Usage], None | Awaitable[None]] | None = None,
    ):
        self._settings = settings
        self._openai = client
        self._claude = claude_client
        self._on_usage = on_usage
        if settings.web_search and settings.openai_model.startswith("claude-"):
            log.warning("Web search is only supported for OpenAI models; Claude won't search")

    async def classify(self, tweet: Tweet, stories: Sequence[Story]) -> Decision:
        return await self._ask(
            self._settings.openai_model, tweet, stories, web_search=self._settings.web_search
        )

    async def _ask(
        self,
        model: str,
        tweet: Tweet,
        stories: Sequence[Story],
        *,
        web_search: bool,
    ) -> Decision:
        claude = model.startswith("claude-")
        web_search = web_search and not claude
        # Read on every call so edits to prompt.md apply without a restart.
        instructions = PROMPT_PATH.read_text()
        if web_search:
            instructions += "\n" + RESEARCH_NOTE
        # The image only matters for spotting a BREAKING graphic, so skip it when the text
        # already carries a label.
        image = tweet.image_url if not LABEL_WORDS.search(tweet.text) else None
        prompt = build_input(tweet, stories, with_image=bool(image))
        started = time.monotonic()
        try:
            decision, usage = await self._send(model, instructions, prompt, image, web_search)
        except Exception as e:
            if not image:
                raise
            # An image X won't serve anymore shouldn't cost us the post.
            log.warning("Retrying post %s without its image: %s", tweet.id, e)
            prompt = build_input(tweet, stories)
            decision, usage = await self._send(model, instructions, prompt, None, web_search)
        usage.seconds = time.monotonic() - started
        if self._on_usage:
            result = self._on_usage(usage)
            if inspect.isawaitable(result):
                await result
        known = {s.id for s in stories}
        if decision.duplicate_of is not None and decision.duplicate_of not in known:
            decision.duplicate_of = None
        decision.categories = ordered(decision.categories)
        return decision

    async def _send(
        self, model: str, instructions: str, prompt: str, image: str | None, web_search: bool
    ) -> tuple[Decision, Usage]:
        if model.startswith("claude-"):
            return await self._ask_claude(model, instructions, prompt, image)
        return await self._ask_openai(model, instructions, prompt, image, web_search)

    async def _ask_openai(
        self, model: str, instructions: str, prompt: str, image: str | None, web_search: bool
    ) -> tuple[Decision, Usage]:
        if self._openai is None:
            self._openai = AsyncOpenAI(
                api_key=self._settings.openai_api_key, max_retries=3, timeout=180
            )
        options = {}
        if web_search:
            options["tools"] = [
                {
                    "type": "web_search",
                    "search_context_size": "low",
                    "user_location": {"type": "approximate", "country": "US"},
                }
            ]
            options["max_tool_calls"] = 2
        content = [{"type": "input_text", "text": prompt}]
        if image:
            # Low detail is plenty to read a banner, and costs ~300 tokens.
            content.append({"type": "input_image", "image_url": image, "detail": "low"})
        resp = await self._openai.responses.parse(
            model=model,
            instructions=instructions,
            input=[{"role": "user", "content": content}],
            reasoning={"effort": self._settings.reasoning_effort},
            text_format=Decision,
            store=False,
            **options,
        )
        usage = Usage(model)
        if u := getattr(resp, "usage", None):
            details = getattr(u, "input_tokens_details", None)
            cached = (details.cached_tokens if details else 0) or 0
            usage.input_tokens = u.input_tokens - cached
            usage.cached_tokens = cached
            usage.output_tokens = u.output_tokens
        usage.web_searches = sum(
            1 for item in getattr(resp, "output", None) or [] if item.type == "web_search_call"
        )
        if resp.output_parsed is None:
            raise ClassifierError(f"model returned no decision (status: {resp.status})")
        return resp.output_parsed, usage

    async def _ask_claude(
        self, model: str, instructions: str, prompt: str, image: str | None
    ) -> tuple[Decision, Usage]:
        if self._claude is None:
            from anthropic import AsyncAnthropic

            self._claude = AsyncAnthropic(
                api_key=self._settings.anthropic_api_key or None, max_retries=3, timeout=180
            )
        # Claude's effort levels start at "low".
        effort = self._settings.reasoning_effort
        if effort in ("none", "minimal"):
            effort = "low"
        content = [{"type": "text", "text": prompt}]
        if image:
            content.append({"type": "image", "source": {"type": "url", "url": image}})
        resp = await self._claude.messages.parse(
            model=model,
            max_tokens=4096,
            # The instructions are the same on every call, so cache them.
            system=[{"type": "text", "text": instructions, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort},
            output_format=Decision,
        )
        u = resp.usage
        usage = Usage(
            model,
            input_tokens=u.input_tokens,
            cached_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
            output_tokens=u.output_tokens,
        )
        if resp.stop_reason != "end_turn" or resp.parsed_output is None:
            raise ClassifierError(f"model returned no decision (stop reason: {resp.stop_reason})")
        return resp.parsed_output, usage


def build_input(tweet: Tweet, stories: Sequence[Story], *, with_image: bool = False) -> str:
    # Stories come first and oldest-first so consecutive calls share a long cacheable prefix.
    lines = ["Stories already posted (oldest first):"]
    if not stories:
        lines.append("(none)")
    for s in stories:
        when = s.created_at.astimezone(UTC).strftime("%a %b %d %H:%M UTC")
        lines.append(
            f"[{s.id}] {when} · @{s.first_author} · {', '.join(s.channels)} · {s.headline}"
        )
    lines += [
        "",
        f"Current time: {datetime.now(UTC):%a %b %d %Y %H:%M UTC}",
        "",
        "New post to evaluate:",
        f"Author: {tweet.author_name} (@{tweet.username})",
        f"Posted: {tweet.created_at.astimezone(UTC):%a %b %d %Y %H:%M UTC}",
    ]
    if tweet.is_self_reply:
        lines.append("This post is a reply in the author's own thread.")
        if tweet.parent_text:
            lines += ["Previous post in the thread:", _quote(tweet.parent_text)]
    if tweet.quoted_text:
        lines += ["It quotes this post:", _quote(tweet.quoted_text)]
    lines += ["Text:", _quote(tweet.text)]
    if with_image:
        lines.append("The post's attached image is included.")
    return "\n".join(lines)


def _quote(text: str) -> str:
    return '"""\n' + text.strip() + '\n"""'
