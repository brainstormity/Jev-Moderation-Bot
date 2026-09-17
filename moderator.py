"""Moderation engine for the Discord bot using TypeSafe AI (Jev System One).

Handles:
1. Context extraction & dynamic false-flag in-context learning injection.
2. 4-Stage Progressive Escalation (Offense 1-2: Warning DMs, Offense 3: 10m Timeout, Offense 4+: 1h Timeout).
3. Dual audit logging: native Discord Server Audit Logs + dedicated #mod-log channel.
4. Interactive Admin-only Discord UI views with two-step ephemeral confirmation (Pardon & Ban).
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import ui

from database import Database, GuildSettings, db_instance
from typesafe import AsyncTypeSafe, Choice, Noul, TypeSafeEvaluationResponse

logger = logging.getLogger("moderator")


def build_state_summary(message: discord.Message, recent_false_flags: Optional[List[str]] = None) -> str:
    """Build a structured state string for TypeSafe AI evaluation."""
    now = datetime.datetime.now(datetime.timezone.utc)
    account_age_days = (now - message.author.created_at).days
    has_external_link = "http://" in message.content.lower() or "https://" in message.content.lower()
    channel_name = getattr(message.channel, "name", "unknown")
    channel_id = message.channel.id

    state_lines = [
        "=== DISCORD MESSAGE CONTEXT ===",
        f"Author ID: {message.author.id}",
        f"Account Age (Days): {account_age_days}",
        f"External Link Indicator: {has_external_link}",
        f"Channel: #{channel_name} (ID: {channel_id})",
    ]

    if recent_false_flags:
        state_lines.append("\n=== COMMUNITY VERIFIED PRECEDENTS (CONFIRMED LEGITIMATE BY ADMINS) ===")
        state_lines.append("The following message styles/contents were previously flagged but verified SAFE by administrators:")
        for idx, flag in enumerate(recent_false_flags[:5], 1):
            clean_flag = flag.replace("\n", " ").strip()[:150]
            state_lines.append(f"{idx}. \"{clean_flag}\"")

    state_lines.extend([
        "\n=== MESSAGE CONTENT TO EVALUATE ===",
        message.content,
    ])

    return "\n".join(state_lines)


async def send_user_dm(member: discord.Member, embed: discord.Embed) -> bool:
    """Safely deliver a direct warning message to a user, handling closed DMs gracefully."""
    try:
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.warning("Could not send DM to user %s (%s): %s", member.id, member.name, exc)
        return False


class PardonConfirmView(ui.View):
    """Two-step ephemeral confirmation view for pardoning an offense."""

    def __init__(
        self,
        offense_id: int,
        user_id: int,
        guild_id: int,
        parent_message: discord.Message,
        db: Database,
    ) -> None:
        super().__init__(timeout=60)
        self.offense_id = offense_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.parent_message = parent_message
        self.db = db

    @ui.button(label="Confirm Pardon", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_pardon(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only administrators can confirm a pardon.", ephemeral=True)
            return

        guild = interaction.guild
        if guild:
            member = guild.get_member(self.user_id)
            if member:
                try:
                    await member.timeout(
                        None,
                        reason=f"TypeSafe False Flag: Pardoned by Admin {interaction.user} ({interaction.user.id})",
                    )
                except Exception as exc:
                    logger.warning("Failed to lift timeout for user %s: %s", self.user_id, exc)

        # Update database record
        await self.db.pardon_offense(self.offense_id, interaction.user.id)

        # Update original mod-log message
        try:
            if self.parent_message.embeds:
                old_embed = self.parent_message.embeds[0]
                resolved_embed = old_embed.copy()
                resolved_embed.color = discord.Color.green()
                resolved_embed.title = f"🟢 [PARDONED AS FALSE FLAG] {old_embed.title or 'Moderation Alert'}"
                resolved_embed.add_field(
                    name="Resolution",
                    value=f"Pardoned by {interaction.user.mention} (<t:{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}:R>).\n"
                          f"*Precedent saved to improve Jev AI runtime memory.*",
                    inline=False,
                )
                await self.parent_message.edit(embed=resolved_embed, view=None)
        except Exception as exc:
            logger.warning("Could not update parent mod-log message: %s", exc)

        await interaction.response.edit_message(
            content="✅ **Successfully Pardoned!** The user's timeout has been lifted, the offense has been pardoned, "
                    "and this example has been added to Jev AI's in-context memory for this server.",
            view=None,
        )

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_pardon(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.edit_message(content="Action cancelled. No changes were made.", view=None)


class BanConfirmView(ui.View):
    """Two-step ephemeral confirmation view for permanently banning a user."""

    def __init__(
        self,
        offense_id: int,
        user_id: int,
        guild_id: int,
        parent_message: discord.Message,
        db: Database,
    ) -> None:
        super().__init__(timeout=60)
        self.offense_id = offense_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.parent_message = parent_message
        self.db = db

    @ui.button(label="Confirm Permanent Ban", style=discord.ButtonStyle.danger, emoji="🔨")
    async def confirm_ban(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only administrators can execute a ban.", ephemeral=True)
            return

        guild = interaction.guild
        if guild:
            try:
                await guild.ban(
                    discord.Object(id=self.user_id),
                    reason=f"TypeSafe Manual Escalation: Banned by Admin {interaction.user} ({interaction.user.id})",
                )
            except Exception as exc:
                await interaction.response.send_message(f"❌ Failed to ban user: {exc}", ephemeral=True)
                return

        # Update database record
        await self.db.escalate_offense_to_ban(self.offense_id, interaction.user.id)

        # Update original mod-log message
        try:
            if self.parent_message.embeds:
                old_embed = self.parent_message.embeds[0]
                resolved_embed = old_embed.copy()
                resolved_embed.color = discord.Color.dark_red()
                resolved_embed.title = f"🔴 [USER PERMANENTLY BANNED] {old_embed.title or 'Moderation Alert'}"
                resolved_embed.add_field(
                    name="Resolution",
                    value=f"Permanently banned by {interaction.user.mention} (<t:{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}:R>).",
                    inline=False,
                )
                await self.parent_message.edit(embed=resolved_embed, view=None)
        except Exception as exc:
            logger.warning("Could not update parent mod-log message: %s", exc)

        await interaction.response.edit_message(
            content=f"🔨 **User <@{self.user_id}> has been permanently banned.**",
            view=None,
        )

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_ban(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.edit_message(content="Action cancelled. User was not banned.", view=None)


class ModLogActionView(ui.View):
    """Action view attached to #mod-log embeds, restricted strictly to Administrators."""

    def __init__(
        self,
        offense_id: int,
        user_id: int,
        guild_id: int,
        db: Database = db_instance,
    ) -> None:
        super().__init__(timeout=None)  # Persistent view
        self.offense_id = offense_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.db = db

    @ui.button(label="Pardon (False Flag)", style=discord.ButtonStyle.success, emoji="🟢", custom_id="modlog_pardon")
    async def pardon_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only server administrators can pardon moderation offenses.", ephemeral=True)
            return

        confirm_view = PardonConfirmView(
            offense_id=self.offense_id,
            user_id=self.user_id,
            guild_id=self.guild_id,
            parent_message=interaction.message,
            db=self.db,
        )
        await interaction.response.send_message(
            content="⚠️ **Confirm False Flag Pardon**\n"
                    f"Are you sure you want to pardon <@{self.user_id}>?\n"
                    "• Lifts any active timeout.\n"
                    "• Clears the offense from active record.\n"
                    "• Injects this message into Jev AI's runtime memory to prevent future false flags.",
            view=confirm_view,
            ephemeral=True,
        )

    @ui.button(label="Ban User", style=discord.ButtonStyle.danger, emoji="🔴", custom_id="modlog_ban")
    async def ban_button(self, interaction: discord.Interaction, button: ui.Button) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only server administrators can ban users.", ephemeral=True)
            return

        confirm_view = BanConfirmView(
            offense_id=self.offense_id,
            user_id=self.user_id,
            guild_id=self.guild_id,
            parent_message=interaction.message,
            db=self.db,
        )
        await interaction.response.send_message(
            content=f"⚠️ **Confirm Permanent Ban**\n"
                    f"Are you sure you want to permanently ban <@{self.user_id}> from **{interaction.guild.name}**?",
            view=confirm_view,
            ephemeral=True,
        )


