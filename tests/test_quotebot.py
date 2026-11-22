from datetime import timezone
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
import quotebot  # noqa: E402


class FakeAttachment:
    def __init__(
        self,
        filename: str,
        data: bytes = b"\x89PNG\r\n\x1a\nDATA",
        *,
        fail: bool = False,
    ) -> None:
        self.filename = filename
        self.url = f"https://cdn.discordapp.com/attachments/1/2/{filename}?ex=abc"
        self._data = data
        self._fail = fail

    async def read(self) -> bytes:
        if self._fail:
            resp = SimpleNamespace(status=404, reason="Not Found")
            raise discord.HTTPException(resp, "Failed")
        return self._data


class FakeChannel:
    def __init__(self, *, nsfw: bool = False) -> None:
        self._nsfw = nsfw

    def is_nsfw(self) -> bool:
        return self._nsfw


class FakeDMChannel:
    """Mimics discord.DMChannel, which has no is_nsfw() method."""


class FakeUser:
    def __init__(self, user_id: int, display_name: str = "Alice") -> None:
        self.id = user_id
        self.display_name = display_name


class FakeResponse:
    def __init__(self, sink: list[dict]) -> None:
        self._done = False
        self._sink = sink

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        assert not self._done
        self._done = True

    async def send_message(self, content: str | None = None, **kwargs) -> None:
        assert not self._done
        self._done = True
        if content is not None:
            assert len(content) <= quotebot.DISCORD_MESSAGE_LIMIT
        self._sink.append(
            {
                "content": content,
                "embed": kwargs.get("embed"),
                "file": kwargs.get("file"),
                "ephemeral": kwargs.get("ephemeral", False),
            }
        )


class FakeFollowup:
    def __init__(self, sink: list[dict]) -> None:
        self._sink = sink

    async def send(self, content: str | None = None, **kwargs) -> None:
        if content is not None:
            assert len(content) <= quotebot.DISCORD_MESSAGE_LIMIT
        self._sink.append(
            {
                "content": content,
                "embed": kwargs.get("embed"),
                "file": kwargs.get("file"),
                "ephemeral": kwargs.get("ephemeral", False),
            }
        )


class FakeInteraction:
    def __init__(
        self,
        *,
        user: FakeUser,
        channel: object,
        guild_id: int | None = 1,
        permissions: discord.Permissions | None = None,
    ) -> None:
        self.user = user
        self.channel = channel
        self.guild_id = guild_id
        self.permissions = (
            permissions
            if permissions is not None
            else discord.Permissions(send_messages=True)
        )
        self.sent: list[dict] = []
        self.response = FakeResponse(self.sent)
        self.followup = FakeFollowup(self.sent)


class QuoteBotSimplifiedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "quotes.sql"
        self.owner_id = 999
        self.bot = quotebot.QuoteBot(db_path=self.db_path)
        self.bot._application = SimpleNamespace(
            owner=SimpleNamespace(id=self.owner_id), team=None
        )
        self.group = self.bot.quote_group

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        self.bot.db.close()
        self.tmpdir.cleanup()

    async def test_commands_native_enum_choices_and_unprivileged_intents(self) -> None:
        """Consolidated subcommands register native Safety IntEnum choices and use default intents."""
        self.assertFalse(self.bot.intents.message_content)
        self.assertEqual(
            {c.name for c in self.group.commands},
            {"add", "get", "random", "find", "set", "del"},
        )
        safety_param = next(p for p in self.group.add.parameters if p.name == "safety")
        self.assertEqual([(c.name, c.value) for c in safety_param.choices], [("sfw", 0), ("nsfw", 1)])

    async def test_sqlite_blob_storage_and_embed_rendering(self) -> None:
        """Images store directly inside SQLite BLOBs and post as discord.Embed + in-memory File."""
        chan = FakeChannel(nsfw=False)
        user = FakeUser(10, "Alice")

        inter_add = FakeInteraction(user=user, channel=chan)
        await self.group.add.callback(
            self.group,
            inter_add,
            quote="```code inside quote``` **bold**",
            image=FakeAttachment("my screenshot (1).png", b"PNG_BYTES"),
            safety=None,
        )
        q = self.bot.db.get_quote(1)
        self.assertIsInstance(q, quotebot.Quote)
        self.assertEqual(q.image_data, b"PNG_BYTES")
        self.assertEqual(q.safety, quotebot.Safety.sfw)

        inter_get = FakeInteraction(user=user, channel=chan)
        await self.group.get.callback(self.group, inter_get, quote_id=1)
        reply = inter_get.sent[-1]
        embed: discord.Embed = reply["embed"]
        self.assertIsNotNone(embed)
        self.assertIn("```code inside quote```", embed.description)
        self.assertEqual(embed.footer.text, "#1 • added by Alice")
        self.assertEqual(embed.timestamp.tzinfo, timezone.utc)
        self.assertEqual(embed.image.url, "attachment://quote_1.png")
        self.assertIsNotNone(reply["file"])

        # Failed attachment download does not insert any DB row
        inter_fail = FakeInteraction(user=user, channel=chan)
        await self.group.add.callback(
            self.group,
            inter_fail,
            quote="broken",
            image=FakeAttachment("bad.png", fail=True),
            safety=None,
        )
        self.assertEqual(self.bot.db.quote_count(), 1)
        self.assertIn("There was an error saving", inter_fail.sent[-1]["content"])

    async def test_channel_safety_and_nsfw_image_blocking(self) -> None:
        """Untagged images inherit channel safety; SFW channels block NSFW images while allowing NSFW text."""
        sfw_chan = FakeChannel(nsfw=False)
        nsfw_chan = FakeChannel(nsfw=True)
        user = FakeUser(10, "Bob")

        # #1: untagged image in NSFW channel -> Safety.nsfw
        await self.group.add.callback(
            self.group,
            FakeInteraction(user=user, channel=nsfw_chan),
            quote=None,
            image=FakeAttachment("nsfw.png", b"NSFW_BYTES"),
            safety=None,
        )
        # #2: explicit NSFW text quote
        await self.group.add.callback(
            self.group,
            FakeInteraction(user=user, channel=nsfw_chan),
            quote="spicy text",
            image=None,
            safety=quotebot.Safety.nsfw,
        )

        # #1 blocked in SFW channel, allowed in NSFW channel
        sfw_img = FakeInteraction(user=user, channel=sfw_chan)
        await self.group.get.callback(self.group, sfw_img, quote_id=1)
        self.assertEqual(
            sfw_img.sent[-1]["content"],
            "NSFW images are not permitted in this channel, quote #1 was not posted.",
        )

        nsfw_img = FakeInteraction(user=user, channel=nsfw_chan)
        await self.group.get.callback(self.group, nsfw_img, quote_id=1)
        self.assertIsNotNone(nsfw_img.sent[-1]["embed"])

        # #2 (NSFW text) allowed in SFW channel
        sfw_txt = FakeInteraction(user=user, channel=sfw_chan)
        await self.group.get.callback(self.group, sfw_txt, quote_id=2)
        self.assertEqual(sfw_txt.sent[-1]["embed"].description, "spicy text")

    async def test_consolidated_get_find_set_del_and_random(self) -> None:
        """Covers /quote get (most recent when omitted), /quote find (count, query, author, combined), set, del, random."""
        sfw_chan = FakeChannel(nsfw=False)
        mod_perms = discord.Permissions(send_messages=True, kick_members=True)
        mod = FakeUser(10, "dark_knight")

        # Empty /quote get (most recent) & /quote find (total count)
        empty_get = FakeInteraction(user=mod, channel=sfw_chan)
        await self.group.get.callback(self.group, empty_get, quote_id=None)
        self.assertIn("No quotes in the store", empty_get.sent[-1]["content"])

        count0 = FakeInteraction(user=mod, channel=sfw_chan)
        await self.group.find.callback(self.group, count0, query=None, author=None)
        self.assertEqual(count0.sent[-1]["content"], "0 quotes in the store.")

        self.bot.db.add_quote("100% match", quotebot.Safety.sfw, "dark_knight", 1700000000.0)
        self.bot.db.add_quote("100% other", quotebot.Safety.sfw, "darkXknight", 1700000005.0)
        self.bot.db.add_quote("X" * 5000, quotebot.Safety.sfw, "dark_knight", 1700000010.0)

        # Combined query + author filter with escaped '%' and '_'
        combined = FakeInteraction(user=mod, channel=sfw_chan)
        await self.group.find.callback(self.group, combined, query="100%", author="dark_knight")
        self.assertEqual(combined.sent[-1]["embed"].description, "100% match")

        # /quote get without quote_id returns #3, truncated cleanly to 4096 embed description limit
        latest = FakeInteraction(user=mod, channel=sfw_chan)
        await self.group.get.callback(self.group, latest, quote_id=None)
        self.assertEqual(len(latest.sent[-1]["embed"].description), 4096)

        # /quote set and /quote del
        set_ok = FakeInteraction(user=mod, channel=sfw_chan, permissions=mod_perms)
        await self.group.set_safety.callback(self.group, set_ok, quote_id=1, safety=quotebot.Safety.nsfw)
        self.assertEqual(self.bot.db.get_quote(1).safety, quotebot.Safety.nsfw)

        del_ok = FakeInteraction(user=mod, channel=sfw_chan, permissions=mod_perms)
        await self.group.delete.callback(self.group, del_ok, quote_id=1)
        self.assertEqual(self.bot.db.quote_count(), 2)

    async def test_application_owner_team_dm_and_env_config(self) -> None:
        """Auto-detects individual and team application owners, restricts DMs, and checks DISCORD_TOKEN."""
        dm = FakeDMChannel()
        stranger = FakeInteraction(user=FakeUser(50), channel=dm, guild_id=None)
        self.assertFalse(await self.bot.tree.interaction_check(stranger))

        owner = FakeInteraction(
            user=FakeUser(self.owner_id, "Owner"),
            channel=dm,
            guild_id=None,
            permissions=discord.Permissions.none(),
        )
        self.assertTrue(await self.bot.tree.interaction_check(owner))
        self.assertTrue(self.bot.has_permission(owner, "kick_members"))

        # Team-owned application support
        self.bot._application = SimpleNamespace(
            owner=None, team=SimpleNamespace(members=[SimpleNamespace(id=777)])
        )
        self.assertTrue(self.bot.is_owner(777))
        self.assertFalse(self.bot.is_owner(self.owner_id))

        # Missing DISCORD_TOKEN exits cleanly
        with patch.dict(os.environ, {}, clear=True), patch("quotebot.load_dotenv"):
            with self.assertRaises(SystemExit):
                quotebot.main()


if __name__ == "__main__":
    unittest.main()
