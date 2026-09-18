"""Unit tests for user behavioral profiling, rolling cache, and channel history scraping."""

from __future__ import annotations

import datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio

from database import Database
from main import on_message, profile_user, profile_user_context
from profiler import (
    UserProfileData,
    build_profile_embed,
    build_profile_state,
    evaluate_user_profile,
    render_progress_bar,
)
from profile_views import (
    ChannelSelectFallbackView,
    ProfileReportView,
    SampledMessagesPaginationView,
    scrape_channel_history,
)
from typesafe import QuestionResult, TypeSafeEvaluationResponse

TEST_PROFILER_DB = "test_profiler_data.db"


@pytest_asyncio.fixture
async def db():
    if os.path.exists(TEST_PROFILER_DB):
        os.remove(TEST_PROFILER_DB)
    database = Database(db_path=TEST_PROFILER_DB)
    await database.init_db()
    yield database
    if os.path.exists(TEST_PROFILER_DB):
        os.remove(TEST_PROFILER_DB)


def make_mock_member(
    user_id: int = 12345,
    name: str = "Alice",
    account_days: int = 30,
    server_days: int = 10,
) -> discord.Member:
    now = datetime.datetime.now(datetime.timezone.utc)
    member = AsyncMock(spec=discord.Member)
    member.id = user_id
    member.name = name
    member.display_name = name
    member.mention = f"<@{user_id}>"
    member.created_at = now - datetime.timedelta(days=account_days)
    member.joined_at = now - datetime.timedelta(days=server_days)
    member.display_avatar = MagicMock()
    member.display_avatar.url = "https://example.com/avatar.png"

    role = MagicMock()
    role.name = "Member"
    member.roles = [role]
    return member


def make_mock_typesafe_response(
    scam: float = 0.05,
    spam: float = 0.1,
    noob: float = 0.8,
    toxic: float = 0.02,
    helpful: float = 0.7,
    persona: str = "BENIGN_NEWBIE",
    action: str = "WELCOME_GUIDE",
) -> TypeSafeEvaluationResponse:
    q_scam = QuestionResult("is_scammy", "noul", {"noul": scam})
    q_spam = QuestionResult("is_spammy", "noul", {"noul": spam})
    q_noob = QuestionResult("is_noob", "noul", {"noul": noob})
    q_toxic = QuestionResult("is_toxic", "noul", {"noul": toxic})
    q_helpful = QuestionResult("is_helpful", "noul", {"noul": helpful})
    q_persona = QuestionResult("primary_persona", "choice", {"choice": persona, "confidence": 0.88})
    q_action = QuestionResult("recommended_action", "choice", {"choice": action, "confidence": 0.92})

    return TypeSafeEvaluationResponse(
        model="jev-latest",
        questions={
            "is_scammy": q_scam,
            "is_spammy": q_spam,
            "is_noob": q_noob,
            "is_toxic": q_toxic,
            "is_helpful": q_helpful,
            "primary_persona": q_persona,
            "recommended_action": q_action,
        },
    )


# ==============================================================================
# Database Tests for Message Caching
# ==============================================================================

@pytest.mark.asyncio
async def test_database_save_user_message(db: Database):
    """Verify single user message is saved with timestamp and duplicates are ignored."""
    ts = "2026-09-17T20:00:00+00:00"
    await db.save_user_message(10, 20, 30, 1001, "Hello world", ts)

    count = await db.count_user_messages(10, 30)
    assert count == 1

    # Duplicate message_id should be ignored
    await db.save_user_message(10, 20, 30, 1001, "Hello world modified", ts)
    count_after = await db.count_user_messages(10, 30)
    assert count_after == 1


@pytest.mark.asyncio
async def test_database_bulk_save_and_retrieve_ordering(db: Database):
    """Verify bulk insertion saves messages for multiple users and retrieves ordered by created_at DESC."""
    bulk_data = [
        (10, 20, 100, 1, "First msg", "2026-09-17T10:00:00"),
        (10, 20, 100, 2, "Second msg", "2026-09-17T11:00:00"),
        (10, 20, 200, 3, "Other user msg", "2026-09-17T10:30:00"),
        (10, 20, 100, 4, "Third msg", "2026-09-17T12:00:00"),
    ]
    inserted = await db.save_user_messages_bulk(bulk_data)
    assert inserted == 4

    # Target user 100 messages
    msgs_100 = await db.get_user_recent_messages(10, 100, limit=10)
    assert len(msgs_100) == 3
    # Ordered newest first
    assert msgs_100[0]["content"] == "Third msg"
    assert msgs_100[1]["content"] == "Second msg"
    assert msgs_100[2]["content"] == "First msg"

    # Other user 200 messages
    msgs_200 = await db.get_user_recent_messages(10, 200, limit=10)
    assert len(msgs_200) == 1
    assert msgs_200[0]["content"] == "Other user msg"


