from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional
if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

import discord
from config import CONTAINER_FOOTER

__all__ = (
    "ReplyView",
    "ContainerBuilder",
    "create_container",
    "create_container_view",
    "CONTAINER_FOOTER",
)


class ReplyView(discord.ui.LayoutView):
    """Fluent builder and interactive layout view for Discord Components v2 Container."""

    def __init__(
        self,
        body: Optional[str] = None,
        *,
        accent_color: Optional[int] = None,
        accessory: Optional[discord.ui.Item] = None,
        thumbnail_url: Optional[str] = None,
        accent: Optional[int] = None,
        include_footer: bool = True,
        footer_text: Optional[str] = None,
        timeout: Optional[float] = None,
        **kwargs,
    ):
        try:
            super().__init__(timeout=timeout, **kwargs)
        except RuntimeError:
            self._BaseView__timeout = timeout
            self._children = []
            self.id = os.urandom(16).hex()
            self._cache_key = None
            self._BaseView__cancel_callback = None
            self._BaseView__timeout_expiry = None
            self._BaseView__timeout_task = None
            self._BaseView__stopped = None
            self._total_children = 0

        color = accent_color if accent_color is not None else accent
        self._container = discord.ui.Container(accent_color=color)
        self.add_item(self._container)
        if body:
            self.add_body(body, accessory=accessory, thumbnail_url=thumbnail_url)
        if include_footer:
            self.add_footer(footer_text or CONTAINER_FOOTER)

    def stop(self) -> None:
        if getattr(self, "_BaseView__stopped", None) is None:
            try:
                self._BaseView__stopped = asyncio.get_running_loop().create_future()
            except RuntimeError:
                pass
        if getattr(self, "_BaseView__stopped", None) is not None:
            super().stop()

    async def wait(self) -> bool:
        if getattr(self, "_BaseView__stopped", None) is None:
            self._BaseView__stopped = asyncio.get_running_loop().create_future()
        return await super().wait()

    @property
    def container(self) -> discord.ui.Container:
        """Access the underlying discord.ui.Container instance."""
        return self._container

    def add_header_image(self, image_url: str) -> Self:
        """Add a MediaGallery banner to the top of the container."""
        media_item = discord.MediaGalleryItem(image_url)
        header_banner_image = discord.ui.MediaGallery(media_item)
        self._container.add_item(header_banner_image)
        return self

    def add_body(
        self,
        body: str,
        accessory: Optional[discord.ui.Item] = None,
        thumbnail_url: Optional[str] = None,
    ) -> Self:
        """Add a markdown section or text display with optional thumbnail or accessory."""
        text_display = discord.ui.TextDisplay(body)

        chosen_accessory = accessory
        if chosen_accessory is None and thumbnail_url:
            ThumbnailClass = getattr(discord.ui, "Thumbnail", None)
            if ThumbnailClass is not None:
                try:
                    chosen_accessory = ThumbnailClass(thumbnail_url)
                except Exception:
                    try:
                        chosen_accessory = ThumbnailClass(media=thumbnail_url)
                    except Exception:
                        chosen_accessory = None

            if chosen_accessory is None:
                ImageClass = getattr(discord.ui, "Image", None)
                if ImageClass is not None:
                    try:
                        chosen_accessory = ImageClass(thumbnail_url)
                    except Exception:
                        chosen_accessory = None

        if chosen_accessory is not None:
            self._container.add_item(discord.ui.Section(text_display, accessory=chosen_accessory))
        else:
            self._container.add_item(text_display)
        return self

    def add_section(
        self,
        body: str,
        accessory: Optional[discord.ui.Item] = None,
        thumbnail_url: Optional[str] = None,
    ) -> Self:
        """Alias for add_body to build sections ergonomically."""
        return self.add_body(body, accessory=accessory, thumbnail_url=thumbnail_url)

    def add_separator(
        self,
        spacing: discord.SeparatorSpacing = discord.SeparatorSpacing.small,
        visible: bool = True,
    ) -> Self:
        """Insert a separator item into the container with configurable spacing and divider visibility."""
        self._container.add_item(discord.ui.Separator(spacing=spacing, visible=visible))
        return self

    def add_footer(self, text: str = CONTAINER_FOOTER, visible: bool = False) -> Self:
        """Append a footer separated by a subtle spacing separator."""
        self._container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small, visible=visible))
        self._container.add_item(discord.ui.TextDisplay(text))
        return self

    def add_action_row(self, *items: discord.ui.Item) -> Self:
        """Add interactive items inside the container."""
        action_row = discord.ui.ActionRow(*items)
        self._container.add_item(action_row)
        return self

    def add_outside_action_row(self, *items: discord.ui.Item) -> Self:
        """Add interactive items to the outer LayoutView below the container."""
        action_row = discord.ui.ActionRow(*items)
        self.add_item(action_row)
        return self

    def credits(self) -> Self:
        """Append default branding footer."""
        return self.add_footer()


ContainerBuilder = ReplyView


def create_container(
    *,
    image_url: Optional[str] = None,
    body: Optional[str] = None,
    sections: Optional[list[str]] = None,
    accessory: Optional[discord.ui.Item] = None,
    accent_color: Optional[int] = None,
    include_footer: bool = True,
    thumbnail_url: Optional[str] = None,
    footer_text: Optional[str] = None,
    separator_visible: bool = True,
    separator_spacing: discord.SeparatorSpacing = discord.SeparatorSpacing.small,
) -> discord.ui.Container:
    """Create a discord.ui.Container with optional header image, body or multi-section layout.

    The footer defaults to the global branding text, but callers may override it
    by providing their own footer_text while keeping the layout consistent.

    Parameters
    - image_url: optional URL to show as a media gallery header
    - body: markdown/text shown in the main section (used if sections is None)
    - sections: optional list of markdown/text sections separated by spacing Separators
    - accessory: an optional ui.Item to attach as an accessory to the body section
    - separator_visible: whether separators between sections show a divider line (defaults to True)
    - separator_spacing: spacing around the separators (SeparatorSpacing.small or large)

    Returns a discord.ui.Container ready to be added to a LayoutView.
    """
    builder = ReplyView(
        accent_color=accent_color,
        include_footer=False,
    )

    if image_url:
        builder.add_header_image(image_url)

    all_sections: list[str] = []
    if sections:
        all_sections.extend(sections)
    elif body:
        all_sections.append(body)

    if all_sections:
        builder.add_body(all_sections[0], accessory=accessory, thumbnail_url=thumbnail_url)
        for sec in all_sections[1:]:
            if sec and sec.strip():
                builder.add_separator(spacing=separator_spacing, visible=separator_visible)
                builder.add_body(sec)

    if include_footer:
        builder.add_footer(footer_text or CONTAINER_FOOTER, visible=separator_visible)

    return builder._container


def create_container_view(
    container: discord.ui.Container,
    *,
    timeout: Optional[float] = None,
) -> discord.ui.LayoutView:
    """Create a discord.ui.LayoutView containing a single container ready for sending."""
    view = discord.ui.LayoutView(timeout=timeout)
    view.add_item(container)
    return view
