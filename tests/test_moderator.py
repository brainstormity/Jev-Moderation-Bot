"""Unit tests for moderation logic, progressive timeouts, and admin-only interactive views."""

from __future__ import annotations

import datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio

from database import Database, GuildSettings
from moderator import (
    BanConfirmView,
    MessageModerator,
    ModLogActionView,
    PardonConfirmView,
    build_state_summary,
    send_user_dm,
)
from typesafe import AsyncTypeSafe, QuestionResult, TypeSafeEvaluationResponse

TEST_MOD_DB = "test_moderator_data.db"


@pytest_asyncio.fixture
async def db():
    if os.path.exists(TEST_MOD_DB):
        os.remove(TEST_MOD_DB)
    database = Database(db_path=TEST_MOD_DB)
    await database.init_db()
    yield database
    if os.path.exists(TEST_MOD_DB):
        os.remove(TEST_MOD_DB)


def make_mock_message(
    guild_id: int = 100,
    author_id: int = 200,
    is_bot: bool = False,
    content: str = "Hello everyone",
    account_days: int = 45,
    has_guild: bool = True,
):
    msg = AsyncMock(spec=discord.Message)
    msg.id = 123456
    msg.content = content

    # Author
    author = AsyncMock(spec=discord.Member)
    author.id = author_id
    author.name = "TestUser"
    author.bot = is_bot
    author.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=account_days)
    author.timeout = AsyncMock()
    author.send = AsyncMock()
    author.is_timed_out = MagicMock(return_value=False)
    msg.author = author

    # Channel
    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 300
    channel.name = "general"
    channel.mention = "<#300>"
    msg.channel = channel

    # Guild
    if has_guild:
        guild = AsyncMock(spec=discord.Guild)
        guild.id = guild_id
        guild.name = "Test Server"
        guild.ban = AsyncMock()
        guild.get_member = MagicMock(return_value=author)

        # Bot member inside guild
        bot_member = MagicMock(spec=discord.Member)
        perms = MagicMock(spec=discord.Permissions)
        perms.view_channel = True
        perms.send_messages = True
        perms.embed_links = True
        channel.permissions_for = MagicMock(return_value=perms)
        guild.me = bot_member

        msg.guild = guild
    else:
        msg.guild = None

    msg.delete = AsyncMock()
    return msg


def make_typesafe_response(choice: str, confidence: float, noul: float):
    q_spam = QuestionResult("spam_classification", "choice", {"choice": choice, "confidence": confidence})
    q_ban = QuestionResult("requires_immediate_ban", "noul", {"noul": noul})
    return TypeSafeEvaluationResponse(
        model="jev-latest",
        questions={"spam_classification": q_spam, "requires_immediate_ban": q_ban},
    )


# ---------------------------------------------------------------------------
# State Summary & DM Tests
# ---------------------------------------------------------------------------

def test_build_state_summary():
    msg = make_mock_message(content="Click http://free-nitro.ru right now!", account_days=20)
    summary = build_state_summary(msg, recent_false_flags=["twitch.tv/mychannel"])

    assert "Author ID: 200" in summary
    assert "Account Age (Days): 20" in summary
    assert "External Link Indicator: True" in summary
    assert "Channel: #general (ID: 300)" in summary
    assert "twitch.tv/mychannel" in summary
    assert "Click http://free-nitro.ru right now!" in summary


@pytest.mark.asyncio
async def test_send_user_dm():
    member = AsyncMock(spec=discord.Member)
    embed = discord.Embed(title="Warning")

    # Success case
    member.send = AsyncMock()
    success = await send_user_dm(member, embed)
    assert success is True
    member.send.assert_called_once_with(embed=embed)

    # Closed DMs (Forbidden)
    member.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "DMs disabled"))
    failed = await send_user_dm(member, embed)
    assert failed is False


# ---------------------------------------------------------------------------
# Moderation Escalation Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ignore_bots_and_dms(db: Database):
    client = AsyncMock(spec=AsyncTypeSafe)
    moderator = MessageModerator(client=client, db=db)

    # Bot message
    bot_msg = make_mock_message(is_bot=True)
    assert await moderator.handle_message(bot_msg) is False
    client.evaluate.assert_not_called()

    # DM message (no guild)
    dm_msg = make_mock_message(has_guild=False)
    assert await moderator.handle_message(dm_msg) is False
    client.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_tier3_legitimate_message_allowed(db: Database):
    client = AsyncMock(spec=AsyncTypeSafe)
    moderator = MessageModerator(client=client, db=db)

    msg = make_mock_message(content="What time is the team meeting?")
    client.evaluate = AsyncMock(return_value=make_typesafe_response("LEGITIMATE", 0.99, 0.01))

    was_moderated = await moderator.handle_message(msg)
    assert was_moderated is False
    msg.delete.assert_not_called()
    msg.author.timeout.assert_not_called()


