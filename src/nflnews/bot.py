from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .config import ALL_NEWS, BREAKING, CHANNELS_BY_KEY, NEWS_CHANNELS, FatalError, Settings
from .db import Store
from .pings import ALL_COMBOS, COMBO_ROLE_NAME, Combo, combo_key, ordered, ping_plan
from .pipeline import DUPLICATE, ERROR, POSTED, UPGRADED, Result, StoryPost
from .x_api import Tweet, snowflake_time

if TYPE_CHECKING:
    from .pipeline import Pipeline
    from .x_api import TweetSource, XClient

log = logging.getLogger(__name__)

# The bot never joins voice; skip discord.py's startup warnings about missing voice libraries.
discord.VoiceClient.warn_nacl = discord.VoiceClient.warn_dave = False

PANEL_EMOJI = {ch.emoji: ch.key for ch in NEWS_CHANNELS}
# Checking X credits is free; every 2 minutes stays well under X's rate limits.
CREDITS_REFRESH_SECONDS = 2 * 60
OPENAI_BALANCE_KEY = "openai:balance"
LOW_X_BALANCE = 5.0
LOW_OPENAI_BALANCE = 2.0


@dataclass
class Layout:
    guild: discord.Guild
    channels: dict[str, discord.TextChannel]
    sub_roles: dict[str, discord.Role]
    combo_roles: dict[Combo, discord.Role]
    subscribe_channel: discord.TextChannel
    panel_message_id: int
    log_channel: discord.TextChannel | None
    credits_channel: discord.TextChannel | None

    def subscriptions(self, member: discord.Member) -> frozenset[str]:
        return frozenset(k for k, role in self.sub_roles.items() if role in member.roles)


