"""Main entry point for the Discord Moderation Bot with TypeSafe AI (Jev System One).

Configures Discord bot intents, registers slash commands, and connects the
real-time on_message moderation pipeline.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import logging
import sys
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import Database, db_instance
from moderator import MessageModerator
from typesafe import AsyncTypeSafe

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot")

# Setup Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=config.COMMAND_PREFIX, intents=intents)
typesafe_client = AsyncTypeSafe(api_key=config.TYPESAFE_API_KEY)
moderator = MessageModerator(client=typesafe_client, db=db_instance)


@bot.event
async def on_ready() -> None:
    """Initialize database and sync application commands on startup."""
    await db_instance.init_db()
    logger.info("Connected to database successfully.")

    try:
        synced = await bot.tree.sync()
        logger.info("Synced %d application command(s).", len(synced))
    except Exception as exc:
        logger.error("Failed to sync application commands: %s", exc)

    logger.info("Bot is ready as %s (ID: %s)", bot.user, bot.user.id if bot.user else "N/A")


@bot.event
async def on_message(message: discord.Message) -> None:
    """Real-time message listener hooked into TypeSafe AI moderation."""
    # 1. Ignore messages from bots
    if message.author.bot:
        return

    # 2. Run moderation pipeline
    try:
        was_moderated = await moderator.handle_message(message)
    except Exception as exc:
        logger.exception("Unexpected error in moderation handler: %s", exc)
        was_moderated = False

    # 3. If message was not removed by moderation, allow normal command processing
    if not was_moderated:
        await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="set-mod-log", description="Set the moderation alert log channel for this server.")
@app_commands.describe(channel="The text channel where moderation alerts should be posted")
@app_commands.checks.has_permissions(administrator=True)
async def set_mod_log(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    """Configure the guild's mod-log channel after validating bot permissions."""
    guild = interaction.guild
    if not guild or not guild.me:
        await interaction.response.send_message("❌ Cannot resolve server information.", ephemeral=True)
        return

    # Check bot permissions in that channel
    perms = channel.permissions_for(guild.me)
    missing = []
    if not perms.view_channel:
        missing.append("View Channel")
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.embed_links:
        missing.append("Embed Links")

    if missing:
        missing_str = ", ".join(missing)
        await interaction.response.send_message(
            f"❌ The bot is missing required permissions in {channel.mention}: **{missing_str}**.\n"
            f"Please grant these permissions in channel settings and try again.",
            ephemeral=True,
        )
        return

    await db_instance.set_mod_log_channel(guild.id, channel.id)
    await interaction.response.send_message(
        f"✅ Moderation alert channel successfully set to {channel.mention}.\n"
        f"Moderation alerts with interactive 1-click action buttons will now appear there.",
        ephemeral=True,
    )


@bot.tree.command(name="unset-mod-log", description="Remove the configured mod-log channel.")
@app_commands.checks.has_permissions(administrator=True)
async def unset_mod_log(interaction: discord.Interaction) -> None:
    """Remove mod-log channel. Moderation continues writing to native Server Audit Logs."""
    if not interaction.guild:
        return
    await db_instance.set_mod_log_channel(interaction.guild.id, None)
    await interaction.response.send_message(
        "✅ Mod-log channel has been unset.\n"
        "*(Note: Automated moderation actions will still be recorded in Discord's native Server Audit Log.)*",
        ephemeral=True,
    )


@bot.tree.command(name="set-timeouts", description="Configure progressive timeout durations for offenses.")
@app_commands.describe(
    first_offense_mins="Timeout duration in minutes for the 3rd offense (default: 10)",
    subsequent_offense_mins="Timeout duration in minutes for 4th+ offenses (default: 60)",
)
@app_commands.checks.has_permissions(administrator=True)
async def set_timeouts(
    interaction: discord.Interaction, first_offense_mins: int, subsequent_offense_mins: int
) -> None:
    """Set custom timeout durations for infractions."""
    if not interaction.guild:
        return

    if first_offense_mins <= 0 or subsequent_offense_mins <= 0:
        await interaction.response.send_message("❌ Durations must be positive integers.", ephemeral=True)
        return

    if first_offense_mins > 40320 or subsequent_offense_mins > 40320:  # Discord 28-day max timeout
        await interaction.response.send_message("❌ Durations cannot exceed 40,320 minutes (28 days).", ephemeral=True)
        return

    await db_instance.set_guild_timeouts(interaction.guild.id, first_offense_mins, subsequent_offense_mins)
    await interaction.response.send_message(
        f"✅ Updated timeout durations for **{interaction.guild.name}**:\n"
        f"• **3rd Offense (First Timeout)**: `{first_offense_mins}` minutes\n"
        f"• **4th+ Offenses (Subsequent)**: `{subsequent_offense_mins}` minutes\n\n"
        f"*(Offenses 1 & 2 will continue receiving warning DMs without timeouts.)*",
        ephemeral=True,
    )


