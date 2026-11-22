"""Single-file Discord quote bot built with discord.app_commands and SQLAlchemy 2.0."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from enum import IntEnum
from functools import wraps
import io
import os
from pathlib import Path
import time
from typing import Any, Callable

import discord
from discord import app_commands
from dotenv import load_dotenv
from sqlalchemy import ColumnElement, Float, Integer, LargeBinary, Text, TypeDecorator, URL, create_engine, delete, event, func, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DISCORD_MESSAGE_LIMIT = 2000
IMAGE_EXTENSIONS = frozenset({".png", ".jpeg", ".gif", ".jpg", ".webp"})


class Safety(IntEnum):
    sfw = 0
    nsfw = 1


class _SafetyType(TypeDecorator[Safety]):
    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: Safety | int | None, dialect: Any) -> int:
        return int(Safety.sfw) if value == 0 else int(Safety.nsfw)

    def process_result_value(self, value: int | None, dialect: Any) -> Safety:
        return Safety.sfw if value == 0 else Safety.nsfw


class _Base(DeclarativeBase):
    pass


class Quote(_Base):
    __tablename__ = "quotes"
    __table_args__ = {"sqlite_autoincrement": True}

    quoteid: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    safety: Mapped[Safety] = mapped_column(_SafetyType, nullable=True, default=Safety.sfw)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)

    @property
    def has_image(self) -> bool:
        return self.image_data is not None

    @property
    def is_nsfw_image(self) -> bool:
        return self.has_image and self.safety is Safety.nsfw


def is_channel_nsfw(channel: object) -> bool:
    fn = getattr(channel, "is_nsfw", None)
    return bool(fn()) if callable(fn) else False


def is_image_attachment(attachment: discord.Attachment) -> bool:
    return Path(attachment.filename).suffix.lower() in IMAGE_EXTENSIONS


def _format_id_list(prefix: str, ids: list[int], limit: int = DISCORD_MESSAGE_LIMIT) -> str:
    full = f"{prefix}{', '.join(map(str, ids))}."
    if len(full) <= limit:
        return full
    tail = f", ... ({len(ids)} total)."
    shown: list[str] = []
    used = len(prefix) + len(tail)
    for idx, qid in enumerate(ids):
        token = str(qid) if idx == 0 else f", {qid}"
        if used + len(token) > limit:
            break
        shown.append(str(qid))
        used += len(token)
    return f"{prefix}{', '.join(shown)}{tail}"


async def _reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    file: discord.File | None = None,
    ephemeral: bool = False,
) -> None:
    kwargs: dict[str, Any] = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if file is not None:
        kwargs["file"] = file
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await send(**kwargs)


def _require_perm(perm: str) -> Callable[..., Any]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(self: Any, interaction: discord.Interaction, *args: Any, **kwargs: Any) -> Any:
            bot: QuoteBot = self if isinstance(self, QuoteBot) else self.bot
            if not bot.has_permission(interaction, perm):
                await _reply(interaction, "User has insufficient permissions for this action.", ephemeral=True)
                return
            return await func(self, interaction, *args, **kwargs)

        return wrapper

    return decorator


class QuoteDB:
    """Single-file SQLite quote store backed by SQLAlchemy 2.0."""

    def __init__(self, database: str | Path = "quotes.sql") -> None:
        db_target = None if str(database) == ":memory:" else str(database)
        self.engine = create_engine(URL.create("sqlite", database=db_target))

        @event.listens_for(self.engine, "connect")
        def _register_unicode_lower(dbapi_conn: Any, _record: Any) -> None:
            dbapi_conn.create_function("LOWER", 1, lambda s: s.lower() if isinstance(s, str) else s)

        _Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _sfw_channel_filter() -> ColumnElement[bool]:
        return Quote.image_data.is_(None) | (Quote.safety == Safety.sfw)

    def add_quote(
        self,
        message: str | None,
        safety: Safety,
        author: str,
        timestamp: float,
        image: tuple[str, bytes] | None = None,
    ) -> int:
        if message is None and image is None:
            raise ValueError("A quote requires text or an image.")
        with self.session_factory.begin() as session:
            quote = Quote(
                message=message,
                image_name=(Path(image[0]).name or "image.png") if image else None,
                image_data=image[1] if image else None,
                safety=safety,
                author=author,
                timestamp=timestamp,
            )
            session.add(quote)
            session.flush()
            return int(quote.quoteid)

    def delete_quote(self, quote_id: int) -> None:
        with self.session_factory.begin() as session:
            if session.execute(delete(Quote).where(Quote.quoteid == quote_id)).rowcount == 0:
                raise KeyError(f"Quote #{quote_id} not found in the store.")

    def get_quote(self, quote_id: int) -> Quote:
        with self.session_factory() as session:
            quote = session.get(Quote, quote_id)
        if quote is None:
            raise KeyError(f"Quote #{quote_id} not found in the store.")
        return quote

    def get_most_recent_id(self, ceiling: Safety = Safety.sfw) -> int:
        with self.session_factory() as session:
            max_id = session.scalar(select(func.max(Quote.quoteid)).where(Quote.safety <= ceiling))
        if max_id is None:
            raise ValueError("No quotes in the store.")
        return int(max_id)

    def quote_count(self) -> int:
        with self.session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(Quote)) or 0)

    def change_safety(self, quote_id: int, safety: Safety) -> None:
        stmt = update(Quote).where(Quote.quoteid == quote_id).values(safety=safety)
        with self.session_factory.begin() as session:
            if session.execute(stmt).rowcount == 0:
                raise KeyError(f"Quote #{quote_id} not found in the store.")

    def find_quotes(
        self, *, query: str | None = None, author: str | None = None, allow_nsfw_images: bool = True
    ) -> list[int]:
        stmt = select(Quote.quoteid)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(func.lower(Quote.message).like(func.lower(f"%{escaped}%"), escape="\\"))
        if author:
            stmt = stmt.where(func.lower(Quote.author) == func.lower(author))
        if not allow_nsfw_images:
            stmt = stmt.where(self._sfw_channel_filter())
        with self.session_factory() as session:
            return list(session.scalars(stmt))

    def random_quote_id(
        self, *, image_only: bool = False, safety: Safety | None = None, allow_nsfw_images: bool = False
    ) -> int | None:
        stmt = select(Quote.quoteid)
        if safety is not None:
            stmt = stmt.where(Quote.safety == safety)
            if not allow_nsfw_images:
                stmt = stmt.where(self._sfw_channel_filter())
        else:
            stmt = stmt.where(Quote.safety <= (Safety.nsfw if allow_nsfw_images else Safety.sfw))
        if image_only:
            stmt = stmt.where(Quote.image_data.is_not(None))
        stmt = stmt.order_by(func.random()).limit(1)
        with self.session_factory() as session:
            result = session.scalar(stmt)
        return int(result) if result is not None else None


class QuoteTree(app_commands.CommandTree["QuoteBot"]):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None and not self.client.is_owner(interaction.user.id):
            await _reply(interaction, "Direct messages are restricted to the bot owner.", ephemeral=True)
            return False
        return True


class QuoteGroup(app_commands.Group):
    def __init__(self, bot: QuoteBot) -> None:
        super().__init__(name="quote", description="Manage and browse quotes.")
        self.bot = bot

    @app_commands.command(name="add", description="Add a text and/or image quote.")
    @_require_perm("send_messages")
    async def add(
        self,
        interaction: discord.Interaction,
        quote: str | None = None,
        image: discord.Attachment | None = None,
        safety: Safety | None = None,
    ) -> None:
        text = quote.strip() if quote and quote.strip() else None
        if image is not None and not is_image_attachment(image):
            await _reply(interaction, "Only image attachments (.png, .jpg, .jpeg, .gif, .webp) are supported.", ephemeral=True)
            return
        if text is None and image is None:
            await _reply(interaction, "Provide quote text, an image attachment, or both.", ephemeral=True)
            return
        effective_safety = (
            safety
            if safety is not None
            else (Safety.nsfw if image is not None and is_channel_nsfw(interaction.channel) else Safety.sfw)
        )
        await self.bot.save_and_reply(
            interaction, text=text, attachments=[image] if image else [], safety=effective_safety
        )

    @app_commands.command(name="get", description="Post a quote by ID, or the most recent allowed quote if omitted.")
    async def get(self, interaction: discord.Interaction, quote_id: int | None = None) -> None:
        if quote_id is None:
            ceiling = Safety.nsfw if is_channel_nsfw(interaction.channel) else Safety.sfw
            try:
                quote_id = self.bot.db.get_most_recent_id(ceiling)
            except ValueError:
                await _reply(interaction, "No quotes in the store at or below the channel's safety level.")
                return
        await self.bot.post_quote(interaction, quote_id)

    @app_commands.command(name="random", description="Post a random quote.")
    async def random(
        self, interaction: discord.Interaction, image_only: bool = False, safety: Safety | None = None
    ) -> None:
        nsfw_chan = is_channel_nsfw(interaction.channel)
        if safety is Safety.nsfw and image_only and not nsfw_chan:
            await _reply(interaction, "NSFW images are not permitted in this channel.")
            return
        qid = self.bot.db.random_quote_id(image_only=image_only, safety=safety, allow_nsfw_images=nsfw_chan)
        if qid is None:
            kind = "image quotes" if image_only else "quotes"
            await _reply(interaction, f"No {kind} with the given safety level.")
        else:
            await self.bot.post_quote(interaction, qid)

    @app_commands.command(name="find", description="Search quotes by text and/or author, or view the total count.")
    async def find(
        self, interaction: discord.Interaction, query: str | None = None, author: str | None = None
    ) -> None:
        clean_query = query.strip() if query and query.strip() else None
        clean_author = author.strip() if author and author.strip() else None
        if clean_query is None and clean_author is None:
            await _reply(interaction, f"{self.bot.db.quote_count()} quotes in the store.")
            return
        ids = self.bot.db.find_quotes(
            query=clean_query, author=clean_author, allow_nsfw_images=is_channel_nsfw(interaction.channel)
        )
        if not ids:
            await _reply(interaction, "No matching quotes in the store.")
        elif len(ids) == 1:
            await self.bot.post_quote(interaction, ids[0])
        else:
            await _reply(interaction, _format_id_list("Matching quotes include ", ids))

    @app_commands.command(name="set", description="Change the safety level of a quote.")
    @_require_perm("kick_members")
    async def set_safety(self, interaction: discord.Interaction, quote_id: int, safety: Safety) -> None:
        try:
            self.bot.db.change_safety(quote_id, safety)
        except KeyError:
            await _reply(interaction, f"Quote #{quote_id} is not in the store.")
        else:
            await _reply(interaction, f"Changed the safety level of Quote #{quote_id}.")

    @app_commands.command(name="del", description="Delete a quote by ID.")
    @_require_perm("kick_members")
    async def delete(self, interaction: discord.Interaction, quote_id: int) -> None:
        try:
            self.bot.db.delete_quote(quote_id)
        except KeyError:
            await _reply(interaction, f"Quote #{quote_id} is not in the store.")
        else:
            await _reply(interaction, f"Quote #{quote_id} has been deleted.")


class QuoteBot(discord.Client):
    """Discord client managing QuoteBot slash commands and context menus."""

    def __init__(self, db_path: str | Path = "quotes.sql", intents: discord.Intents | None = None) -> None:
        self.db = QuoteDB(db_path)
        super().__init__(
            intents=intents if intents is not None else discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.tree = QuoteTree(self)
        self.quote_group = QuoteGroup(self)
        self.tree.add_command(self.quote_group)
        self.tree.add_command(app_commands.ContextMenu(name="Add Quote", callback=self.quote_message_context))

    async def setup_hook(self) -> None:
        await self.tree.sync()

    async def on_ready(self) -> None:
        print("Logged in as ", self.user)

    def is_owner(self, user_id: int) -> bool:
        app = self.application
        if app is None:
            return False
        if app.team is not None:
            return any(m.id == user_id for m in app.team.members)
        return app.owner.id == user_id

    def has_permission(self, interaction: discord.Interaction, perm: str) -> bool:
        return self.is_owner(interaction.user.id) or bool(getattr(interaction.permissions, perm, False))

    @_require_perm("send_messages")
    async def quote_message_context(self, interaction: discord.Interaction, message: discord.Message) -> None:
        text = message.content.strip() if message.content and message.content.strip() else None
        images = [a for a in message.attachments if is_image_attachment(a)]
        if text is None and not images:
            await _reply(interaction, "That message has no text or supported images to quote.", ephemeral=True)
            return
        safety = Safety.nsfw if images and is_channel_nsfw(interaction.channel) else Safety.sfw
        await self.save_and_reply(interaction, text=text, attachments=images, safety=safety)

    async def save_and_reply(
        self,
        interaction: discord.Interaction,
        *,
        text: str | None,
        attachments: list[discord.Attachment],
        safety: Safety,
    ) -> None:
        timestamp = time.time()
        author = interaction.user.display_name
        if not attachments:
            qid = self.db.add_quote(text, safety, author, timestamp)
            await _reply(interaction, f"Added #{qid} to the store.")
            return

        await interaction.response.defer()
        lines: list[str] = []
        for att in attachments:
            try:
                qid = self.db.add_quote(text, safety, author, timestamp, image=(att.filename, await att.read()))
            except discord.DiscordException:
                lines.append(f"There was an error saving {att.url}.")
            else:
                lines.append(f"Added quote #{qid} to the store with image <{att.url}>.")
        await _reply(interaction, "\n".join(lines)[:DISCORD_MESSAGE_LIMIT])

    async def post_quote(self, interaction: discord.Interaction, quote_id: int) -> None:
        try:
            quote = self.db.get_quote(quote_id)
        except KeyError:
            await _reply(interaction, f"Quote #{quote_id} is not in the store.")
            return

        if quote.is_nsfw_image and not is_channel_nsfw(interaction.channel):
            await _reply(interaction, f"NSFW images are not permitted in this channel, quote #{quote_id} was not posted.")
            return

        embed = discord.Embed(
            description=quote.message[:4096] if quote.message else None,
            timestamp=datetime.fromtimestamp(quote.timestamp, tz=timezone.utc),
        )
        embed.set_footer(text=f"#{quote.quoteid} • added by {quote.author}")

        if quote.has_image and quote.image_data is not None:
            ext = Path(quote.image_name or "image.png").suffix.lower() or ".png"
            filename = f"quote_{quote.quoteid}{ext}"
            embed.set_image(url=f"attachment://{filename}")
            with closing(discord.File(io.BytesIO(quote.image_data), filename=filename)) as discord_file:
                await _reply(interaction, embed=embed, file=discord_file)
        else:
            await _reply(interaction, embed=embed)


def main() -> None:
    load_dotenv()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable is required.")
    QuoteBot(os.environ.get("QUOTE_DB", "quotes.sql")).run(token)


if __name__ == "__main__":
    main()
