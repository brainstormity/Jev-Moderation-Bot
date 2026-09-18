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
from profiler import build_profile_embed, evaluate_user_profile
from profile_views import ChannelSelectFallbackView, ProfileReportView, scrape_channel_history
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
    """Real-time message listener hooked into TypeSafe AI moderation and rolling message cache."""
    # 1. Ignore messages from bots or outside guilds
    if message.author.bot or not message.guild:
        return

    # 2. Persist to rolling message cache with accurate Discord created_at timestamp
    if message.content.strip():
        try:
            await db_instance.save_user_message(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                message_id=message.id,
                content=message.content,
                created_at=message.created_at.isoformat(),
            )
        except Exception as exc:
            logger.warning("Failed to cache message %s: %s", message.id, exc)

    # 3. Run moderation pipeline
    try:
        was_moderated = await moderator.handle_message(message)
    except Exception as exc:
        logger.exception("Unexpected error in moderation handler: %s", exc)
        was_moderated = False

    # 4. If message was not removed by moderation, allow normal command processing
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


@bot.tree.command(
    name="profile",
    description="Build an AI behavioral dossier for a member (Noobness, Spam, Scam, Toxicity).",
)
@app_commands.describe(
    user="The member whose behavioral profile you want to inspect",
    message_count="Number of recent messages to analyze (10 to 100, default: 25)",
    channel="Optional specific channel to scrape if local cache is insufficient",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def profile_user(
    interaction: discord.Interaction,
    user: discord.Member,
    message_count: app_commands.Range[int, 10, 100] = 25,
    channel: Optional[discord.TextChannel] = None,
) -> None:
    """Generate an AI behavioral profile for a member using local cache or targeted channel scrape."""
    if not interaction.guild:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    # Ephemeral deferral allows moderators to inspect privately without timing out
    await interaction.response.defer(ephemeral=True)

    guild_id = interaction.guild.id
    settings = await db_instance.get_guild_settings(guild_id)
    prior_offenses = await db_instance.get_user_offenses(guild_id, user.id, limit=10)

    # 1. If a specific channel was explicitly provided, scrape it directly and backfill
    if channel:
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.read_message_history or not perms.view_channel:
            await interaction.followup.send(
                f"❌ The bot lacks `Read Message History` permission in {channel.mention}.",
                ephemeral=True,
            )
            return

        await scrape_channel_history(
            channel=channel,
            target_user_id=user.id,
            count=message_count,
            max_scan=300,
            db=db_instance,
        )

    # 2. Retrieve messages from local database
    messages = await db_instance.get_user_recent_messages(guild_id, user.id, limit=message_count)

    # 3. If no messages exist in DB, offer interactive channel dropdown fallback
    if not messages:
        fallback_view = ChannelSelectFallbackView(
            target_member=user,
            requested_count=message_count,
            client=typesafe_client,
            db=db_instance,
            existing_cached_count=0,
        )
        await interaction.followup.send(
            f"ℹ️ **No cached messages found for {user.mention}** in local database.\n"
            f"Please select a channel from the dropdown below where {user.display_name} has been active to fetch their history:",
            view=fallback_view,
            ephemeral=True,
        )
        return

    # 4. Generate AI profile
    profile = await evaluate_user_profile(
        client=typesafe_client,
        member=user,
        messages=messages,
        prior_offenses=prior_offenses,
        model=settings.model_override,
    )

    embed = build_profile_embed(profile, user, interaction.guild)
    report_view = ProfileReportView(
        profile=profile,
        target_member=user,
        client=typesafe_client,
        db=db_instance,
        requested_count=message_count,
    )

    notice = ""
    if len(messages) < message_count and not channel:
        notice = f"*(Note: Found only `{len(messages)}/{message_count}` messages in local cache. Use 'Scan Another Channel' below to pull more.)*\n"

    await interaction.followup.send(
        content=notice or None,
        embed=embed,
        view=report_view,
        ephemeral=True,
    )


@bot.tree.context_menu(name="Generate AI Profile")
@app_commands.checks.has_permissions(moderate_members=True)
async def profile_user_context(interaction: discord.Interaction, user: discord.Member) -> None:
    """Right-click context menu shortcut to generate an AI profile for a member."""
    if not interaction.guild:
        await interaction.response.send_message("❌ This action can only be used in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    guild_id = interaction.guild.id
    settings = await db_instance.get_guild_settings(guild_id)
    prior_offenses = await db_instance.get_user_offenses(guild_id, user.id, limit=10)
    messages = await db_instance.get_user_recent_messages(guild_id, user.id, limit=25)

    if not messages:
        fallback_view = ChannelSelectFallbackView(
            target_member=user,
            requested_count=25,
            client=typesafe_client,
            db=db_instance,
            existing_cached_count=0,
        )
        await interaction.followup.send(
            f"ℹ️ **No cached messages found for {user.mention}** in local database.\n"
            f"Please select a channel below to fetch their history:",
            view=fallback_view,
            ephemeral=True,
        )
        return

    profile = await evaluate_user_profile(
        client=typesafe_client,
        member=user,
        messages=messages,
        prior_offenses=prior_offenses,
        model=settings.model_override,
    )

    embed = build_profile_embed(profile, user, interaction.guild)
    report_view = ProfileReportView(
        profile=profile,
        target_member=user,
        client=typesafe_client,
        db=db_instance,
        requested_count=25,
    )

    await interaction.followup.send(embed=embed, view=report_view, ephemeral=True)


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
