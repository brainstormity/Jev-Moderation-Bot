"""Unit tests for bot slash commands and permission handling."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio

from database import Database, db_instance
from main import (
    export_feedback,
    mod_config,
    pardon_user,
    set_mod_log,
    set_thresholds,
    set_timeouts,
    unset_mod_log,
    user_offenses,
)

TEST_CMD_DB = "test_commands_data.db"


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db():
    if os.path.exists(TEST_CMD_DB):
        os.remove(TEST_CMD_DB)
    test_db = Database(db_path=TEST_CMD_DB)
    await test_db.init_db()

    with patch("main.db_instance", test_db):
        yield test_db

    if os.path.exists(TEST_CMD_DB):
        os.remove(TEST_CMD_DB)


def make_mock_interaction(guild_id: int = 123, is_admin: bool = True):
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    interaction.guild = AsyncMock(spec=discord.Guild)
    interaction.guild.id = guild_id
    interaction.guild.name = "Command Test Guild"

    interaction.user = AsyncMock(spec=discord.Member)
    interaction.user.id = 777
    interaction.user.guild_permissions.administrator = is_admin
    interaction.user.guild_permissions.moderate_members = is_admin

    interaction.response = AsyncMock()
    interaction.response.send_message = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_set_mod_log_permissions_check(setup_test_db: Database):
    interaction = make_mock_interaction()
    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 999
    channel.mention = "<#999>"

    # Bot missing 'Send Messages'
    perms = MagicMock(spec=discord.Permissions)
    perms.view_channel = True
    perms.send_messages = False
    perms.embed_links = True
    channel.permissions_for = MagicMock(return_value=perms)

    bot_member = MagicMock(spec=discord.Member)
    interaction.guild.me = bot_member

    await set_mod_log.callback(interaction, channel)
    interaction.response.send_message.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "missing required permissions" in args[0]
    assert "Send Messages" in args[0]

    # Now grant all permissions
    perms.send_messages = True
    interaction.response.send_message.reset_mock()

    await set_mod_log.callback(interaction, channel)
    interaction.response.send_message.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "successfully set to <#999>" in args[0]

    settings = await setup_test_db.get_guild_settings(interaction.guild_id)
    assert settings.mod_log_channel_id == 999


@pytest.mark.asyncio
async def test_unset_mod_log(setup_test_db: Database):
    interaction = make_mock_interaction()
    await setup_test_db.set_mod_log_channel(interaction.guild_id, 999)

    await unset_mod_log.callback(interaction)
    interaction.response.send_message.assert_called_once()

    settings = await setup_test_db.get_guild_settings(interaction.guild_id)
    assert settings.mod_log_channel_id is None


@pytest.mark.asyncio
async def test_set_timeouts_command(setup_test_db: Database):
    interaction = make_mock_interaction()

    # Invalid negative
    await set_timeouts.callback(interaction, -5, 60)
    args, _ = interaction.response.send_message.call_args
    assert "must be positive integers" in args[0]

    # Valid update
    interaction.response.send_message.reset_mock()
    await set_timeouts.callback(interaction, 15, 120)
    args, _ = interaction.response.send_message.call_args
    assert "15" in args[0]
    assert "120" in args[0]

    settings = await setup_test_db.get_guild_settings(interaction.guild_id)
    assert settings.first_timeout_minutes == 15
    assert settings.subsequent_timeout_minutes == 120


@pytest.mark.asyncio
async def test_set_thresholds_command(setup_test_db: Database):
    interaction = make_mock_interaction()

    # Invalid range: tier2 >= tier1
    await set_thresholds.callback(interaction, 0.70, 0.80)
    args, _ = interaction.response.send_message.call_args
    assert "Thresholds must satisfy" in args[0]

    # Valid update
    interaction.response.send_message.reset_mock()
    await set_thresholds.callback(interaction, 0.92, 0.65)
    args, _ = interaction.response.send_message.call_args
    assert "0.92" in args[0]
    assert "0.65" in args[0]

    settings = await setup_test_db.get_guild_settings(interaction.guild_id)
    assert settings.tier1_threshold == 0.92
    assert settings.tier2_threshold == 0.65


@pytest.mark.asyncio
async def test_user_offenses_command(setup_test_db: Database):
    interaction = make_mock_interaction()
    target_user = AsyncMock(spec=discord.Member)
    target_user.id = 555
    target_user.mention = "<@555>"
    target_user.display_name = "BadActor"

    # User with no offenses
    await user_offenses.callback(interaction, target_user)
    args, _ = interaction.response.send_message.call_args
    assert "no recorded moderation offenses" in args[0]

    # User with an offense
    await setup_test_db.record_offense(
        guild_id=interaction.guild_id,
        user_id=555,
        channel_id=111,
        message_content="phishing link http://evil.com",
        state_summary="state",
        classification="SCAM_LINK",
        confidence=0.98,
        noul=0.97,
        action_taken="WARN_1_DM",
    )

    interaction.response.send_message.reset_mock()
    await user_offenses.callback(interaction, target_user)
    kwargs = interaction.response.send_message.call_args[1]
    embed = kwargs.get("embed")
    assert embed is not None
    assert "Infraction History — BadActor" in embed.title
    assert len(embed.fields) == 1
    assert "phishing link" in embed.fields[0].value


@pytest.mark.asyncio
async def test_pardon_command(setup_test_db: Database):
    interaction = make_mock_interaction()
    target_user = AsyncMock(spec=discord.Member)
    target_user.id = 444
    target_user.mention = "<@444>"
    target_user.is_timed_out = MagicMock(return_value=True)
    target_user.timeout = AsyncMock()

    # User with no active offenses
    await pardon_user.callback(interaction, target_user)
    args, _ = interaction.response.send_message.call_args
    assert "does not have any active infractions" in args[0]

    # Add active offense
    off_id, _ = await setup_test_db.record_offense(
        guild_id=interaction.guild_id,
        user_id=444,
        channel_id=111,
        message_content="harmless link http://good.com",
        state_summary="state",
        classification="SCAM_LINK",
        confidence=0.95,
        noul=0.90,
        action_taken="TIMEOUT_10M",
    )

    interaction.response.send_message.reset_mock()
    await pardon_user.callback(interaction, target_user)
    args, _ = interaction.response.send_message.call_args
    assert f"Pardoned Offense #{off_id}" in args[0]
    target_user.timeout.assert_called_once_with(None, reason=pytest.approx(None, abs=1) if False else unittest_any())


@pytest.mark.asyncio
async def test_export_feedback_command(setup_test_db: Database):
    interaction = make_mock_interaction()

    # Empty feedback
    await export_feedback.callback(interaction, file_format="json")
    args, _ = interaction.response.send_message.call_args
    assert "No feedback records found" in args[0]

    # Add an offense and pardon it to create feedback
    off_id, _ = await setup_test_db.record_offense(
        guild_id=interaction.guild_id,
        user_id=333,
        channel_id=111,
        message_content="sample false flag",
        state_summary="state",
        classification="SCAM_LINK",
        confidence=0.96,
        noul=0.95,
        action_taken="WARN_1_DM",
    )
    await setup_test_db.pardon_offense(off_id, moderator_id=777)

    # Export as JSON
    interaction.response.send_message.reset_mock()
    await export_feedback.callback(interaction, file_format="json")
    kwargs = interaction.response.send_message.call_args[1]
    file = kwargs.get("file")
    assert file is not None
    assert file.filename.endswith(".json")

    # Export as CSV
    interaction.response.send_message.reset_mock()
    await export_feedback.callback(interaction, file_format="csv")
    kwargs = interaction.response.send_message.call_args[1]
    file = kwargs.get("file")
    assert file is not None
    assert file.filename.endswith(".csv")


@pytest.mark.asyncio
async def test_mod_config_command(setup_test_db: Database):
    interaction = make_mock_interaction()
    await mod_config.callback(interaction)
    kwargs = interaction.response.send_message.call_args[1]
    embed = kwargs.get("embed")
    assert embed is not None
    assert "Moderation Settings" in embed.title


class unittest_any:
    def __eq__(self, other):
        return True
