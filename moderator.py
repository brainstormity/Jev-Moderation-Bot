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
from container import create_container, create_container_view

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
        state_lines.append("\n=== RECENT SERVER SAFE PRECEDENTS (FALSE POSITIVES TO LEARN FROM) ===")
        for idx, flag in enumerate(recent_false_flags, 1):
            clean_flag = flag.replace("\n", " ").strip()
            state_lines.append(f"Precedent #{idx}: {clean_flag}")

    state_lines.extend([
        "\n=== MESSAGE EVALUATION TARGET ===",
        message.content,
    ])

    return "\n".join(state_lines)


async def send_user_dm(
    member: discord.Member,
    content_item: discord.ui.Container | discord.ui.LayoutView | discord.Embed,
) -> bool:
    """Safely deliver a direct warning message to a user, handling closed DMs gracefully."""
    try:
        if isinstance(content_item, discord.ui.Container):
            view = create_container_view(content_item)
            await member.send(view=view)
        elif isinstance(content_item, discord.ui.LayoutView):
            await member.send(view=content_item)
        elif isinstance(content_item, discord.Embed):
            await member.send(embed=content_item)
        else:
            await member.send(content=str(content_item))
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
            timestamp = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            resolved_container = create_container(
                body=(
                    f"## 🟢 [PARDONED AS FALSE FLAG] Moderation Alert\n"
                    f"**Offense ID**: `#{self.offense_id}` | **Target User**: <@{self.user_id}>\n\n"
                    f"### 📝 Resolution\n"
                    f"Pardoned by {interaction.user.mention} (<t:{timestamp}:R>).\n"
                    f"*Precedent saved to improve Jev AI runtime memory.*"
                ),
                accent_color=0x57F287,
                footer_text=f"Offense ID: #{self.offense_id} • TypeSafe Jev System One • Pardoned",
            )
            resolved_view = create_container_view(resolved_container)
            if self.parent_message.embeds:
                old_embed = self.parent_message.embeds[0]
                resolved_embed = old_embed.copy()
                resolved_embed.color = discord.Color.green()
                resolved_embed.title = f"🟢 [PARDONED AS FALSE FLAG] {old_embed.title or 'Moderation Alert'}"
                resolved_embed.add_field(
                    name="Resolution",
                    value=f"Pardoned by {interaction.user.mention} (<t:{timestamp}:R>).\n"
                          f"*Precedent saved to improve Jev AI runtime memory.*",
                    inline=False,
                )
                await self.parent_message.edit(embed=resolved_embed, view=resolved_view)
            else:
                await self.parent_message.edit(view=resolved_view)
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
            timestamp = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            resolved_container = create_container(
                body=(
                    f"## 🔴 [USER PERMANENTLY BANNED] Moderation Alert\n"
                    f"**Offense ID**: `#{self.offense_id}` | **Target User**: <@{self.user_id}>\n\n"
                    f"### 📝 Resolution\n"
                    f"Permanently banned by {interaction.user.mention} (<t:{timestamp}:R>)."
                ),
                accent_color=0xED4245,
                footer_text=f"Offense ID: #{self.offense_id} • TypeSafe Jev System One • Banned",
            )
            resolved_view = create_container_view(resolved_container)
            if self.parent_message.embeds:
                old_embed = self.parent_message.embeds[0]
                resolved_embed = old_embed.copy()
                resolved_embed.color = discord.Color.dark_red()
                resolved_embed.title = f"🔴 [USER PERMANENTLY BANNED] {old_embed.title or 'Moderation Alert'}"
                resolved_embed.add_field(
                    name="Resolution",
                    value=f"Permanently banned by {interaction.user.mention} (<t:{timestamp}:R>).",
                    inline=False,
                )
                await self.parent_message.edit(embed=resolved_embed, view=resolved_view)
            else:
                await self.parent_message.edit(view=resolved_view)
        except Exception as exc:
            logger.warning("Could not update parent mod-log message: %s", exc)

        await interaction.response.edit_message(
            content=f"🔨 **User <@{self.user_id}> has been permanently banned.**",
            view=None,
        )

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_ban(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.edit_message(content="Action cancelled. User was not banned.", view=None)


class ModLogActionView(ui.LayoutView):
    """Action view attached to #mod-log Components v2 Container, restricted strictly to Administrators."""

    def __init__(
        self,
        offense_id: int,
        user_id: int,
        guild_id: int,
        container: Optional[discord.ui.Container] = None,
        db: Database = db_instance,
    ) -> None:
        super().__init__(timeout=None)  # Persistent view
        self.offense_id = offense_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.container = container
        self.db = db

        if self.container is not None:
            self.add_item(self.container)

        self.pardon_button = ui.Button(
            label="Pardon (False Flag)",
            style=discord.ButtonStyle.success,
            emoji="🟢",
            custom_id="modlog_pardon",
        )
        self.pardon_button.callback = self._pardon_callback

        self.ban_button = ui.Button(
            label="Ban User",
            style=discord.ButtonStyle.danger,
            emoji="🔴",
            custom_id="modlog_ban",
        )
        self.ban_button.callback = self._ban_callback

        action_row = ui.ActionRow()
        action_row.add_item(self.pardon_button)
        action_row.add_item(self.ban_button)
        self.add_item(action_row)

    async def _pardon_callback(self, interaction: discord.Interaction) -> None:
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

    async def _ban_callback(self, interaction: discord.Interaction) -> None:
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
            content="🔨 **Confirm Permanent Server Ban**\n"
                    f"Are you sure you want to permanently ban <@{self.user_id}> from the server?\n"
                    "• This action cannot be undone automatically.\n"
                    "• Escalates the offense record to permanent BAN.",
            view=confirm_view,
            ephemeral=True,
        )


