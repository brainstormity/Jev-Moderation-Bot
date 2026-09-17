"""Unit tests for the database layer (database.py)."""

from __future__ import annotations

import os
import pytest
import pytest_asyncio
from database import Database, GuildSettings

TEST_DB_PATH = "test_bot_data.db"


@pytest_asyncio.fixture
async def db():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    database = Database(db_path=TEST_DB_PATH)
    await database.init_db()
    yield database
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)


@pytest.mark.asyncio
async def test_guild_settings_defaults_and_updates(db: Database):
    # Unconfigured guild gets defaults
    settings = await db.get_guild_settings(guild_id=123)
    assert settings.guild_id == 123
    assert settings.mod_log_channel_id is None
    assert settings.tier1_threshold == 0.95
    assert settings.tier2_threshold == 0.70
    assert settings.first_timeout_minutes == 10
    assert settings.subsequent_timeout_minutes == 60

    # Set mod log channel
    await db.set_mod_log_channel(guild_id=123, channel_id=987654)
    updated = await db.get_guild_settings(guild_id=123)
    assert updated.mod_log_channel_id == 987654

    # Update timeouts
    await db.set_guild_timeouts(guild_id=123, first_mins=15, subsequent_mins=120)
    updated = await db.get_guild_settings(guild_id=123)
    assert updated.first_timeout_minutes == 15
    assert updated.subsequent_timeout_minutes == 120

    # Update thresholds
    await db.set_guild_thresholds(guild_id=123, tier1=0.90, tier2=0.65)
    updated = await db.get_guild_settings(guild_id=123)
    assert updated.tier1_threshold == 0.90
    assert updated.tier2_threshold == 0.65

    # Unset channel
    await db.set_mod_log_channel(guild_id=123, channel_id=None)
    updated = await db.get_guild_settings(guild_id=123)
    assert updated.mod_log_channel_id is None


@pytest.mark.asyncio
async def test_offense_recording_and_progressive_counts(db: Database):
    guild_id = 456
    user_id = 789

    # Initially 0 active offenses
    count = await db.get_active_offense_count(guild_id, user_id)
    assert count == 0

    # Record 1st offense
    off1, active1 = await db.record_offense(
        guild_id=guild_id,
        user_id=user_id,
        channel_id=111,
        message_content="free nitro click here http://scam.ru",
        state_summary="state context 1",
        classification="SCAM_LINK",
        confidence=0.98,
        noul=0.97,
        action_taken="WARN_1_DM",
    )
    assert off1 > 0
    assert active1 == 1

    # Record 2nd offense
    off2, active2 = await db.record_offense(
        guild_id=guild_id,
        user_id=user_id,
        channel_id=111,
        message_content="second spam message",
        state_summary="state context 2",
        classification="SPAM",
        confidence=0.88,
        noul=0.20,
        action_taken="WARN_2_DM",
    )
    assert off2 > off1
    assert active2 == 2

    # Verify history retrieval
    history = await db.get_user_offenses(guild_id, user_id, limit=5)
    assert len(history) == 2
    assert history[0]["id"] == off2
    assert history[0]["message_content"] == "second spam message"
    assert history[0]["action_taken"] == "WARN_2_DM"
    assert history[0]["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_pardon_and_dynamic_false_flags(db: Database):
    guild_id = 100
    user_id = 200

    # Record an offense that is actually a false flag
    off_id, _ = await db.record_offense(
        guild_id=guild_id,
        user_id=user_id,
        channel_id=300,
        message_content="join my twitch stream guys https://twitch.tv/safe",
        state_summary="state...",
        classification="SCAM_LINK",
        confidence=0.96,
        noul=0.95,
        action_taken="WARN_1_DM",
    )

    # Active count is 1
    assert await db.get_active_offense_count(guild_id, user_id) == 1

    # Pardon the offense
    success = await db.pardon_offense(offense_id=off_id, moderator_id=999, notes="harmless twitch link")
    assert success is True

    # Active count drops to 0
    assert await db.get_active_offense_count(guild_id, user_id) == 0

    # Status updated in user_offenses
    history = await db.get_user_offenses(guild_id, user_id)
    assert history[0]["status"] == "PARDONED"

    # Precedent added to dynamic false flags for Jev AI in-context memory
    false_flags = await db.get_recent_false_flags(guild_id)
    assert len(false_flags) == 1
    assert "twitch.tv/safe" in false_flags[0]

    # Feedback records
    feedback = await db.get_feedback_records(guild_id)
    assert len(feedback) == 1
    assert feedback[0]["feedback_type"] == "FALSE_FLAG"
    assert feedback[0]["moderator_id"] == 999


@pytest.mark.asyncio
async def test_escalate_to_ban(db: Database):
    guild_id = 500
    user_id = 600

    off_id, _ = await db.record_offense(
        guild_id=guild_id,
        user_id=user_id,
        channel_id=300,
        message_content="dangerous phishing site",
        state_summary="state...",
        classification="SCAM_LINK",
        confidence=0.99,
        noul=0.99,
        action_taken="TIMEOUT_10M",
    )

    success = await db.escalate_offense_to_ban(offense_id=off_id, moderator_id=888)
    assert success is True

    history = await db.get_user_offenses(guild_id, user_id)
    assert history[0]["status"] == "BANNED"

    feedback = await db.get_feedback_records(guild_id)
    assert feedback[0]["feedback_type"] == "CONFIRMED_THREAT"