@bot.tree.command(name="set-thresholds", description="Adjust TypeSafe AI confidence thresholds.")
@app_commands.describe(
    tier1="Confidence threshold for Tier 1 high-confidence threats (default: 0.95)",
    tier2="Confidence threshold for Tier 2 medium-confidence threats (default: 0.70)",
)
@app_commands.checks.has_permissions(administrator=True)
async def set_thresholds(interaction: discord.Interaction, tier1: float, tier2: float) -> None:
    """Adjust Tier 1 and Tier 2 confidence sensitivity."""
    if not interaction.guild:
        return

    if not (0.0 < tier2 < tier1 <= 1.0):
        await interaction.response.send_message("❌ Thresholds must satisfy: `0.0 < tier2 < tier1 <= 1.0`.", ephemeral=True)
        return

    await db_instance.set_guild_thresholds(interaction.guild.id, tier1, tier2)
    await interaction.response.send_message(
        f"✅ Updated moderation thresholds for **{interaction.guild.name}**:\n"
        f"• **Tier 1 (High Threat)**: `{tier1:.2f}`\n"
        f"• **Tier 2 (Medium Threat)**: `{tier2:.2f}`",
        ephemeral=True,
    )


@bot.tree.command(name="user-offenses", description="View a member's complete infraction timeline and what they said.")
@app_commands.describe(user="The member whose offense history you want to inspect")
@app_commands.checks.has_permissions(moderate_members=True)
async def user_offenses(interaction: discord.Interaction, user: discord.Member) -> None:
    """Display user offense history with timestamps, message snippets, and resolution status."""
    if not interaction.guild:
        return

    offenses = await db_instance.get_user_offenses(interaction.guild.id, user.id, limit=10)
    if not offenses:
        await interaction.response.send_message(f"✅ {user.mention} has no recorded moderation offenses in this server.", ephemeral=True)
        return

    active_count = sum(1 for o in offenses if o["status"] == "ACTIVE")
    pardoned_count = sum(1 for o in offenses if o["status"] == "PARDONED")

    embed = discord.Embed(
        title=f"📋 Infraction History — {user.display_name}",
        description=(
            f"**Member**: {user.mention} (`{user.id}`)\n"
            f"**Active Infractions**: `{active_count}` | **Pardoned**: `{pardoned_count}`\n"
            f"*Showing the {len(offenses)} most recent offenses:*"
        ),
        color=discord.Color.gold() if active_count > 0 else discord.Color.green(),
    )

    for off in offenses:
        status_emoji = "🔴" if off["status"] == "ACTIVE" else ("🟢" if off["status"] == "PARDONED" else "⚫")
        created_str = off["created_at"]
        clean_msg = off["message_content"].replace("```", "")[:120]

        embed.add_field(
            name=f"{status_emoji} Offense #{off['id']} • {off['action_taken']} [{off['status']}]",
            value=(
                f"**Channel**: <#{off['channel_id']}> | **Date**: `{created_str}`\n"
                f"**Classification**: `{off['classification']}` (Conf: `{off['confidence']:.1%}`)\n"
                f"**Content**: ```{clean_msg}```"
            ),
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="pardon", description="Manually lift timeout and pardon a member's latest infraction.")
@app_commands.describe(user="The member to pardon")
@app_commands.checks.has_permissions(administrator=True)
async def pardon_user(interaction: discord.Interaction, user: discord.Member) -> None:
    """Manually pardon a user and store feedback in Jev AI memory."""
    if not interaction.guild:
        return

    offenses = await db_instance.get_user_offenses(interaction.guild.id, user.id, limit=5)
    active_offenses = [o for o in offenses if o["status"] == "ACTIVE"]

    if not active_offenses:
        await interaction.response.send_message(f"ℹ️ {user.mention} does not have any active infractions to pardon.", ephemeral=True)
        return

    latest = active_offenses[0]

    # Lift timeout if active
    if user.is_timed_out():
        try:
            await user.timeout(None, reason=f"Manually pardoned by {interaction.user}")
        except Exception as exc:
            logger.warning("Could not lift timeout for %s: %s", user.id, exc)

    await db_instance.pardon_offense(latest["id"], interaction.user.id, notes="Manually pardoned via /pardon slash command")
    await interaction.response.send_message(
        f"✅ **Pardoned Offense #{latest['id']} for {user.mention}.**\n"
        f"• Timeout lifted (if active).\n"
        f"• Infraction record marked as PARDONED.\n"
        f"• Precedent saved to Jev AI in-context memory for this server.",
        ephemeral=True,
    )