class MessageModerator:
    """Core message moderation pipeline integrating TypeSafe AI and progressive escalation."""

    def __init__(
        self,
        client: AsyncTypeSafe,
        db: Database = db_instance,
        consecutive_failure_threshold: int = 3,
    ) -> None:
        self.client = client
        self.db = db
        self.consecutive_failure_threshold = consecutive_failure_threshold
        self._guild_consecutive_failures: Dict[int, int] = {}
        self._guild_outage_alerted: Dict[int, bool] = {}

    async def _handle_evaluation_failure(
        self, guild: discord.Guild, settings: GuildSettings, exc: Exception
    ) -> None:
        """Track consecutive failures and notify mod-log channel once threshold is reached."""
        failures = self._guild_consecutive_failures.get(guild.id, 0) + 1
        self._guild_consecutive_failures[guild.id] = failures

        if failures >= self.consecutive_failure_threshold and not self._guild_outage_alerted.get(guild.id, False):
            self._guild_outage_alerted[guild.id] = True
            logger.warning(
                "Guild %s reached %s consecutive TypeSafe AI evaluation failures. Dispatching outage warning.",
                guild.id,
                failures,
            )
            if settings.mod_log_channel_id:
                mod_channel = guild.get_channel(settings.mod_log_channel_id)
                if mod_channel and isinstance(mod_channel, discord.TextChannel):
                    bot_member = guild.me
                    perms = mod_channel.permissions_for(bot_member)
                    if perms.view_channel and perms.send_messages:
                        error_type = type(exc).__name__
                        error_detail = str(exc)[:200]
                        outage_body = (
                            "## ⚠️ TypeSafe AI Moderation Service Outage\n"
                            f"Automated moderation has failed for **{failures} consecutive messages**.\n\n"
                            "• **Status**: Failing Open (messages are allowed through unmoderated to prevent false deletions)\n"
                            f"• **Last Error**: `{error_type}`: {error_detail}\n\n"
                            "Please verify your `TYPESAFE_API_KEY`, API rate limits, or TypeSafe system status. "
                            "A recovery notice will be posted here once evaluations succeed again."
                        )
                        container = create_container(
                            body=outage_body,
                            accent_color=0xED4245,
                            footer_text="TypeSafe AI Outage Alert • Jev Moderation",
                        )
                        view = create_container_view(container)
                        try:
                            await mod_channel.send(view=view)
                        except Exception as send_exc:
                            logger.warning("Could not send outage warning to mod-log channel %s: %s", mod_channel.id, send_exc)

    async def _handle_evaluation_success(
        self, guild: discord.Guild, settings: GuildSettings
    ) -> None:
        """Reset consecutive failures and notify mod-log if recovering from an outage."""
        was_alerted = self._guild_outage_alerted.get(guild.id, False)
        self._guild_consecutive_failures[guild.id] = 0

        if was_alerted:
            self._guild_outage_alerted[guild.id] = False
            logger.info("TypeSafe AI evaluation recovered for guild %s. Dispatching recovery notice.", guild.id)
            if settings.mod_log_channel_id:
                mod_channel = guild.get_channel(settings.mod_log_channel_id)
                if mod_channel and isinstance(mod_channel, discord.TextChannel):
                    bot_member = guild.me
                    perms = mod_channel.permissions_for(bot_member)
                    if perms.view_channel and perms.send_messages:
                        recovery_body = (
                            "## ✅ TypeSafe AI Moderation Service Restored\n"
                            "TypeSafe AI message evaluations are succeeding normally again.\n\n"
                            "• **Status**: Operational\n"
                            "• **Automated Moderation**: Active"
                        )
                        container = create_container(
                            body=recovery_body,
                            accent_color=0x57F287,
                            footer_text="TypeSafe AI Status Restored • Jev Moderation",
                        )
                        view = create_container_view(container)
                        try:
                            await mod_channel.send(view=view)
                        except Exception as send_exc:
                            logger.warning("Could not send recovery notice to mod-log channel %s: %s", mod_channel.id, send_exc)

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
            # Note: High scores on requires_immediate_ban escalate the infraction to Tier 1
            # and present an interactive 1-click ban button in #mod-log for administrator confirmation.
            # Automated bans are never unassisted; punishment decisions remain strictly administrator-governed.
            Noul(
                id="requires_immediate_ban",
                description="Is this an explicit malicious scam attempt that warrants immediate administrative ban review?",
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
            await self._handle_evaluation_failure(guild, settings, exc)
            return False

        if not response:
            return False

        # 4. Handle recovery notice if service was previously in an outage state
        await self._handle_evaluation_success(guild, settings)

        # 4. Extract classification results
        spam_result = response.questions.get("spam_classification")
        ban_result = response.questions.get("requires_immediate_ban")

        if not spam_result:
            return False

        choice = spam_result.choice or "LEGITIMATE"
        confidence = spam_result.confidence
        noul = getattr(ban_result, "noul", 0.0) if ban_result else 0.0

        # Calculate combined threat probability from probabilities dictionary
        probs = getattr(spam_result, "probabilities", {}) or {}
        if probs and ("SPAM" in probs or "SCAM_LINK" in probs):
            threat_prob = float(probs.get("SPAM", 0.0) + probs.get("SCAM_LINK", 0.0))
        else:
            threat_prob = confidence if choice in ["SPAM", "SCAM_LINK"] else 0.0

        # Tier Decision Logic: use threat_prob for sensitivity thresholds
        is_tier1 = (threat_prob >= settings.tier1_threshold and choice in ["SPAM", "SCAM_LINK"]) or (noul >= settings.tier1_threshold)
        is_tier2 = (settings.tier2_threshold <= threat_prob < settings.tier1_threshold and choice in ["SPAM", "SCAM_LINK"]) and not is_tier1

        # Tier 3 — Low Confidence / Legitimate: Allow through
        if not is_tier1 and not is_tier2:
            return False

        # 5. Execute Action: Delete message immediately with Audit Log reason
        tier_label = "Tier 1 (High Threat)" if is_tier1 else "Tier 2 (Medium Threat)"
        audit_reason = f"TypeSafe AI {tier_label}: {choice} (Threat: {threat_prob:.2f}, Conf: {confidence:.2f}, Noul: {noul:.2f})"
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound) as exc:
            logger.warning("Could not delete message %s: %s", message.id, exc)

        # 6. Progressive Escalation Ladder based on user's active offense count
        prior_active_count = await self.db.get_active_offense_count(guild.id, message.author.id)
        current_offense_num = prior_active_count + 1

        action_taken = ""
        timeout_minutes = 0
        accent_color = 0xED4245 if is_tier1 else 0xE67E22

        if current_offense_num == 1:
            action_taken = "WARN_1_DM"
            warning_headline = (
                "⚠️ **First Warning**: Your message was flagged by automated moderation for spam/scam and removed.\n"
                "Please review the server rules. Continued infractions will result in temporary timeouts and bans."
            )
        elif current_offense_num == 2:
            action_taken = "WARN_2_DM"
            warning_headline = (
                "⚠️ **Final Warning**: This is your **2nd moderation infraction** in this server.\n"
                "Any subsequent violations will result in immediate timeouts."
            )
        elif current_offense_num == 3:
            timeout_minutes = settings.first_timeout_minutes
            action_taken = f"TIMEOUT_{timeout_minutes}M"
            warning_headline = (
                f"⏱️ **Timeout Applied (3rd Infraction)**: You have been placed on a **{timeout_minutes}-minute timeout**.\n"
                "Further violations will trigger longer timeouts or a permanent server ban."
            )
        else:
            timeout_minutes = settings.subsequent_timeout_minutes
            action_taken = f"TIMEOUT_{timeout_minutes}M"
            warning_headline = (
                f"⏱️ **Extended Timeout Applied ({current_offense_num}th Infraction)**: You have been placed on a **{timeout_minutes}-minute timeout**.\n"
                "Please contact a server administrator if you believe this was an error."
            )

        dm_body = (
            f"## 🛡️ Message Removed in {guild.name}\n"
            f"{warning_headline}\n\n"
            f"• **Violation Category**: `{choice}`\n"
            f"• **Detection Severity**: {tier_label}\n\n"
            f"**Removed Message**:\n```{message.content[:200]}```"
        )
        dm_container = create_container(
            body=dm_body,
            accent_color=accent_color,
            footer_text=f"Server: {guild.name} • Offense #{current_offense_num}",
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
            dm_delivered = await send_user_dm(message.author, dm_container)

        # 7. Record offense in database
        offense_id, _ = await self.db.record_offense(
            guild_id=guild.id,
            user_id=message.author.id,
            channel_id=message.channel.id,
            message_content=message.content,
            state_summary=state_summary or "",
            classification=choice,
            confidence=threat_prob,
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
                if perms.view_channel and perms.send_messages:
                    dm_status_str = "Delivered ✅" if dm_delivered else "Undelivered (DMs Closed) ❌"
                    clean_content = message.content.replace("```", "")[:500]
                    alert_body = (
                        f"## 🛡️ TypeSafe Moderation Alert — {tier_label}\n"
                        f"• **User**: {message.author.mention} (`{message.author.id}`)\n"
                        f"• **Channel**: {message.channel.mention}\n"
                        f"• **Offense Stage**: **Offense #{current_offense_num}** ({action_taken})\n"
                        f"• **Classification**: `{choice}` (Threat: `{threat_prob:.1%}`, Conf: `{confidence:.1%}`)\n"
                        f"• **Noul (Ban Urgency)**: `{noul:.1%}`\n"
                        f"• **DM Status**: {dm_status_str}\n\n"
                        f"**Message Content**:\n```{clean_content}```"
                    )
                    alert_container = create_container(
                        body=alert_body,
                        accent_color=0xED4245 if is_tier1 else 0xE67E22,
                        footer_text=f"Offense ID: #{offense_id} • TypeSafe Jev System One",
                    )

                    action_view = ModLogActionView(
                        offense_id=offense_id,
                        user_id=message.author.id,
                        guild_id=guild.id,
                        container=alert_container,
                        db=self.db,
                    )
                    try:
                        await mod_channel.send(view=action_view)
                    except Exception as exc:
                        logger.warning("Could not send alert to mod-log channel %s: %s", mod_channel.id, exc)

        return True
