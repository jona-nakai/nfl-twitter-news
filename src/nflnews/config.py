from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_ACCOUNTS = (
    "AdamSchefter",
    "RapSheet",
    "TomPelissero",
    "JFowlerESPN",
    "Schultz_Report",
)

BREAKING = "breaking"
TRANSACTIONS = "transactions"
INJURIES = "injuries"
DISCIPLINE = "discipline"
RUMORS = "rumors"
ALL_NEWS = "all-news"


class FatalError(Exception):
    """A configuration problem the bot can't recover from; shown to the user without a
    traceback."""


@dataclass(frozen=True)
class NewsChannel:
    key: str
    channel_name: str
    label: str
    short_label: str
    emoji: str
    color: int
    blurb: str


# Order matters: a story is posted to its channels in this order, and each subscriber is
# pinged only in the first of those channels they subscribe to.
NEWS_CHANNELS = (
    NewsChannel(
        BREAKING,
        "breaking-news",
        "Breaking News",
        "Breaking",
        "🚨",
        0xE53935,
        "Stories reporters flag as BREAKING: big trades, signings, injuries, firings.",
    ),
    NewsChannel(
        TRANSACTIONS,
        "transactions",
        "Transactions",
        "Transactions",
        "🔁",
        0x1E88E5,
        "Trades, signings, extensions, releases, waiver claims, coaching/GM hires and firings.",
    ),
    NewsChannel(
        INJURIES,
        "injuries",
        "Injuries",
        "Injuries",
        "🏥",
        0xFB8C00,
        "Injuries, diagnoses, IR moves, game statuses and return timelines.",
    ),
    NewsChannel(
        DISCIPLINE,
        "discipline",
        "Discipline",
        "Discipline",
        "🚩",
        0xFDD835,
        "Suspensions, fines, arrests and legal trouble, league investigations, reinstatements.",
    ),
    NewsChannel(
        RUMORS,
        "rumors",
        "Rumors",
        "Rumors",
        "👀",
        0x8E24AA,
        "Trade talks, interest, visits, trade requests and other moves that aren't done yet.",
    ),
    NewsChannel(
        ALL_NEWS,
        "all-news",
        "All News",
        "All News",
        "📰",
        0x546E7A,
        "Every story the bot posts, plus league-wide news that fits no other channel.",
    ),
)
CHANNELS_BY_KEY = {c.key: c for c in NEWS_CHANNELS}
CHANNEL_KEYS = tuple(c.key for c in NEWS_CHANNELS)
CATEGORY_KEYS = (TRANSACTIONS, INJURIES, DISCIPLINE, RUMORS)


@dataclass(frozen=True)
class Settings:
    discord_token: str
    guild_id: int | None
    openai_api_key: str
    openai_model: str
    reasoning_effort: str
    web_search: bool
    x_bearer_token: str
    x_accounts: tuple[str, ...]
    x_mode: str
    poll_seconds: float
    backfill_minutes: int
    db_path: Path
    category_name: str
    subscribe_channel_name: str
    log_channel_name: str | None
    credits_channel_name: str | None
    story_window_hours: int
    max_recent_stories: int
    # Only needed when a claude-* model is configured.
    anthropic_api_key: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        accounts = os.getenv("X_ACCOUNTS", "")
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        log_channel = os.getenv("LOG_CHANNEL_NAME", "newsbot-log").strip()
        credits_channel = os.getenv("CREDITS_CHANNEL_NAME", "api-credits").strip()
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            guild_id=int(guild_id) if guild_id else None,
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            # Luna reads every post, without searching (cheapest).
            openai_model=os.getenv("OPENAI_MODEL", "gpt-6-luna").strip(),
            reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "low").strip(),
            web_search=_bool(os.getenv("OPENAI_WEB_SEARCH", "false")),
            x_bearer_token=os.getenv("X_BEARER_TOKEN", "").strip(),
            x_accounts=tuple(a.strip().lstrip("@") for a in accounts.split(",") if a.strip())
            or DEFAULT_ACCOUNTS,
            x_mode=os.getenv("X_MODE", "stream").strip().lower(),
            poll_seconds=float(os.getenv("X_POLL_SECONDS", "15")),
            backfill_minutes=int(os.getenv("X_BACKFILL_MINUTES", "30")),
            db_path=Path(os.getenv("DB_PATH", "data/nflnews.db")),
            category_name=os.getenv("DISCORD_CATEGORY_NAME", "NFL News").strip(),
            subscribe_channel_name=os.getenv("SUBSCRIBE_CHANNEL_NAME", "subscribe").strip(),
            log_channel_name=log_channel or None,
            credits_channel_name=credits_channel or None,
            story_window_hours=int(os.getenv("STORY_WINDOW_HOURS", "48")),
            max_recent_stories=int(os.getenv("MAX_RECENT_STORIES", "150")),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        )

    def llm_key_names(self) -> list[str]:
        """The API-key settings the configured model needs."""
        return [
            "anthropic_api_key" if self.openai_model.startswith("claude-") else "openai_api_key"
        ]

    def require(self, *names: str) -> None:
        """Exit with a readable message if any of the given env-backed settings are empty."""
        env_names = {
            "discord_token": "DISCORD_TOKEN",
            "openai_api_key": "OPENAI_API_KEY",
            "anthropic_api_key": "ANTHROPIC_API_KEY",
            "x_bearer_token": "X_BEARER_TOKEN",
        }
        missing = [env_names[n] for n in names if not getattr(self, n)]
        if missing:
            raise SystemExit(f"Missing required settings in .env: {', '.join(missing)}")
        if self.x_mode not in ("stream", "poll"):
            raise SystemExit(f"X_MODE must be 'stream' or 'poll', got {self.x_mode!r}")


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")
