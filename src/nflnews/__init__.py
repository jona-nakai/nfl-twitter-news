"""NFL breaking-news Discord bot."""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
from datetime import UTC, datetime, timedelta

import discord

from .config import FatalError, Settings

# Manage Channels, Manage Roles, View Channels, Send Messages, Embed Links, Read Message
# History, Add Reactions (to seed the #subscribe panel), and Mention @everyone/All Roles (to
# ping the bot's own non-mentionable roles).
INVITE_PERMISSIONS = discord.Permissions(
    manage_channels=True,
    manage_roles=True,
    view_channel=True,
    send_messages=True,
    embed_links=True,
    read_message_history=True,
    add_reactions=True,
    mention_everyone=True,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="nflnews", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="Run the bot (default)")
    run.add_argument(
        "--since-hours",
        type=float,
        help="On startup, first process posts from the last N hours (X reads cost money)",
    )
    sub.add_parser("invite-url", help="Print the link to add the bot to your server")
    classify = sub.add_parser("classify", help="Run a post through the LLM and print its decision")
    classify.add_argument("text")
    classify.add_argument("--author", default="AdamSchefter")
    evals = sub.add_parser("eval", help="Score models on the labeled posts in evals/cases.json")
    evals.add_argument(
        "models",
        nargs="*",
        help="Models to compare (default: gpt-6-luna claude-haiku-5-5 gpt-6.1-sol)",
    )
    evals.add_argument("--effort", help="Reasoning effort (default: OPENAI_REASONING_EFFORT)")
    evals.add_argument("--web-search", action="store_true", help="Allow web search (costs more)")
    evals.add_argument("--repeat", type=int, default=1, help="Run each post N times")
    evals.add_argument("--only", help="Comma-separated case ids to run")
    evals.add_argument("--cases", default="evals/cases.json")
    args = parser.parse_args()

    discord.utils.setup_logging(level=logging.INFO)
    settings = Settings.from_env()
    if args.command == "invite-url":
        settings.require("discord_token")
        print(invite_url(settings.discord_token))
    elif args.command == "classify":
        settings.require(*settings.llm_key_names())
        asyncio.run(_classify(settings, args.text, args.author.lstrip("@")))
    elif args.command == "eval":
        _eval(settings, args)
    else:
        settings.require("discord_token", "x_bearer_token", *settings.llm_key_names())
        try:
            since = getattr(args, "since_hours", None)
            start_from = datetime.now(UTC) - timedelta(hours=since) if since else None
            asyncio.run(_run(settings, start_from))
        except* FatalError as group:
            raise SystemExit("\n".join(f"Error: {e}" for e in group.exceptions)) from None


def invite_url(token: str) -> str:
    # A bot token's first segment is its application id, base64-encoded.
    head = token.split(".")[0]
    app_id = base64.b64decode(head + "=" * (-len(head) % 4)).decode()
    return discord.utils.oauth_url(
        app_id, permissions=INVITE_PERMISSIONS, scopes=("bot", "applications.commands")
    )


async def _run(settings: Settings, start_from: datetime | None = None) -> None:
    from .bot import NewsBot
    from .classifier import Classifier
    from .db import Store
    from .pipeline import Pipeline
    from .x_api import TweetSource, XClient

    store = await Store.open(settings.db_path)
    x_client = XClient(settings.x_bearer_token)
    try:
        bot = NewsBot(settings, store)

        async def record_cost(usage) -> None:
            if usage.cost is not None:
                await store.add_llm_cost(usage.model, usage.cost)

        pipeline = Pipeline(store, Classifier(settings, on_usage=record_cost), bot, settings)
        source = TweetSource(x_client, store, settings, pipeline.submit, start_from)
        bot.pipeline, bot.source, bot.x_client = pipeline, source, x_client
        async with bot:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(bot.run_until_closed())
                tasks.create_task(pipeline.run())
                tasks.create_task(source.run())
                tasks.create_task(bot.run_credits())
    finally:
        await x_client.aclose()
        await store.close()


async def _classify(settings: Settings, text: str, author: str) -> None:
    from .classifier import Classifier
    from .db import Store
    from .x_api import Tweet

    store = await Store.open(settings.db_path)
    try:
        stories = await store.recent_stories(
            hours=settings.story_window_hours, limit=settings.max_recent_stories
        )
    finally:
        await store.close()
    tweet = Tweet(
        id="cli",
        author_id="cli",
        username=author,
        author_name=author,
        avatar_url=None,
        text=text,
        created_at=datetime.now(UTC),
        simulated=True,
    )
    decision = await Classifier(settings).classify(tweet, stories)
    print(decision.model_dump_json(indent=2))
    print("channels:", ", ".join(decision.channels()) or "(not posted)")


def _eval(settings: Settings, args: argparse.Namespace) -> None:
    from dataclasses import replace
    from pathlib import Path

    from . import evals

    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    models = args.models or list(evals.DEFAULT_MODELS)
    settings = replace(
        settings,
        reasoning_effort=args.effort or settings.reasoning_effort,
        web_search=args.web_search,
    )
    for model in models:
        setup = replace(settings, openai_model=model)
        setup.require(*setup.llm_key_names())
    cases = evals.load_cases(Path(args.cases))
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.id in wanted]
    print(
        f"{len(cases)} posts x {len(models)} setups x{args.repeat}, effort "
        f"{settings.reasoning_effort}, web search {'on' if args.web_search else 'off'}. "
        f"Rough cost: ~${evals.rough_estimate(models, len(cases) * args.repeat):.2f}"
    )
    results = asyncio.run(evals.run_eval(settings, models, cases, repeat=args.repeat))
    print()
    print(evals.report(results))
    print(f"Details: {evals.save(results)}")