@pytest.mark.asyncio
async def test_4_stage_progressive_escalation(db: Database):
    client = AsyncMock(spec=AsyncTypeSafe)
    client.model = "jev-latest"
    moderator = MessageModerator(client=client, db=db)

    guild_id = 100
    author_id = 200

    # Configure mod-log channel
    mod_channel = AsyncMock(spec=discord.TextChannel)
    mod_channel.id = 999
    mod_channel.send = AsyncMock()
    perms = MagicMock(spec=discord.Permissions)
    perms.view_channel = True
    perms.send_messages = True
    perms.embed_links = True
    mod_channel.permissions_for = MagicMock(return_value=perms)

    await db.set_mod_log_channel(guild_id, 999)

    # -----------------------------------------------------------------------
    # 1st Offense: Warning 1 DM (No timeout)
    # -----------------------------------------------------------------------
    msg1 = make_mock_message(guild_id=guild_id, author_id=author_id, content="steam giftcard free http://scam.link")
    msg1.guild.get_channel = MagicMock(return_value=mod_channel)
    client.evaluate = AsyncMock(return_value=make_typesafe_response("SCAM_LINK", 0.98, 0.97))

    res1 = await moderator.handle_message(msg1)
    assert res1 is True
    msg1.delete.assert_called_once()
    msg1.author.timeout.assert_not_called()  # No timeout on 1st offense!
    msg1.author.send.assert_called_once()    # DM sent!
    assert await db.get_active_offense_count(guild_id, author_id) == 1

    # Mod log received alert
    mod_channel.send.assert_called_once()

    # -----------------------------------------------------------------------
    # 2nd Offense: Final Warning DM (No timeout)
    # -----------------------------------------------------------------------
    msg2 = make_mock_message(guild_id=guild_id, author_id=author_id, content="another scam link http://fake.com")
    msg2.guild.get_channel = MagicMock(return_value=mod_channel)
    client.evaluate = AsyncMock(return_value=make_typesafe_response("SCAM_LINK", 0.98, 0.97))

    res2 = await moderator.handle_message(msg2)
    assert res2 is True
    msg2.delete.assert_called_once()
    msg2.author.timeout.assert_not_called()  # No timeout on 2nd offense!
    msg2.author.send.assert_called_once()    # DM sent!
    assert await db.get_active_offense_count(guild_id, author_id) == 2

    # -----------------------------------------------------------------------
    # 3rd Offense: 10-Minute Timeout Applied
    # -----------------------------------------------------------------------
    msg3 = make_mock_message(guild_id=guild_id, author_id=author_id, content="third scam message http://bad.com")
    msg3.guild.get_channel = MagicMock(return_value=mod_channel)
    client.evaluate = AsyncMock(return_value=make_typesafe_response("SCAM_LINK", 0.99, 0.98))

    res3 = await moderator.handle_message(msg3)
    assert res3 is True
    msg3.delete.assert_called_once()
    msg3.author.timeout.assert_called_once()  # 10m timeout applied!
    assert await db.get_active_offense_count(guild_id, author_id) == 3

    # -----------------------------------------------------------------------
    # 4th Offense: 60-Minute Extended Timeout Applied
    # -----------------------------------------------------------------------
    msg4 = make_mock_message(guild_id=guild_id, author_id=author_id, content="fourth scam message http://worst.com")
    msg4.guild.get_channel = MagicMock(return_value=mod_channel)
    client.evaluate = AsyncMock(return_value=make_typesafe_response("SCAM_LINK", 0.99, 0.98))

    res4 = await moderator.handle_message(msg4)
    assert res4 is True
    msg4.delete.assert_called_once()
    msg4.author.timeout.assert_called_once()  # 60m timeout applied!
    assert await db.get_active_offense_count(guild_id, author_id) == 4


