from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest
import discord
from container import ReplyView, ContainerBuilder, create_container, create_container_view
from config import CONTAINER_FOOTER
from moderator import send_user_dm, ModLogActionView
from profile_views import SampledMessagesPaginationView


def test_create_container_basic():
    container = create_container(
        body="**Hello Discord Components v2**",
        accent_color=0x5865F2,
    )
    assert isinstance(container, discord.ui.Container)
    # Check components
    assert len(container.children) >= 2  # TextDisplay, Separator, TextDisplay footer


def test_create_container_with_image_and_footer():
    container = create_container(
        image_url="https://example.com/banner.png",
        body="Message body",
        footer_text="Custom Footer Text",
    )
    # Check children
    assert len(container.children) >= 3


def test_create_container_with_thumbnail():
    container = create_container(
        body="User Profile Summary",
        thumbnail_url="https://example.com/avatar.png",
        accent_color=0x00FF00,
    )
    assert len(container.children) >= 2


def test_create_container_without_footer():
    container = create_container(
        body="No footer body",
        include_footer=False,
    )
    assert isinstance(container, discord.ui.Container)
    # Check that footer was not added
    assert not any(getattr(c, "content", "") == CONTAINER_FOOTER for c in container.children)


@pytest.mark.asyncio
async def test_create_container_view():
    container = create_container(body="Test View Container")
    view = create_container_view(container)
    assert isinstance(view, discord.ui.LayoutView)
    assert view.has_components_v2() is True
    comps = view.to_components()
    assert len(comps) == 1
    assert comps[0]["type"] == 17  # Container type


@pytest.mark.asyncio
async def test_send_user_dm_with_container():
    member = AsyncMock(spec=discord.Member)
    member.send = AsyncMock()

    container = create_container(body="Direct Message Warning Container", accent_color=0xED4245)
    success = await send_user_dm(member, container)
    assert success is True
    member.send.assert_called_once()
    kwargs = member.send.call_args[1]
    assert "view" in kwargs
    assert isinstance(kwargs["view"], discord.ui.LayoutView)
    comps = kwargs["view"].to_components()
    assert comps[0]["type"] == 17


@pytest.mark.asyncio
async def test_mod_log_action_view_with_container():
    container = create_container(body="Alert Container", accent_color=0xED4245)
    view = ModLogActionView(offense_id=10, user_id=123, guild_id=456, container=container)
    assert isinstance(view, discord.ui.LayoutView)
    comps = view.to_components()
    # Should have container and action row
    assert len(comps) == 2
    assert comps[0]["type"] == 17  # Container
    assert comps[1]["type"] == 1   # ActionRow
    assert len(comps[1]["components"]) == 2  # Pardon and Ban buttons


@pytest.mark.asyncio
async def test_sampled_messages_pagination_view():
    sample_msgs = [
        {"content": f"Message {i}", "created_at": "2026-09-18T01:00:00", "channel_id": 100}
        for i in range(7)
    ]
    paginator = SampledMessagesPaginationView(messages=sample_msgs, member_name="Tester")
    assert isinstance(paginator, discord.ui.LayoutView)
    comps = paginator.to_components()
    assert len(comps) == 2
    assert comps[0]["type"] == 17
    assert comps[1]["type"] == 1


def test_create_container_with_sections():
    sections = [
        "Header Section",
        "Metadata Section",
        "Radar Section",
        "Synthesis Section",
    ]
    container = create_container(sections=sections, thumbnail_url="https://example.com/pic.png")
    # First section has thumbnail Section, followed by 3 sections with separators + footer spacer + footer
    assert len(container.children) == 9
    types = [type(c).__name__ for c in container.children]
    assert types == [
        "Section",
        "Separator",
        "TextDisplay",
        "Separator",
        "TextDisplay",
        "Separator",
        "TextDisplay",
        "Separator",
        "TextDisplay",
    ]


def test_reply_view_builder_with_separators():
    """Verify ReplyView fluent chaining with add_separator in between sections."""
    from reply import ReplyView as ImportedReplyView, ContainerBuilder as ImportedBuilder
    assert ImportedReplyView is ReplyView
    assert ImportedBuilder is ContainerBuilder

    view = ReplyView(
        body="## Header Section",
        thumbnail_url="https://example.com/avatar.png",
        accent_color=None,
        include_footer=False,
    )

    # Test fluent return of Self
    chained = view.add_separator(spacing=discord.SeparatorSpacing.small, visible=True)
    assert chained is view

    view.add_body("### Metadata Section")
    view.add_separator(spacing=discord.SeparatorSpacing.large, visible=False)
    view.add_section("### Radar Section")
    view.add_footer("Custom footer text", visible=True)

    assert isinstance(view.container, discord.ui.Container)
    assert view._container is view.container
    assert len(view._container.children) >= 6

    # Verify separator properties
    separators = [c for c in view._container.children if isinstance(c, discord.ui.Separator)]
    assert len(separators) == 3
    assert separators[0].spacing == discord.SeparatorSpacing.small
    assert separators[0].visible is True
    assert separators[1].spacing == discord.SeparatorSpacing.large
    assert separators[1].visible is False


def test_reply_view_action_rows():
    """Verify add_action_row and add_outside_action_row."""
    view = ReplyView("Test Body", include_footer=False)
    btn1 = discord.ui.Button(label="Inside")
    btn2 = discord.ui.Button(label="Outside")

    view.add_action_row(btn1)
    view.add_outside_action_row(btn2)

    # Inside container
    container_rows = [c for c in view._container.children if isinstance(c, discord.ui.ActionRow)]
    assert len(container_rows) == 1
    assert container_rows[0].children[0] is btn1

    # Outside container
    outside_rows = [c for c in view.children if isinstance(c, discord.ui.ActionRow)]
    assert len(outside_rows) == 1
    assert outside_rows[0].children[0] is btn2