class NewsBot(discord.Client):
    def __init__(self, settings: Settings, store: Store):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True  # to keep combo roles in sync with subscription roles
        intents.guild_reactions = True  # subscriptions are reactions on the #subscribe panel
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.store = store
        self.tree = app_commands.CommandTree(self)
        self.pipeline: Pipeline | None = None
        self.source: TweetSource | None = None
        self.x_client: XClient | None = None
        self.layout: Layout | None = None
        self.layout_ready = asyncio.Event()
        self._pending_syncs: dict[int, asyncio.Task] = {}
        self._member_locks: dict[int, asyncio.Lock] = {}
        self.fatal_error: Exception | None = None

    async def run_until_closed(self) -> None:
        try:
            await self.start(self.settings.discord_token)
        except discord.LoginFailure as e:
            raise FatalError(
                "Discord rejected DISCORD_TOKEN. Reset it in the developer portal."
            ) from e
        except discord.PrivilegedIntentsRequired as e:
            raise FatalError(
                "Turn on the SERVER MEMBERS INTENT for the bot in the Discord developer portal "
                "(Bot tab → Privileged Gateway Intents)."
            ) from e
        raise self.fatal_error or FatalError("Disconnected from Discord")

    async def on_ready(self) -> None:
        if self.layout_ready.is_set() or self.fatal_error:
            return
        try:
            guild = self._pick_guild()
            log.info("Logged in as %s; setting up %s", self.user, guild.name)
            self.layout = await self._ensure_layout(guild)
            self.tree.add_command(AdminCommands(self), guild=guild)
            await self.tree.sync(guild=guild)
            await self._sync_subscriptions_from_reactions()
        except discord.Forbidden as e:
            self.fatal_error = FatalError(
                f"The bot is missing permissions in the server ({e.text}). Re-invite it with "
                "`uv run nflnews invite-url`, and make sure its role sits above the roles it "
                "manages in Server Settings → Roles."
            )
        except Exception as e:
            self.fatal_error = e
        if self.fatal_error:
            await self.close()
            return
        self.layout_ready.set()
        log.info("Discord layout ready")

    def _pick_guild(self) -> discord.Guild:
        if self.settings.guild_id:
            guild = self.get_guild(self.settings.guild_id)
            if guild is None:
                raise FatalError(
                    f"The bot isn't in server DISCORD_GUILD_ID={self.settings.guild_id}"
                )
            return guild
        if len(self.guilds) != 1:
            raise FatalError(
                f"The bot is in {len(self.guilds)} servers; set DISCORD_GUILD_ID to pick one. "
                "Get an invite link with `uv run nflnews invite-url`."
            )
        return self.guilds[0]

    # ---- server layout -------------------------------------------------------------------

    async def _ensure_layout(self, guild: discord.Guild) -> Layout:
        sub_roles = {
            ch.key: await self._ensure_role(guild, f"discord:role:{ch.key}", ch.label, ch.color)
            for ch in NEWS_CHANNELS
        }
        combo_roles = {
            combo: await self._ensure_role(
                guild, f"discord:combo:{combo_key(combo)}", COMBO_ROLE_NAME, find_by_name=False
            )
            for combo in ALL_COMBOS
        }

        category = guild.get_channel(int(await self.store.get("discord:category") or 0))
        if not isinstance(category, discord.CategoryChannel):
            category = discord.utils.get(guild.categories, name=self.settings.category_name)
        if category is None:
            category = await guild.create_category(
                self.settings.category_name, reason="NFL news bot"
            )
        await self.store.set("discord:category", str(category.id))

        me = guild.me
        read_only = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        private = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        subscribe_channel = await self._ensure_channel(
            guild,
            category,
            "subscribe",
            self.settings.subscribe_channel_name,
            "Pick which news channels ping you.",
            read_only,
            position=0,
        )
        channels = {
            ch.key: await self._ensure_channel(
                guild, category, ch.key, ch.channel_name, f"{ch.emoji} {ch.blurb}", read_only
            )
            for ch in NEWS_CHANNELS
        }
        log_channel = None
        if self.settings.log_channel_name:
            log_channel = await self._ensure_channel(
                guild,
                category,
                "log",
                self.settings.log_channel_name,
                "What the news bot did with every post, and why. Admins only.",
                private,
            )

        credits_channel = None
        if self.settings.credits_channel_name:
            credits_channel = await self._ensure_channel(
                guild,
                category,
                "credits",
                self.settings.credits_channel_name,
                "X and OpenAI credits left. Admins only.",
                private,
            )

        panel_message_id = await self._ensure_panel(subscribe_channel, channels)
        return Layout(
            guild,
            channels,
            sub_roles,
            combo_roles,
            subscribe_channel,
            panel_message_id,
            log_channel,
            credits_channel,
        )

    async def _ensure_role(
        self,
        guild: discord.Guild,
        kv_key: str,
        name: str,
        color: int | None = None,
        *,
        find_by_name: bool = True,
    ) -> discord.Role:
        """`find_by_name` adopts an existing role with this name; combo roles all share one name,
        so they're only ever found by their saved id."""
        role_id = await self.store.get(kv_key)
        role = guild.get_role(int(role_id)) if role_id else None
        if role is None and find_by_name:
            role = discord.utils.get(guild.roles, name=name)
        if role is not None and role.name != name:
            await role.edit(name=name, reason="NFL news bot subscriptions")
        if role is None:
            role = await guild.create_role(
                name=name,
                color=discord.Color(color) if color else discord.Color.default(),
                mentionable=False,
                hoist=False,
                permissions=discord.Permissions.none(),
                reason="NFL news bot subscriptions",
            )
            log.info("Created role %s", name)
        await self.store.set(kv_key, str(role.id))
        return role

    async def _ensure_channel(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        key: str,
        name: str,
        topic: str,
        overwrites: dict,
        position: int | None = None,
    ) -> discord.TextChannel:
        """Find the channel by remembered id, then by name; create it if missing. Existing
        channels are left as-is so admins can rename, move, or re-permission them."""
        kv_key = f"discord:channel:{key}"
        channel_id = await self.store.get(kv_key)
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = discord.utils.get(guild.text_channels, name=name)
        if channel is None:
            kwargs = {"position": position} if position is not None else {}
            channel = await guild.create_text_channel(
                name,
                category=category,
                topic=topic,
                overwrites=overwrites,
                reason="NFL news bot",
                **kwargs,
            )
            log.info("Created channel #%s", name)
        await self.store.set(kv_key, str(channel.id))
        return channel

    async def _ensure_panel(
        self, channel: discord.TextChannel, channels: dict[str, discord.TextChannel]
    ) -> int:
        """Post (or refresh) the #subscribe message and seed it with one reaction per channel."""
        embed = panel_embed(channels)
        message = None
        message_id = await self.store.get("discord:panel")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(embed=embed, view=None)
            except discord.NotFound:
                message = None
        if message is None:
            message = await channel.send(embed=embed)
            await self.store.set("discord:panel", str(message.id))
        try:
            for emoji in PANEL_EMOJI:
                await message.add_reaction(emoji)
        except discord.Forbidden:
            log.warning(
                "Can't add reactions in #%s. Give the bot the Add Reactions permission there, "
                "or add the %s reactions to the panel yourself.",
                channel.name,
                " ".join(PANEL_EMOJI),
            )
        return message.id

    # ---- subscriptions -------------------------------------------------------------------

    async def sync_member(self, member: discord.Member) -> None:
        """Give the member exactly the one combo role matching their subscription roles."""
        layout = self.layout
        if layout is None or member.bot:
            return
        want = layout.combo_roles.get(layout.subscriptions(member))
        combo_role_ids = {r.id for r in layout.combo_roles.values()}
        stale = [r for r in member.roles if r.id in combo_role_ids and r != want]
        if stale:
            await member.remove_roles(*stale, reason="NFL news subscription sync")
        if want and want not in member.roles:
            await member.add_roles(want, reason="NFL news subscription sync")

    async def _sync_subscriptions_from_reactions(self) -> None:
        """Make everyone's roles match their reactions on the panel. Catches reactions added or
        removed while the bot was offline; reactions are the source of truth."""
        layout = self.layout
        guild = layout.guild
        if not guild.chunked:
            await guild.chunk()
        message = await layout.subscribe_channel.fetch_message(layout.panel_message_id)
        reacted: dict[int, set[str]] = {}
        for reaction in message.reactions:
            key = PANEL_EMOJI.get(str(reaction.emoji))
            if key:
                async for user in reaction.users():
                    reacted.setdefault(user.id, set()).add(key)
        for member in guild.members:
            if member.bot:
                continue
            try:
                await self.set_subscriptions(member, frozenset(reacted.get(member.id, ())))
            except discord.HTTPException as e:
                log.warning("Couldn't sync roles for %s: %s", member, e)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._on_panel_reaction(payload, added=True)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._on_panel_reaction(payload, added=False)

    async def _on_panel_reaction(
        self, payload: discord.RawReactionActionEvent, added: bool
    ) -> None:
        layout = self.layout
        if layout is None or payload.message_id != layout.panel_message_id:
            return
        key = PANEL_EMOJI.get(str(payload.emoji))
        member = layout.guild.get_member(payload.user_id)
        if key is None or member is None or member.bot:
            return
        role = layout.sub_roles[key]
        # One role at a time, in order per member; the combo role follows via on_member_update.
        async with self._member_locks.setdefault(member.id, asyncio.Lock()):
            try:
                if added:
                    await member.add_roles(role, reason="NFL news subscription")
                else:
                    await member.remove_roles(role, reason="NFL news subscription")
            except discord.HTTPException as e:
                log.warning("Couldn't update %s for %s: %s", role.name, member, e)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        # Keeps the combo role matching the subscription roles after reactions (or an admin)
        # change them. Role edits arrive as a burst of events, so wait for them to settle first.
        if self.layout and before.roles != after.roles and after.guild == self.layout.guild:
            pending = self._pending_syncs.pop(after.id, None)
            if pending:
                pending.cancel()
            self._pending_syncs[after.id] = asyncio.create_task(self._sync_later(after.id))

    async def _sync_later(self, member_id: int, delay: float = 3.0) -> None:
        await asyncio.sleep(delay)
        self._pending_syncs.pop(member_id, None)
        member = self.layout.guild.get_member(member_id)
        if member is None:
            return
        try:
            await self.sync_member(member)
        except discord.HTTPException as e:
            log.warning("Couldn't sync roles for %s: %s", member, e)

    async def set_subscriptions(self, member: discord.Member, keys: frozenset[str]) -> None:
        """Set subscription roles and the matching combo role in one pass."""
        layout = self.layout
        ours = set(layout.sub_roles.values()) | set(layout.combo_roles.values())
        target = {layout.sub_roles[k] for k in keys}
        if keys:
            target.add(layout.combo_roles[keys])
        current = set(member.roles)
        remove = (current & ours) - target
        add = target - current
        if remove:
            await member.remove_roles(*remove, reason="NFL news subscription")
        if add:
            await member.add_roles(*add, reason="NFL news subscription")

    # ---- publishing ----------------------------------------------------------------------

    async def publish(self, post: StoryPost) -> dict[str, int]:
        await self.layout_ready.wait()
        layout = self.layout
        sent = {}
        for key, ping in ping_plan(post.channels, post.already_posted):
            if ping.base_role:
                roles = [layout.sub_roles[ping.base_role]]
            else:
                roles = [layout.combo_roles[c] for c in ping.combos]
            try:
                message = await layout.channels[key].send(
                    content="-# " + " ".join(r.mention for r in roles),
                    embed=story_embed(post, key),
                    view=LinkView(post.tweet),
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, users=False, roles=roles
                    ),
                )
            except discord.HTTPException:
                log.exception("Couldn't post story %s to #%s", post.story_id, key)
                continue
            sent[key] = message.id
        return sent

    async def log_result(self, tweet: Tweet, result: Result) -> None:
        await self.layout_ready.wait()
        if self.layout.log_channel is None:
            return
        d = result.decision
        who = f"[@{tweet.username}](<{tweet.url}>)"
        if result.outcome == POSTED:
            where = " ".join(self.layout.channels[k].mention for k in result.channels)
            line = f"✅ **Posted** → {where} · {who}: {d.headline}"
        elif result.outcome == UPGRADED:
            story = await self.store.get_story(result.story_id)
            line = (
                f"⬆️ **Upgraded to** {self.layout.channels[BREAKING].mention} · "
                f"#{result.story_id} “{story.headline if story else d.headline}” · {who}"
            )
        elif result.outcome == DUPLICATE:
            story = await self.store.get_story(d.duplicate_of)
            original = (
                f"#{story.id} “{story.headline}” (@{story.first_author})"
                if story
                else f"#{d.duplicate_of}"
            )
            line = f"🔁 **Duplicate** of {original} · {who}"
        elif result.outcome == ERROR:
            line = f"⚠️ **Error** · {who}: `{(result.error or '')[:300]}`"
        else:
            line = f"⏭️ **Skipped** · {who}: {_snippet(tweet.text)}"
        if d is not None:
            line += f"\n-# {d.reason}"
        await self.layout.log_channel.send(line[:2000], suppress_embeds=True)

    # ---- credits ---------------------------------------------------------------------------

    async def run_credits(self) -> None:
        """Keep one message in the credits channel up to date."""
        await self.layout_ready.wait()
        if self.layout.credits_channel is None:
            return
        while True:
            try:
                await self.refresh_credits()
            except Exception:
                log.exception("Couldn't update the credits message")
            await asyncio.sleep(CREDITS_REFRESH_SECONDS)

    async def refresh_credits(self) -> None:
        channel = self.layout.credits_channel
        if channel is None:
            return
        x_credits = x_usage = None
        if self.x_client:
            try:
                x_credits = await self.x_client.credits()
                x_usage = await self.x_client.post_usage()
            except Exception as e:
                log.warning("Couldn't load X credits: %s", e)
        now = datetime.now(UTC)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spend = {
            "today": await self.store.llm_spend_since(today),
            "7 days": await self.store.llm_spend_since(now - timedelta(days=7)),
            "this month": await self.store.llm_spend_since(today.replace(day=1)),
        }
        balance = await self.store.get_json(OPENAI_BALANCE_KEY)
        remaining = None
        if balance:
            since = datetime.fromisoformat(balance["set_at"])
            remaining = balance["amount"] - await self.store.llm_spend_since(since)
        content = credits_text(now, x_credits, x_usage, spend, balance, remaining)

        message_id = await self.store.get("discord:credits_message")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(content=content)
                return
            except discord.NotFound:
                pass
        message = await channel.send(content)
        await self.store.set("discord:credits_message", str(message.id))