# ==============================================================================
# Profiler Engine Tests
# ==============================================================================

def test_render_progress_bar():
    """Verify progress bar generation for various thresholds."""
    assert render_progress_bar(0.0) == "`[░░░░░░░░░░]` **0%**"
    assert render_progress_bar(0.5) == "`[█████░░░░░]` **50%**"
    assert render_progress_bar(1.0) == "`[██████████]` **100%**"


def test_build_profile_state():
    """Verify state summary includes member data, timestamps, and prior offenses."""
    member = make_mock_member(12345, "Bob", account_days=50, server_days=20)
    messages = [
        {"content": "How do I verify?", "created_at": "2026-09-17T15:00:00", "channel_id": 999},
        {"content": "Where is the rules channel?", "created_at": "2026-09-17T15:05:00", "channel_id": 999},
    ]
    prior_offenses = [
        {"created_at": "2026-09-10", "action_taken": "WARN_1_DM", "classification": "SPAM", "message_content": "check out my site"}
    ]

    state = build_profile_state(member, messages, prior_offenses)
    assert "Member: Bob" in state
    assert "Prior Moderation Infractions: 1" in state
    assert "How do I verify?" in state
    assert "Where is the rules channel?" in state
    assert "check out my site" in state


@pytest.mark.asyncio
async def test_evaluate_user_profile():
    """Verify TypeSafe evaluation maps to UserProfileData fields properly."""
    member = make_mock_member()
    messages = [{"content": "What is this server about?", "created_at": "2026-09-17T12:00:00", "channel_id": 1}]

    mock_client = AsyncMock()
    mock_client.evaluate = AsyncMock(return_value=make_mock_typesafe_response(
        scam=0.01, spam=0.05, noob=0.85, toxic=0.0, helpful=0.65, persona="BENIGN_NEWBIE", action="WELCOME_GUIDE"
    ))

    profile = await evaluate_user_profile(mock_client, member, messages)
    assert profile.user_id == member.id
    assert profile.noob_score == 0.85
    assert profile.scam_score == 0.01
    assert profile.persona == "BENIGN_NEWBIE"
    assert profile.recommended_action == "WELCOME_GUIDE"
    assert "Server Beginner" in profile.summary


def test_build_profile_embed():
    """Verify embed formatting and radar fields."""
    member = make_mock_member(user_id=123, name="Charlie")
    guild = AsyncMock(spec=discord.Guild)
    guild.name = "Awesome Community"

    profile = UserProfileData(
        user_id=123,
        display_name="Charlie",
        account_age_days=100,
        server_age_days=40,
        sampled_message_count=15,
        prior_offense_count=0,
        scam_score=0.1,
        spam_score=0.2,
        noob_score=0.75,
        toxic_score=0.05,
        helpful_score=0.6,
        persona="BENIGN_NEWBIE",
        persona_confidence=0.9,
        recommended_action="WELCOME_GUIDE",
        action_confidence=0.85,
        summary="Server Beginner / Needs Guidance",
    )

    embed = build_profile_embed(profile, member, guild)
    assert "Member Dossier — Charlie" in embed.title
    assert "BENIGN_NEWBIE" in embed.description
    assert any("Behavioral Radar" in field.name for field in embed.fields)
    assert any("Noobness" in field.value for field in embed.fields)


# ==============================================================================
# Channel Scraper Tests (Early Termination & Bulk Caching)
# ==============================================================================

@pytest.mark.asyncio
async def test_scrape_channel_history_early_termination(db: Database):
    """Verify channel scraping breaks as soon as count is achieved and backfills all users."""
    guild = AsyncMock(spec=discord.Guild)
    guild.id = 555

    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 777
    channel.guild = guild

    # Generate 10 simulated messages from various users
    now = datetime.datetime.now(datetime.timezone.utc)
    mock_msgs = []
    # User 1 (target user) speaks at index 0, 2, 4
    # User 2 speaks at index 1, 3, 5, 6, 7, 8, 9
    for i in range(10):
        m = AsyncMock(spec=discord.Message)
        m.id = 1000 + i
        m.guild = guild
        m.channel = channel
        m.content = f"Message content {i}"
        m.created_at = now - datetime.timedelta(minutes=i)

        author = AsyncMock(spec=discord.Member)
        author.id = 1 if i in [0, 2, 4] else 2
        author.bot = False
        m.author = author
        mock_msgs.append(m)

    async def mock_history(limit=300, oldest_first=False):
        for msg in mock_msgs:
            yield msg

    channel.history = mock_history

    # Ask for 2 messages from user 1
    target_msgs, total_cached = await scrape_channel_history(
        channel=channel,
        target_user_id=1,
        count=2,
        max_scan=300,
        db=db,
    )

    # Should have stopped after finding 2 messages for user 1 (at index 2)
    assert len(target_msgs) == 2
    # At index 2, messages index 0, 1, 2 were scanned and cached
    assert total_cached == 3

    # Check both users were saved in DB
    user1_msgs = await db.get_user_recent_messages(555, 1)
    user2_msgs = await db.get_user_recent_messages(555, 2)
    assert len(user1_msgs) == 2
    assert len(user2_msgs) == 1