@pytest.mark.asyncio
async def test_api_error_recovery(db: Database):
    client = AsyncMock(spec=AsyncTypeSafe)
    client.evaluate = AsyncMock(side_effect=RuntimeError("TypeSafe API timeout"))
    moderator = MessageModerator(client=client, db=db)

    msg = make_mock_message(content="Potential spam")
    was_moderated = await moderator.handle_message(msg)

    # Default to allow message through without breaking bot
    assert was_moderated is False
    msg.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Interactive Views & Ephemeral Confirmations Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mod_log_action_view_admin_check(db: Database):
    view = ModLogActionView(offense_id=1, user_id=200, guild_id=100, db=db)

    # Non-admin clicks Pardon
    non_admin_interaction = AsyncMock(spec=discord.Interaction)
    non_admin_interaction.user.guild_permissions.administrator = False
    non_admin_interaction.response.send_message = AsyncMock()

    await view.pardon_button.callback(non_admin_interaction)
    non_admin_interaction.response.send_message.assert_called_once()
    args, kwargs = non_admin_interaction.response.send_message.call_args
    assert "Only server administrators" in args[0]
    assert kwargs.get("ephemeral") is True

    # Admin clicks Pardon -> receives ephemeral confirmation view
    admin_interaction = AsyncMock(spec=discord.Interaction)
    admin_interaction.user.guild_permissions.administrator = True
    admin_interaction.response.send_message = AsyncMock()

    await view.pardon_button.callback(admin_interaction)
    admin_interaction.response.send_message.assert_called_once()
    args, kwargs = admin_interaction.response.send_message.call_args
    assert "Confirm False Flag Pardon" in kwargs.get("content", "")
    assert isinstance(kwargs.get("view"), PardonConfirmView)


@pytest.mark.asyncio
async def test_pardon_confirm_view_execution(db: Database):
    guild_id = 100
    user_id = 200

    # Record active offense in DB
    off_id, _ = await db.record_offense(
        guild_id=guild_id,
        user_id=user_id,
        channel_id=300,
        message_content="harmless link https://github.com/myproject",
        state_summary="context",
        classification="SCAM_LINK",
        confidence=0.96,
        noul=0.95,
        action_taken="TIMEOUT_10M",
    )

    parent_msg = AsyncMock(spec=discord.Message)
    embed = discord.Embed(title="Alert")
    parent_msg.embeds = [embed]
    parent_msg.edit = AsyncMock()

    confirm_view = PardonConfirmView(
        offense_id=off_id,
        user_id=user_id,
        guild_id=guild_id,
        parent_message=parent_msg,
        db=db,
    )

    # Admin confirms pardon
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user.guild_permissions.administrator = True
    interaction.user.id = 888
    interaction.user.mention = "<@888>"
    interaction.response.edit_message = AsyncMock()

    member = AsyncMock(spec=discord.Member)
    member.timeout = AsyncMock()
    interaction.guild.get_member = MagicMock(return_value=member)

    await confirm_view.confirm_pardon.callback(interaction)

    # Timeout lifted
    member.timeout.assert_called_once_with(None, reason=pytest.approx(None, abs=1) if False else unittest_any())

    # DB offense marked PARDONED
    assert await db.get_active_offense_count(guild_id, user_id) == 0

    # False flag added to Jev AI runtime memory
    flags = await db.get_recent_false_flags(guild_id)
    assert len(flags) == 1
    assert "github.com/myproject" in flags[0]

    # Parent message updated
    parent_msg.edit.assert_called_once()
    edited_embed = parent_msg.edit.call_args[1]["embed"]
    assert "PARDONED AS FALSE FLAG" in edited_embed.title


@pytest.mark.asyncio
async def test_ban_confirm_view_execution(db: Database):
    guild_id = 100
    user_id = 300

    off_id, _ = await db.record_offense(
        guild_id=guild_id,
        user_id=user_id,
        channel_id=300,
        message_content="wallet drainer link",
        state_summary="context",
        classification="SCAM_LINK",
        confidence=0.99,
        noul=0.99,
        action_taken="TIMEOUT_10M",
    )

    parent_msg = AsyncMock(spec=discord.Message)
    embed = discord.Embed(title="Alert")
    parent_msg.embeds = [embed]
    parent_msg.edit = AsyncMock()

    ban_view = BanConfirmView(
        offense_id=off_id,
        user_id=user_id,
        guild_id=guild_id,
        parent_message=parent_msg,
        db=db,
    )

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user.guild_permissions.administrator = True
    interaction.user.id = 888
    interaction.user.mention = "<@888>"
    interaction.response.edit_message = AsyncMock()
    interaction.guild.ban = AsyncMock()

    await ban_view.confirm_ban.callback(interaction)

    # Ban executed on guild
    interaction.guild.ban.assert_called_once()

    # DB updated to BANNED
    history = await db.get_user_offenses(guild_id, user_id)
    assert history[0]["status"] == "BANNED"

    # Parent message updated
    parent_msg.edit.assert_called_once()


class unittest_any:
    def __eq__(self, other):
        return True