def credits_text(
    now: datetime,
    x_credits: dict | None,
    x_usage: dict | None,
    spend: dict[str, float],
    balance: dict | None,
    remaining: float | None,
) -> str:
    lines = [f"## 💳 API credits\n-# Updated <t:{int(now.timestamp())}:R>", "", "**X**"]
    if x_credits:
        total = x_credits.get("total_balance", 0)
        warn = " ⚠️ running low" if total < LOW_X_BALANCE else ""
        lines.append(f"**${total:.2f} left**{warn}")
        parts = []
        for grant in x_credits.get("free_grants", []):
            expiry = grant.get("expires_at")
            when = (
                f", expires <t:{int(datetime.fromisoformat(expiry.replace('Z', '+00:00')).timestamp())}:D>"
                if expiry
                else ""
            )
            parts.append(f"${grant['amount']:.2f} free credit{when}")
        parts.append(f"${x_credits.get('prepaid_balance', 0):.2f} prepaid")
        lines.append("-# " + " · ".join(parts))
        if x_usage:
            lines.append(
                f"-# {int(x_usage.get('project_usage', 0)):,} posts read this cycle "
                f"(resets on day {x_usage.get('cap_reset_day', '?')} of the month)"
            )
    else:
        lines.append("Couldn't load the X balance right now.")

    lines += ["", "**OpenAI**"]
    if remaining is not None:
        warn = " ⚠️ running low" if remaining < LOW_OPENAI_BALANCE else ""
        set_at = int(datetime.fromisoformat(balance["set_at"]).timestamp())
        lines.append(f"**~${remaining:.2f} left**{warn}")
        lines.append(
            f"-# ${balance['amount']:.2f} on <t:{set_at}:D>, minus what the bot has spent since"
        )
    else:
        lines.append("Set your balance with `/newsbot openai-balance` to see what's left.")
    lines.append("-# Spent: " + " · ".join(f"{k} ${v:.2f}" for k, v in spend.items()))
    lines.append(
        "-# The bot's own estimate from token counts; it doesn't see other use of the key. "
        "OpenAI's exact numbers: <https://platform.openai.com/usage>"
    )
    return "\n".join(lines)