# ==============================================================================
# Slash Command Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_profile_user_command_from_cache(db: Database):
    """Verify /profile runs directly from DB cache when sufficient messages exist."""
    guild_id = 99
    target_member = make_mock_member(user_id=888, name="Dave")

    # Seed 10 messages in DB
    for i in range(10):
        await db.save_user_message(
            guild_id=guild_id,
            channel_id=101,
            user_id=888,
            message_id=5000 + i,
            content=f"Message number {i}",
            created_at=f"2026-09-17T10:{i:02d}:00",
        )

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    interaction.guild = AsyncMock(spec=discord.Guild)
    interaction.guild.id = guild_id
    interaction.guild.name = "Test Guild"
    interaction.user = make_mock_member(user_id=1, name="Admin")
    interaction.user.guild_permissions.moderate_members = True

    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()

    mock_typesafe = AsyncMock()
    mock_typesafe.evaluate = AsyncMock(return_value=make_mock_typesafe_response())

    with patch("main.db_instance", db), patch("main.typesafe_client", mock_typesafe):
        await profile_user.callback(
            interaction=interaction,
            user=target_member,
            message_count=5,
            channel=None,
        )

    interaction.response.defer.assert_called_once_with(ephemeral=True)
    interaction.followup.send.assert_called_once()
    _, kwargs = interaction.followup.send.call_args
    assert "embed" in kwargs
    assert isinstance(kwargs["view"], ProfileReportView)


@pytest.mark.asyncio
async def test_profile_user_command_cache_miss_shows_fallback_dropdown(db: Database):
    """Verify /profile presents ChannelSelectFallbackView when 0 messages in DB."""
    guild_id = 99
    target_member = make_mock_member(user_id=999, name="Ghost")

    interaction = AsyncMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    interaction.guild = AsyncMock(spec=discord.Guild)
    interaction.guild.id = guild_id
    interaction.guild.name = "Test Guild"
    interaction.user = make_mock_member(user_id=1, name="Admin")
    interaction.user.guild_permissions.moderate_members = True

    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()

    with patch("main.db_instance", db):
        await profile_user.callback(
            interaction=interaction,
            user=target_member,
            message_count=25,
            channel=None,
        )

    interaction.followup.send.assert_called_once()
    args, kwargs = interaction.followup.send.call_args
    assert "No cached messages found" in args[0]
    assert isinstance(kwargs["view"], ChannelSelectFallbackView)


# ==============================================================================
# Real-Time on_message Ingestion Test
# ==============================================================================

@pytest.mark.asyncio
async def test_on_message_real_time_caching(db: Database):
    """Verify on_message persists incoming messages to user_messages table."""
    guild = AsyncMock(spec=discord.Guild)
    guild.id = 888

    author = make_mock_member(user_id=444, name="ActiveUser")
    author.bot = False

    channel = AsyncMock(spec=discord.TextChannel)
    channel.id = 222

    msg = AsyncMock(spec=discord.Message)
    msg.id = 987654
    msg.guild = guild
    msg.channel = channel
    msg.author = author
    msg.content = "Real-time incoming message"
    msg.created_at = datetime.datetime.now(datetime.timezone.utc)

    mock_mod = AsyncMock()
    mock_mod.handle_message = AsyncMock(return_value=False)
    mock_process = AsyncMock()

    with patch("main.db_instance", db), patch("main.moderator", mock_mod), patch("main.bot.process_commands", mock_process):
        await on_message(msg)

    mock_process.assert_called_once_with(msg)

    # Check that message was persisted
    count = await db.count_user_messages(888, 444)
    assert count == 1
    messages = await db.get_user_recent_messages(888, 444)
    assert messages[0]["content"] == "Real-time incoming message"
