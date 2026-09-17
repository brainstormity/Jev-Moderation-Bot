"""Database layer for the Discord Moderation Bot using aiosqlite.

Manages persistent guild configurations, offense histories, and
TypeSafe AI feedback memory for real-time in-context learning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

DEFAULT_DB_PATH = os.environ.get("DATABASE_PATH", "bot_data.db")


@dataclass
class GuildSettings:
    guild_id: int
    mod_log_channel_id: Optional[int] = None
    tier1_threshold: float = 0.95
    tier2_threshold: float = 0.70
    first_timeout_minutes: int = 10
    subsequent_timeout_minutes: int = 60
    model_override: Optional[str] = None


class Database:
    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    async def init_db(self) -> None:
        """Initialize all tables and indexes if they do not exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    mod_log_channel_id INTEGER,
                    tier1_threshold REAL DEFAULT 0.95,
                    tier2_threshold REAL DEFAULT 0.70,
                    first_timeout_minutes INTEGER DEFAULT 10,
                    subsequent_timeout_minutes INTEGER DEFAULT 60,
                    model_override TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_offenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_content TEXT NOT NULL,
                    state_summary TEXT,
                    classification TEXT,
                    confidence REAL,
                    noul REAL,
                    action_taken TEXT,
                    status TEXT DEFAULT 'ACTIVE',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_offenses_guild_user
                ON user_offenses (guild_id, user_id, status);
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS moderation_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    offense_id INTEGER,
                    user_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    message_content TEXT NOT NULL,
                    state_summary TEXT,
                    original_choice TEXT,
                    original_confidence REAL,
                    original_noul REAL,
                    feedback_type TEXT NOT NULL,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (offense_id) REFERENCES user_offenses(id)
                );
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_feedback_guild_type
                ON moderation_feedback (guild_id, feedback_type, created_at DESC);
                """
            )
            await db.commit()

    async def get_guild_settings(self, guild_id: int) -> GuildSettings:
        """Fetch settings for a guild, or return defaults if unconfigured."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT guild_id, mod_log_channel_id, tier1_threshold,
                       tier2_threshold, first_timeout_minutes,
                       subsequent_timeout_minutes, model_override
                FROM guild_settings WHERE guild_id = ?;
                """,
                (guild_id,),
            )
            row = await cursor.fetchone()
            if row:
                return GuildSettings(
                    guild_id=row["guild_id"],
                    mod_log_channel_id=row["mod_log_channel_id"],
                    tier1_threshold=row["tier1_threshold"],
                    tier2_threshold=row["tier2_threshold"],
                    first_timeout_minutes=row["first_timeout_minutes"],
                    subsequent_timeout_minutes=row["subsequent_timeout_minutes"],
                    model_override=row["model_override"],
                )
            return GuildSettings(guild_id=guild_id)

    async def set_mod_log_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        """Assign or clear the designated mod-log channel for a guild."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO guild_settings (guild_id, mod_log_channel_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    mod_log_channel_id = excluded.mod_log_channel_id,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (guild_id, channel_id),
            )
            await db.commit()

    async def set_guild_timeouts(self, guild_id: int, first_mins: int, subsequent_mins: int) -> None:
        """Update first and subsequent timeout durations (in minutes)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO guild_settings (guild_id, first_timeout_minutes, subsequent_timeout_minutes)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    first_timeout_minutes = excluded.first_timeout_minutes,
                    subsequent_timeout_minutes = excluded.subsequent_timeout_minutes,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (guild_id, first_mins, subsequent_mins),
            )
            await db.commit()

    async def set_guild_thresholds(self, guild_id: int, tier1: float, tier2: float) -> None:
        """Update confidence thresholds for tier 1 and tier 2."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO guild_settings (guild_id, tier1_threshold, tier2_threshold)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    tier1_threshold = excluded.tier1_threshold,
                    tier2_threshold = excluded.tier2_threshold,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (guild_id, tier1, tier2),
            )
            await db.commit()

    async def get_active_offense_count(self, guild_id: int, user_id: int) -> int:
        """Get the number of currently ACTIVE infractions for a user."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM user_offenses
                WHERE guild_id = ? AND user_id = ? AND status = 'ACTIVE';
                """,
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def record_offense(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        message_content: str,
        state_summary: str,
        classification: str,
        confidence: float,
        noul: float,
        action_taken: str,
    ) -> Tuple[int, int]:
        """Record an offense and return (offense_id, new_active_offense_count)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO user_offenses (
                    guild_id, user_id, channel_id, message_content,
                    state_summary, classification, confidence, noul,
                    action_taken, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE');
                """,
                (
                    guild_id,
                    user_id,
                    channel_id,
                    message_content,
                    state_summary,
                    classification,
                    confidence,
                    noul,
                    action_taken,
                ),
            )
            offense_id = cursor.lastrowid or 0

            count_cursor = await db.execute(
                """
                SELECT COUNT(*) FROM user_offenses
                WHERE guild_id = ? AND user_id = ? AND status = 'ACTIVE';
                """,
                (guild_id, user_id),
            )
            count_row = await count_cursor.fetchone()
            total_active = count_row[0] if count_row else 1

            await db.commit()
            return offense_id, total_active

    async def get_user_offenses(
        self, guild_id: int, user_id: int, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Fetch past infractions for a user in chronological order (newest first)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, channel_id, message_content, classification,
                       confidence, noul, action_taken, status, created_at
                FROM user_offenses
                WHERE guild_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?;
                """,
                (guild_id, user_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def pardon_offense(
        self, offense_id: int, moderator_id: int, notes: str = ""
    ) -> bool:
        """Mark an offense as PARDONED and record it into moderation_feedback as FALSE_FLAG."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, guild_id, user_id, message_content, state_summary,
                       classification, confidence, noul, status
                FROM user_offenses WHERE id = ?;
                """,
                (offense_id,),
            )
            offense = await cursor.fetchone()
            if not offense:
                return False

            # Update status to PARDONED
            await db.execute(
                "UPDATE user_offenses SET status = 'PARDONED' WHERE id = ?;",
                (offense_id,),
            )

            # Insert into moderation_feedback
            await db.execute(
                """
                INSERT INTO moderation_feedback (
                    guild_id, offense_id, user_id, moderator_id,
                    message_content, state_summary, original_choice,
                    original_confidence, original_noul, feedback_type, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FALSE_FLAG', ?);
                """,
                (
                    offense["guild_id"],
                    offense["id"],
                    offense["user_id"],
                    moderator_id,
                    offense["message_content"],
                    offense["state_summary"],
                    offense["classification"],
                    offense["confidence"],
                    offense["noul"],
                    notes or "Pardoned as false flag by administrator",
                ),
            )
            await db.commit()
            return True

    async def escalate_offense_to_ban(
        self, offense_id: int, moderator_id: int, notes: str = ""
    ) -> bool:
        """Mark an offense as BANNED and record it into moderation_feedback as CONFIRMED_THREAT."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, guild_id, user_id, message_content, state_summary,
                       classification, confidence, noul
                FROM user_offenses WHERE id = ?;
                """,
                (offense_id,),
            )
            offense = await cursor.fetchone()
            if not offense:
                return False

            await db.execute(
                "UPDATE user_offenses SET status = 'BANNED' WHERE id = ?;",
                (offense_id,),
            )

            await db.execute(
                """
                INSERT INTO moderation_feedback (
                    guild_id, offense_id, user_id, moderator_id,
                    message_content, state_summary, original_choice,
                    original_confidence, original_noul, feedback_type, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CONFIRMED_THREAT', ?);
                """,
                (
                    offense["guild_id"],
                    offense["id"],
                    offense["user_id"],
                    moderator_id,
                    offense["message_content"],
                    offense["state_summary"],
                    offense["classification"],
                    offense["confidence"],
                    offense["noul"],
                    notes or "Confirmed threat by administrator (escalated to ban)",
                ),
            )
            await db.commit()
            return True

    async def get_recent_false_flags(self, guild_id: int, limit: int = 5) -> List[str]:
        """Fetch the most recent pardoned false-positive message contents for this guild.

        These are dynamically injected into Jev AI's prompt criteria on subsequent
        evaluations, improving model performance in real time on the fly.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT message_content FROM moderation_feedback
                WHERE guild_id = ? AND feedback_type = 'FALSE_FLAG'
                ORDER BY id DESC
                LIMIT ?;
                """,
                (guild_id, limit),
            )
            rows = await cursor.fetchall()
            return [row["message_content"] for row in rows if row["message_content"]]

    async def get_feedback_records(
        self, guild_id: int, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Export feedback records (false flags & confirmed threats) for auditing/evaluation."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, offense_id, user_id, moderator_id, message_content,
                       original_choice, original_confidence, original_noul,
                       feedback_type, notes, created_at
                FROM moderation_feedback
                WHERE guild_id = ?
                ORDER BY id DESC
                LIMIT ?;
                """,
                (guild_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


db_instance = Database()