# ---- views and embeds --------------------------------------------------------------------


def panel_embed(channels: dict[str, discord.TextChannel]) -> discord.Embed:
    lines = [
        "**React below** to get pinged when news drops in a channel. Remove your reaction to "
        "stop. Every channel is open to everyone either way — this only controls pings.",
        "",
    ]
    for ch in NEWS_CHANNELS:
        lines.append(f"{ch.emoji} **{ch.label}** — {channels[ch.key].mention}\n{ch.blurb}")
    lines += [
        "",
        "You're pinged **once per story**. Big news often lands in more than one channel "
        "(say, breaking news *and* injuries) — you'll only be pinged in the first of your "
        "channels it's posted to, in the order above.",
    ]
    return discord.Embed(
        title="🏈 Choose your NFL news pings", description="\n".join(lines), color=0x013369
    )


def story_embed(post: StoryPost, key: str) -> discord.Embed:
    tweet = post.tweet
    title = post.headline
    if post.breaking and key != BREAKING:
        title = "🚨 " + title
    embed = discord.Embed(
        title=title[:256],
        url=tweet.url,
        description=tweet.text[:4096],
        color=CHANNELS_BY_KEY[key].color,
        timestamp=tweet.created_at,
    )
    embed.set_author(
        name=f"{tweet.author_name} (@{tweet.username})",
        url=f"https://x.com/{tweet.username}",
        icon_url=tweet.avatar_url,
    )
    if tweet.quoted_text:
        embed.add_field(name="Quoting", value=tweet.quoted_text[:1024], inline=False)
    # Every story is in #all-news, so only that channel lists where else it went.
    others = [
        CHANNELS_BY_KEY[k]
        for k in ordered((*post.channels, *post.already_posted))
        if k not in (key, ALL_NEWS)
    ]
    footer = "Simulated test post" if tweet.simulated else "via X"
    if others:
        footer += " · also in " + ", ".join(f"#{c.channel_name}" for c in others)
    embed.set_footer(text=footer)
    return embed


