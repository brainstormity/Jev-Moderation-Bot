"""Interactive Discord UI views and channel history scraping for User Profiling.

Includes:
1. scrape_channel_history: Optimized channel traversal with early break and bulk DB backfill.
2. ChannelSelectFallbackView: ChannelSelect dropdown shown when DB has insufficient history.
3. ProfileReportView: Action buttons (View Messages, Rescan Channel, Quick Timeout).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import ui

from database import Database, db_instance
from profiler import UserProfileData, build_profile_container, evaluate_user_profile
from typesafe import AsyncTypeSafe
from container import create_container

logger = logging.getLogger("profiler.views")


async def scrape_channel_history(
    channel: discord.TextChannel,
    target_user_id: int,
    count: int = 25,
    max_scan: int = 300,
    db: Database = db_instance,
) -> Tuple[List[Dict[str, Any]], int]:
    """Scrape recent channel history with early loop termination and opportunistic bulk backfill.

    - Collects ALL non-bot messages from ALL users into a bulk batch.
    - Tracks target user messages and terminates immediately once `count` is satisfied.
    - Saves all collected messages into SQLite via `save_user_messages_bulk`.
    - Returns (target_user_messages, total_messages_cached).
    """
    bulk_tuples: List[Tuple[int, int, int, int, str, str]] = []
    target_messages: List[Dict[str, Any]] = []

    guild_id = channel.guild.id
    channel_id = channel.id

    try:
        async for msg in channel.history(limit=max_scan, oldest_first=False):
            # Skip bot messages and empty messages
            if msg.author.bot or not msg.content.strip():
                continue

            created_iso = msg.created_at.isoformat()
            bulk_tuples.append(
                (guild_id, channel_id, msg.author.id, msg.id, msg.content, created_iso)
            )

            if msg.author.id == target_user_id:
                target_messages.append({
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "user_id": msg.author.id,
                    "message_id": msg.id,
                    "content": msg.content,
                    "created_at": created_iso,
                })
                if len(target_messages) >= count:
                    break  # Early termination: target count achieved!

    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.warning("Error fetching history from channel %s: %s", channel.id, exc)

    # Opportunistically persist all scanned messages across all users
    if bulk_tuples:
        await db.save_user_messages_bulk(bulk_tuples)

    return target_messages, len(bulk_tuples)


class SampledMessagesPaginationView(ui.LayoutView):
    """Ephemeral view displaying paginated sampled messages using Components v2 Container."""

    def __init__(self, messages: List[Dict[str, Any]], member_name: str) -> None:
        super().__init__(timeout=120)
        self.messages = messages
        self.member_name = member_name
        self.page = 0
        self.page_size = 5
        self.max_pages = max(1, (len(messages) + self.page_size - 1) // self.page_size)

        self.prev_btn = ui.Button(label="◀ Previous", style=discord.ButtonStyle.secondary, custom_id="msg_prev")
        self.next_btn = ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="msg_next")
        self.prev_btn.callback = self._on_prev
        self.next_btn.callback = self._on_next

        self._refresh()

    def get_current_container(self) -> discord.ui.Container:
        start = self.page * self.page_size
        end = start + self.page_size
        slice_msgs = self.messages[start:end]

        body = (
            f"## 📜 Sampled Messages — {self.member_name}\n"
            f"Showing messages `{start + 1}` to `{min(end, len(self.messages))}` of `{len(self.messages)}` total:\n"
        )
        for idx, m in enumerate(slice_msgs, start=start + 1):
            ts = m.get("created_at", "Unknown")
            clean_content = m.get("content", "").replace("```", "")[:250]
            body += f"\n**#{idx} • Channel <#{m.get('channel_id', 'unknown')}> • {ts}**\n```{clean_content}```\n"

        return create_container(
            body=body,
            accent_color=0x2B2D31,
            footer_text=f"Page {self.page + 1}/{self.max_pages}",
        )


    def _refresh(self) -> None:
        self.clear_items()
        self.add_item(self.get_current_container())

        self.prev_btn.disabled = (self.page == 0)
        self.next_btn.disabled = (self.page >= self.max_pages - 1)

        row = ui.ActionRow()
        row.add_item(self.prev_btn)
        row.add_item(self.next_btn)
        self.add_item(row)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.max_pages - 1, self.page + 1)
        self._refresh()
        await interaction.response.edit_message(view=self)


class ChannelSelectFallbackView(ui.View):
    """Interactive view allowing the admin to select a channel when local cache is insufficient."""

    def __init__(
        self,
        target_member: discord.Member,
        requested_count: int,
        client: AsyncTypeSafe,
        db: Database = db_instance,
        existing_cached_count: int = 0,
    ) -> None:
        super().__init__(timeout=180)
        self.target_member = target_member
        self.requested_count = requested_count
        self.client = client
        self.db = db
        self.existing_cached_count = existing_cached_count

        # Configure channel select
        self.channel_select = ui.ChannelSelect(
            placeholder="🔍 Select a channel to fetch message history...",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=1,
        )
        self.channel_select.callback = self.on_channel_selected
        self.add_item(self.channel_select)

    async def on_channel_selected(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ Only moderators can run this action.", ephemeral=True)
            return

        selected_channel = self.channel_select.values[0]
        # Resolve channel object
        channel = interaction.guild.get_channel(selected_channel.id) if interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("❌ Selected channel is invalid or inaccessible.", ephemeral=True)
            return

        # Check bot permissions in channel
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.read_message_history or not perms.view_channel:
            await interaction.response.send_message(
                f"❌ The bot lacks `Read Message History` permission in {channel.mention}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # Scrape and backfill
        target_msgs, total_cached = await scrape_channel_history(
            channel=channel,
            target_user_id=self.target_member.id,
            count=self.requested_count,
            max_scan=300,
            db=self.db,
        )

        # Retrieve combined user messages from DB
        all_messages = await self.db.get_user_recent_messages(
            interaction.guild.id, self.target_member.id, limit=self.requested_count
        )

        prior_offenses = await self.db.get_user_offenses(interaction.guild.id, self.target_member.id, limit=10)
        settings = await self.db.get_guild_settings(interaction.guild.id)

        # Evaluate profile
        profile = await evaluate_user_profile(
            client=self.client,
            member=self.target_member,
            messages=all_messages,
            prior_offenses=prior_offenses,
            model=settings.model_override,
        )

        report_view = ProfileReportView(
            profile=profile,
            target_member=self.target_member,
            client=self.client,
            guild=interaction.guild,
            db=self.db,
            requested_count=self.requested_count,
        )

        await interaction.edit_original_response(
            content=f"✅ Fetched history from {channel.mention} (found `{len(target_msgs)}` user messages, cached `{total_cached}` total messages across all users).",
            view=report_view,
        )


class ProfileReportView(ui.LayoutView):
    """Action view attached to the final dossier Components v2 Container."""

    def __init__(
        self,
        profile: UserProfileData,
        target_member: discord.Member,
        client: AsyncTypeSafe,
        guild: Optional[discord.Guild] = None,
        db: Database = db_instance,
        requested_count: int = 25,
        container: Optional[discord.ui.Container] = None,
    ) -> None:
        super().__init__(timeout=300)
        self.profile = profile
        self.target_member = target_member
        self.client = client
        self.guild = guild or (target_member.guild if hasattr(target_member, "guild") else None)
        self.db = db
        self.requested_count = requested_count

        if container is not None:
            self.container = container
        elif self.guild:
            self.container = build_profile_container(self.profile, self.target_member, self.guild)
        else:
            self.container = create_container(
                body=f"## 👤 Member Dossier — {self.target_member.display_name}\n{self.profile.summary}"
            )

        self.add_item(self.container)

        self.view_messages_btn = ui.Button(label="View Messages", style=discord.ButtonStyle.secondary, emoji="📜")
        self.view_messages_btn.callback = self.view_messages_callback
        self.rescan_btn = ui.Button(label="Scan Another Channel", style=discord.ButtonStyle.primary, emoji="🔄")
        self.rescan_btn.callback = self.rescan_callback

        action_row = ui.ActionRow()
        action_row.add_item(self.view_messages_btn)
        action_row.add_item(self.rescan_btn)
        self.add_item(action_row)

    async def view_messages_callback(self, interaction: discord.Interaction) -> None:
        if not self.profile.sampled_messages:
            await interaction.response.send_message("ℹ️ No sampled messages available to display.", ephemeral=True)
            return

        paginator = SampledMessagesPaginationView(
            messages=self.profile.sampled_messages,
            member_name=self.target_member.display_name,
        )
        await interaction.response.send_message(
            view=paginator,
            ephemeral=True,
        )

    async def rescan_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ Only moderators can use this action.", ephemeral=True)
            return

        fallback_view = ChannelSelectFallbackView(
            target_member=self.target_member,
            requested_count=self.requested_count,
            client=self.client,
            db=self.db,
            existing_cached_count=len(self.profile.sampled_messages),
        )
        await interaction.response.send_message(
            content=f"🔍 Select an additional channel to fetch more message history for {self.target_member.mention}:",
            view=fallback_view,
            ephemeral=True,
        )