class MessageModerator:
    """Core message moderation pipeline integrating TypeSafe AI and progressive escalation."""

    def __init__(self, client: AsyncTypeSafe, db: Database = db_instance) -> None:
        self.client = client
        self.db = db

    async def evaluate_message(
        self, message: discord.Message, settings: GuildSettings
    ) -> Tuple[Optional[TypeSafeEvaluationResponse], Optional[str]]:
        """Call TypeSafe Jev System One with dynamic in-context precedents."""
        recent_false_flags = await self.db.get_recent_false_flags(message.guild.id, limit=5)
        state_summary = build_state_summary(message, recent_false_flags)

        choice_criteria: Dict[str, Any] = {
            "LEGITIMATE": {
                "description": "Safe, legitimate, ordinary community conversation, questions, or approved links.",
            },
            "SPAM": "Repetitive promotional messages, unsolicited advertisements, commercial links, or copypasta spam.",
            "SCAM_LINK": "Malicious phishing attempts, fake Steam/Discord Nitro giveaways, wallet drainers, credential stealers, or scam URLs.",
        }
        if recent_false_flags:
            choice_criteria["LEGITIMATE"]["known_safe_precedents"] = recent_false_flags

        questions = [
            Choice(
                id="spam_classification",
                options=["LEGITIMATE", "SPAM", "SCAM_LINK"],
                description="Classify if the message content is legitimate, general spam, or a dangerous scam/phishing link.",
                criteria=choice_criteria,
            ),
            Noul(
                id="requires_immediate_ban",
                description="Is this an explicit malicious scam attempt requiring an immediate ban?",
            ),
        ]

        target_model = settings.model_override or getattr(self.client, "model", "jev-latest")
        response = await self.client.evaluate(state=state_summary, questions=questions, model=target_model)
        return response, state_summary

    async def handle_message(self, message: discord.Message) -> bool:
        """Process a message through the moderation pipeline.

        Returns True if a moderation action was taken (message deleted),
        or False if message was allowed through (Tier 3 or error).
        """
        # 1. Ignore bots & DMs
        if message.author.bot or not message.guild:
            return False

        # 2. Retrieve guild settings
        guild = message.guild
        settings = await self.db.get_guild_settings(guild.id)

        # 3. Evaluate with TypeSafe AI inside try/except block
        try:
            response, state_summary = await self.evaluate_message(message, settings)
        except Exception as exc:
            logger.exception("TypeSafe AI evaluation encountered an error for message %s: %s", message.id, exc)
            return False

        if not response:
            return False

        # 4. Extract classification results
        spam_result = response.questions.get("spam_classification")
        ban_result = response.questions.get("requires_immediate_ban")

        if not spam_result:
            return False

        choice = spam_result.choice or "LEGITIMATE"
        confidence = spam_result.confidence
        noul = getattr(ban_result, "noul", 0.0) if ban_result else 0.0

        # Tier Decision Logic
        is_tier1 = (confidence >= settings.tier1_threshold and choice in ["SPAM", "SCAM_LINK"]) or (noul >= settings.tier1_threshold)
        is_tier2 = (settings.tier2_threshold <= confidence < settings.tier1_threshold and choice in ["SPAM", "SCAM_LINK"]) and not is_tier1

        # Tier 3 — Low Confidence / Legitimate: Allow through
        if not is_tier1 and not is_tier2:
            return False

        # 5. Execute Action: Delete message immediately with Audit Log reason
        tier_label = "Tier 1 (High Threat)" if is_tier1 else "Tier 2 (Medium Threat)"
        audit_reason = f"TypeSafe AI {tier_label}: {choice} (Conf: {confidence:.2f}, Noul: {noul:.2f})"
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound) as exc:
            logger.warning("Could not delete message %s: %s", message.id, exc)

        # 6. Progressive Escalation Ladder based on user's active offense count
        prior_active_count = await self.db.get_active_offense_count(guild.id, message.author.id)
        current_offense_num = prior_active_count + 1

        action_taken = ""
        timeout_minutes = 0
        dm_embed = discord.Embed(
            title=f"🛡️ Message Removed in {guild.name}",
            color=discord.Color.red() if is_tier1 else discord.Color.orange(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        dm_embed.add_field(name="Violation Category", value=f"`{choice}`", inline=True)
        dm_embed.add_field(name="Detection Severity", value=tier_label, inline=True)
        dm_embed.add_field(name="Removed Message", value=f"```{message.content[:200]}```", inline=False)

        if current_offense_num == 1:
            action_taken = "WARN_1_DM"
            dm_embed.description = (
                "⚠️ **First Warning**: Your message was flagged by automated moderation for spam/scam and removed.\n"
                "Please review the server rules. Continued infractions will result in temporary timeouts and bans."
            )
        elif current_offense_num == 2:
            action_taken = "WARN_2_DM"
            dm_embed.description = (
                "⚠️ **Final Warning**: This is your **2nd moderation infraction** in this server.\n"
                "Any subsequent violations will result in immediate timeouts."
            )
        elif current_offense_num == 3:
            timeout_minutes = settings.first_timeout_minutes
            action_taken = f"TIMEOUT_{timeout_minutes}M"
            dm_embed.description = (
                f"⏱️ **Timeout Applied (3rd Infraction)**: You have been placed on a **{timeout_minutes}-minute timeout**.\n"
                "Further violations will trigger longer timeouts or a permanent server ban."
            )
        else:
            timeout_minutes = settings.subsequent_timeout_minutes
            action_taken = f"TIMEOUT_{timeout_minutes}M"
            dm_embed.description = (
                f"⏱️ **Extended Timeout Applied ({current_offense_num}th Infraction)**: You have been placed on a **{timeout_minutes}-minute timeout**.\n"
                "Please contact a server administrator if you believe this was an error."
            )

        # Apply timeout if applicable (written to Server Audit Log)
        if timeout_minutes > 0 and isinstance(message.author, discord.Member):
            try:
                await message.author.timeout(
                    datetime.timedelta(minutes=timeout_minutes),
                    reason=f"{audit_reason} | Offense #{current_offense_num}",
                )
            except Exception as exc:
                logger.warning("Failed to apply timeout to %s: %s", message.author.id, exc)

        # Deliver Warning DM
        dm_delivered = False
        if isinstance(message.author, discord.Member):
            dm_delivered = await send_user_dm(message.author, dm_embed)

        # 7. Record offense in database
        offense_id, _ = await self.db.record_offense(
            guild_id=guild.id,
            user_id=message.author.id,
            channel_id=message.channel.id,
            message_content=message.content,
            state_summary=state_summary or "",
            classification=choice,
            confidence=confidence,
            noul=noul,
            action_taken=action_taken,
        )

        # 8. Mod-log Channel Logging (Dual Logging with Audit Log)
        if settings.mod_log_channel_id:
            mod_channel = guild.get_channel(settings.mod_log_channel_id)
            if mod_channel and isinstance(mod_channel, discord.TextChannel):
                # Verify bot permissions in channel
                bot_member = guild.me
                perms = mod_channel.permissions_for(bot_member)
                if perms.view_channel and perms.send_messages and perms.embed_links:
                    embed = discord.Embed(
                        title=f"🛡️ TypeSafe Moderation Alert — {tier_label}",
                        color=discord.Color.red() if is_tier1 else discord.Color.orange(),
                        timestamp=datetime.datetime.now(datetime.timezone.utc),
                    )
                    embed.add_field(name="User", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
                    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
                    embed.add_field(name="Offense Stage", value=f"**Offense #{current_offense_num}** ({action_taken})", inline=True)

                    embed.add_field(name="Classification", value=f"`{choice}` (Conf: `{confidence:.1%}`)", inline=True)
                    embed.add_field(name="Noul (Ban Urgency)", value=f"`{noul:.1%}`", inline=True)
                    embed.add_field(name="DM Status", value="Delivered ✅" if dm_delivered else "Undelivered (DMs Closed) ❌", inline=True)

                    embed.add_field(name="Message Content", value=f"```{message.content[:500]}```", inline=False)
                    embed.set_footer(text=f"Offense ID: #{offense_id} • TypeSafe Jev System One")

                    action_view = ModLogActionView(
                        offense_id=offense_id,
                        user_id=message.author.id,
                        guild_id=guild.id,
                        db=self.db,
                    )
                    try:
                        await mod_channel.send(embed=embed, view=action_view)
                    except Exception as exc:
                        logger.warning("Could not send embed to mod-log channel %s: %s", mod_channel.id, exc)

        return True