class LinkView(discord.ui.View):
    def __init__(self, tweet: Tweet):
        super().__init__(timeout=None)
        label = "Open on X" if not tweet.simulated else "Open profile on X"
        self.add_item(discord.ui.Button(label=label, url=tweet.url))


def _snippet(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---- admin slash commands ----------------------------------------------------------------


@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
class AdminCommands(app_commands.Group):
    def __init__(self, bot: NewsBot):
        super().__init__(name="newsbot", description="NFL news bot admin tools")
        self.bot = bot

    @app_commands.command(description="Show what the news bot is doing")
    async def status(self, interaction: discord.Interaction) -> None:
        source, pipeline = self.bot.source, self.bot.pipeline
        counts = await self.bot.store.outcome_counts(hours=24)
        last_id = await self.bot.store.last_tweet_id()
        lines = [
            f"**X feed:** {source.mode if source else '?'} mode, "
            f"{'connected' if source and source.connected else 'not connected'}"
            + (f" — ⚠️ {source.problem}" if source and source.problem else ""),
            f"**Accounts:** {', '.join('@' + a.username for a in source.accounts.values()) if source else '?'}",
            f"**Models:** {_models_summary(self.bot.settings)}",
            f"**Queue:** {pipeline.backlog if pipeline else '?'} posts waiting",
            "**Last 24h:** "
            + (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "nothing yet"),
        ]
        if last_id:
            lines.append(f"**Last post handled:** <t:{int(snowflake_time(last_id).timestamp())}:R>")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="openai-balance",
        description="Tell the bot your current OpenAI credit balance (from your billing page)",
    )
    @app_commands.describe(
        dollars="Balance shown at platform.openai.com/settings/organization/billing"
    )
    async def openai_balance(self, interaction: discord.Interaction, dollars: float) -> None:
        await self.bot.store.set(
            OPENAI_BALANCE_KEY,
            json.dumps({"amount": dollars, "set_at": datetime.now(UTC).isoformat()}),
        )
        await interaction.response.send_message(
            f"Got it: ${dollars:.2f}. The credits channel will count down from here.",
            ephemeral=True,
        )
        await self.bot.refresh_credits()

    @app_commands.command(
        description="Run a made-up post through the classifier (and optionally post it)"
    )
    @app_commands.describe(
        text="The post text to test",
        author="X handle to pretend it came from",
        post="Actually post it (with pings) to the channels it's routed to",
    )
    async def simulate(
        self,
        interaction: discord.Interaction,
        text: str,
        author: str = "AdamSchefter",
        post: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        tweet = _simulated_tweet(self.bot, text, author.lstrip("@"))
        pipeline = self.bot.pipeline
        try:
            if post:
                result = await pipeline.process(tweet)
                decision, outcome = result.decision, result.outcome
            else:
                decision, outcome = await pipeline.preview(tweet), "preview only"
        except Exception as e:
            await interaction.followup.send(f"Failed: `{type(e).__name__}: {e}`", ephemeral=True)
            return
        if decision is None:
            await interaction.followup.send(f"Outcome: **{outcome}**", ephemeral=True)
            return
        channels = decision.channels()
        lines = [
            f"**Outcome:** {outcome}",
            f"**Headline:** {decision.headline}",
            f"**Newsworthy:** {decision.newsworthy} · **Breaking:** {decision.breaking} · "
            f"**Duplicate of:** {decision.duplicate_of or '—'}",
            f"**Channels:** {', '.join('#' + CHANNELS_BY_KEY[k].channel_name for k in channels) or 'none'}",
            f"**Reason:** {decision.reason}",
        ]
        await interaction.followup.send("\n".join(lines), ephemeral=True)


def _models_summary(settings: Settings) -> str:
    search = "with web search" if settings.web_search else "no web search"
    return f"{settings.openai_model} ({search}) · reasoning: {settings.reasoning_effort}"


def _simulated_tweet(bot: NewsBot, text: str, username: str) -> Tweet:
    account = None
    if bot.source:
        account = next(
            (a for a in bot.source.accounts.values() if a.username.lower() == username.lower()),
            None,
        )
    now = datetime.now(UTC)
    return Tweet(
        id=f"sim-{int(now.timestamp() * 1000)}",
        author_id=account.id if account else "simulated",
        username=account.username if account else username,
        author_name=account.name if account else username,
        avatar_url=account.avatar_url if account else None,
        text=text,
        created_at=now,
        simulated=True,
    )