@bot.tree.command(name="export-feedback", description="Export moderation feedback data (for TypeSafe AI tuning).")
@app_commands.describe(file_format="Format for the exported dataset (json or csv)")
@app_commands.checks.has_permissions(administrator=True)
async def export_feedback(
    interaction: discord.Interaction, file_format: Literal["json", "csv"] = "json"
) -> None:
    """Export feedback records for evaluating and refining TypeSafe AI System One decision prompts."""
    if not interaction.guild:
        return

    records = await db_instance.get_feedback_records(interaction.guild.id, limit=500)
    if not records:
        await interaction.response.send_message("ℹ️ No feedback records found for this server.", ephemeral=True)
        return

    if file_format == "json":
        data_str = json.dumps(records, indent=2)
        file_bytes = io.BytesIO(data_str.encode("utf-8"))
        filename = f"typesafe_feedback_guild_{interaction.guild.id}.json"
    else:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
        file_bytes = io.BytesIO(output.getvalue().encode("utf-8"))
        filename = f"typesafe_feedback_guild_{interaction.guild.id}.csv"

    file_bytes.seek(0)
    discord_file = discord.File(fp=file_bytes, filename=filename)
    await interaction.response.send_message(
        f"📁 Exported **{len(records)}** feedback record(s) for TypeSafe AI evaluation.",
        file=discord_file,
        ephemeral=True,
    )


@bot.tree.command(name="mod-config", description="View current server moderation settings and Jev AI status.")
@app_commands.checks.has_permissions(administrator=True)
async def mod_config(interaction: discord.Interaction) -> None:
    """View current guild settings and active false flag memory."""
    if not interaction.guild:
        return

    settings = await db_instance.get_guild_settings(interaction.guild.id)
    recent_flags = await db_instance.get_recent_false_flags(interaction.guild.id, limit=10)

    channel_mention = f"<#{settings.mod_log_channel_id}>" if settings.mod_log_channel_id else "*None (Server Audit Log only)*"

    embed = discord.Embed(
        title=f"⚙️ Moderation Settings — {interaction.guild.name}",
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="Mod-Log Channel", value=channel_mention, inline=True)
    embed.add_field(name="Tier 1 Threshold", value=f"`{settings.tier1_threshold:.2f}`", inline=True)
    embed.add_field(name="Tier 2 Threshold", value=f"`{settings.tier2_threshold:.2f}`", inline=True)

    embed.add_field(name="1st Timeout Duration", value=f"`{settings.first_timeout_minutes}` mins (3rd Offense)", inline=True)
    embed.add_field(name="Subsequent Timeout", value=f"`{settings.subsequent_timeout_minutes}` mins (4th+ Offenses)", inline=True)
    embed.add_field(name="Model Override", value=f"`{settings.model_override or 'Default (jev-latest)'}`", inline=True)

    embed.add_field(
        name="🧠 Jev AI In-Context Memory",
        value=f"**{len(recent_flags)}** active safe precedent(s) currently guiding Jev evaluations in this server.",
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """Handle application command permission errors gracefully."""
    if isinstance(error, app_commands.MissingPermissions):
        perms = ", ".join(error.missing_permissions)
        await interaction.response.send_message(
            f"❌ You do not have the required permissions (`{perms}`) to use this command.",
            ephemeral=True,
        )
    else:
        logger.error("Command error in %s: %s", interaction.command.name if interaction.command else "unknown", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ An error occurred while executing the command: {error}", ephemeral=True)


def main() -> None:
    """Main startup script."""
    if not config.DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN environment variable is not set. Please update .env.")
        sys.exit(1)
    if not config.TYPESAFE_API_KEY:
        logger.warning("TYPESAFE_API_KEY is not set. TypeSafe AI evaluations will fail unless set.")

    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
