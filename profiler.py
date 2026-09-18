"""User behavioral profiling engine using TypeSafe AI (Jev System One).

Analyzes recent user message history across channels to build a multidimensional
behavioral profile assessing Noobness, Spamminess, Scam Risk, Toxicity, and Community Value.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import discord

from typesafe import AsyncTypeSafe, Choice, Noul, TypeSafeEvaluationResponse
from container import ReplyView

logger = logging.getLogger("profiler")


@dataclass
class UserProfileData:
    """Encapsulates the analyzed behavioral scores and archetype classification for a user."""
    user_id: int
    display_name: str
    account_age_days: int
    server_age_days: int
    sampled_message_count: int
    prior_offense_count: int
    scam_score: float = 0.0
    spam_score: float = 0.0
    noob_score: float = 0.0
    toxic_score: float = 0.0
    helpful_score: float = 0.0
    persona: str = "CASUAL_CHATTER"
    persona_confidence: float = 0.0
    recommended_action: str = "NO_ACTION"
    action_confidence: float = 0.0
    summary: str = ""
    sampled_messages: List[Dict[str, Any]] = field(default_factory=list)


def render_progress_bar(val: float, length: int = 10) -> str:
    """Generate a high-contrast visual ASCII progress bar."""
    clamped = max(0.0, min(1.0, float(val)))
    filled = int(round(clamped * length))
    empty = length - filled
    pct = int(clamped * 100)
    bar = "█" * filled + "░" * empty
    return f"`[{bar}]` **{pct}%**"


def build_profile_state(
    member: discord.Member,
    messages: List[Dict[str, Any]],
    prior_offenses: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build a structured prompt state representing the member's profile and message history."""
    now = datetime.datetime.now(datetime.timezone.utc)
    account_age_days = (now - member.created_at).days
    server_age_days = (now - member.joined_at).days if member.joined_at else 0
    offenses = prior_offenses or []

    lines = [
        "=== DISCORD USER AUDIT DOSSIER ===",
        f"Member: {member.name} (Display: {member.display_name}, ID: {member.id})",
        f"Account Created: {member.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')} ({account_age_days} days ago)",
        f"Joined Server: {member.joined_at.strftime('%Y-%m-%d %H:%M:%S UTC') if member.joined_at else 'Unknown'} ({server_age_days} days ago)",
        f"Server Roles: {', '.join([r.name for r in member.roles if r.name != '@everyone']) or 'None'}",
        f"Prior Moderation Infractions: {len(offenses)}",
    ]

    if offenses:
        lines.append("\n=== PRIOR RECORDED INFRACTIONS IN SERVER ===")
        for idx, off in enumerate(offenses[:5], 1):
            created = off.get("created_at", "Unknown")
            action = off.get("action_taken", "UNKNOWN")
            classification = off.get("classification", "UNKNOWN")
            clean_text = off.get("message_content", "").replace("\n", " ")[:100]
            lines.append(f"{idx}. [{created}] Action: {action} | Class: {classification} | Snippet: \"{clean_text}\"")

    lines.append(f"\n=== RECENT USER MESSAGES ({len(messages)} SAMPLED, NEWEST FIRST) ===")
    if not messages:
        lines.append("(No messages available for evaluation)")
    else:
        for idx, msg in enumerate(messages, 1):
            ts = msg.get("created_at", "Unknown Timestamp")
            cid = msg.get("channel_id", "unknown")
            content = str(msg.get("content", "")).replace("\n", " ").strip()
            lines.append(f"[{idx}] [{ts}] (Channel ID: {cid}): \"{content}\"")

    lines.append("\n=== EVALUATION TASK ===")
    lines.append("Analyze this user's communication style, intent, risk factors, and value to the Discord server.")
    return "\n".join(lines)


async def evaluate_user_profile(
    client: AsyncTypeSafe,
    member: discord.Member,
    messages: List[Dict[str, Any]],
    prior_offenses: Optional[List[Dict[str, Any]]] = None,
    model: Optional[str] = None,
) -> UserProfileData:
    """Evaluate a user's messages using TypeSafe AI Jev System One questions."""
    now = datetime.datetime.now(datetime.timezone.utc)
    account_age_days = (now - member.created_at).days
    server_age_days = (now - member.joined_at).days if member.joined_at else 0
    offenses = prior_offenses or []

    state_summary = build_profile_state(member, messages, offenses)

    questions = [
        Noul(
            id="is_scammy",
            description="Does this user exhibit malicious scam, phishing, fraudulent links, crypto/giftcard bait, wallet drainers, or social engineering?",
        ),
        Noul(
            id="is_spammy",
            description="Does this user exhibit repetitive unsolicited self-promotion, advertising, link dumping, copypasta, or message flooding?",
        ),
        Noul(
            id="is_noob",
            description="Does this user exhibit newcomer confusion, asking basic onboarding/FAQ questions, unfamiliarity with server rules or discord mechanics?",
        ),
        Noul(
            id="is_toxic",
            description="Does this user exhibit hostile, provocative, disrespectful, inflammatory, or insulting behavior towards others?",
        ),
        Noul(
            id="is_helpful",
            description="Does this user exhibit constructive, supportive, community-building behavior, answering questions or adding valuable insights?",
        ),
        Choice(
            id="primary_persona",
            options=[
                "VALUED_REGULAR",
                "BENIGN_NEWBIE",
                "CASUAL_CHATTER",
                "UNSOLICITED_PROMOTER",
                "VOLATILE_TROLL",
                "SUSPICIOUS_ACCOUNT",
            ],
            description="Classify the single dominant behavioral persona for this user based on their message history.",
        ),
        Choice(
            id="recommended_action",
            options=[
                "NO_ACTION",
                "WELCOME_GUIDE",
                "MONITOR_WATCHLIST",
                "ISSUE_WARNING",
                "MUTE_OR_TIMEOUT",
                "BAN",
            ],
            description="Recommended administrative or moderation next step for this user.",
        ),
    ]

    target_model = model or getattr(client, "model", "jev-latest")
    try:
        response: TypeSafeEvaluationResponse = await client.evaluate(
            state=state_summary,
            questions=questions,
            model=target_model,
        )
    except Exception as exc:
        logger.exception("TypeSafe profile evaluation failed: %s", exc)
        # Fallback profile on error
        return UserProfileData(
            user_id=member.id,
            display_name=member.display_name,
            account_age_days=account_age_days,
            server_age_days=server_age_days,
            sampled_message_count=len(messages),
            prior_offense_count=len(offenses),
            summary="⚠️ AI evaluation could not be completed due to an API error.",
            sampled_messages=messages,
        )

    # Extract answers
    scam_q = response.questions.get("is_scammy")
    spam_q = response.questions.get("is_spammy")
    noob_q = response.questions.get("is_noob")
    toxic_q = response.questions.get("is_toxic")
    helpful_q = response.questions.get("is_helpful")
    persona_q = response.questions.get("primary_persona")
    action_q = response.questions.get("recommended_action")

    scam_score = scam_q.noul if scam_q else 0.0
    spam_score = spam_q.noul if spam_q else 0.0
    noob_score = noob_q.noul if noob_q else 0.0
    toxic_score = toxic_q.noul if toxic_q else 0.0
    helpful_score = helpful_q.noul if helpful_q else 0.0

    persona = persona_q.choice if persona_q and persona_q.choice else "CASUAL_CHATTER"
    persona_conf = persona_q.confidence if persona_q else 0.0

    rec_action = action_q.choice if action_q and action_q.choice else "NO_ACTION"
    action_conf = action_q.confidence if action_q else 0.0

    # Build qualitative summary
    traits = []
    if scam_score >= 0.6:
        traits.append("High Scam/Phishing Risk")
    if spam_score >= 0.6:
        traits.append("Promotional/Spam Tendencies")
    if noob_score >= 0.6:
        traits.append("Server Beginner / Needs Guidance")
    if toxic_score >= 0.5:
        traits.append("Friction/Hostility Detected")
    if helpful_score >= 0.6:
        traits.append("Helpful Community Contributor")

    summary = ", ".join(traits) if traits else "Normal conversation flow with no strong behavioral extremes."

    return UserProfileData(
        user_id=member.id,
        display_name=member.display_name,
        account_age_days=account_age_days,
        server_age_days=server_age_days,
        sampled_message_count=len(messages),
        prior_offense_count=len(offenses),
        scam_score=scam_score,
        spam_score=spam_score,
        noob_score=noob_score,
        toxic_score=toxic_score,
        helpful_score=helpful_score,
        persona=persona,
        persona_confidence=persona_conf,
        recommended_action=rec_action,
        action_confidence=action_conf,
        summary=summary,
        sampled_messages=messages,
    )


def build_profile_reply_view(
    profile: UserProfileData, member: discord.Member, guild: discord.Guild
) -> ReplyView:
    """Build a rich Discord Components v2 ReplyView presenting the full behavioral dossier."""
    persona_emojis = {
        "VALUED_REGULAR": "🌟",
        "BENIGN_NEWBIE": "🐣",
        "CASUAL_CHATTER": "💬",
        "UNSOLICITED_PROMOTER": "📢",
        "VOLATILE_TROLL": "⚡",
        "SUSPICIOUS_ACCOUNT": "🚨",
    }
    emoji = persona_emojis.get(profile.persona, "👤")
    fresh_warning = " 🚨 *(Fresh Account)*" if profile.account_age_days < 7 else ""

    radar_lines = [
        f"- **Scam / Threat**: {render_progress_bar(profile.scam_score)}",
        f"- **Spam / Promo**: {render_progress_bar(profile.spam_score)}",
        f"- **Noobness**: {render_progress_bar(profile.noob_score)}",
        f"- **Toxicity**: {render_progress_bar(profile.toxic_score)}",
        f"- **Helpfulness**: {render_progress_bar(profile.helpful_score)}",
    ]

    header_section = (
        f"## {emoji} Member Dossier — {member.display_name}\n"
        f"**Member**: {member.mention} (`{member.id}`)\n"
        f"**Primary Persona**: `{profile.persona}` (Conf: `{profile.persona_confidence:.0%}`)\n"
        f"**Recommended Action**: `{profile.recommended_action}`"
    )

    metadata_section = (
        f"### 📋 Account & Guild Metadata\n"
        f"• **Account Age**: `{profile.account_age_days}` days{fresh_warning}\n"
        f"• **Joined Server**: `{profile.server_age_days}` days ago\n"
        f"• **Sampled Messages**: `{profile.sampled_message_count}`\n"
        f"• **Prior Infractions**: `{profile.prior_offense_count}` recorded"
    )

    radar_section = (
        f"### 📊 Behavioral Radar\n"
        + "\n".join(radar_lines)
    )

    synthesis_section = (
        f"### 🧠 AI Behavioral Synthesis\n"
        f"{profile.summary}"
    )

    avatar_url = member.display_avatar.url if member.display_avatar else None

    reply = ReplyView(
        header_section,
        thumbnail_url=avatar_url,
        accent_color=None,
        include_footer=False,
    )
    reply.add_separator(spacing=discord.SeparatorSpacing.small, visible=True)
    reply.add_body(metadata_section)
    reply.add_separator(spacing=discord.SeparatorSpacing.small, visible=True)
    reply.add_body(radar_section)
    reply.add_separator(spacing=discord.SeparatorSpacing.small, visible=True)
    reply.add_body(synthesis_section)
    reply.add_footer(
        f"Server: {guild.name} • Analyzed {profile.sampled_message_count} messages",
        visible=True,
    )
    return reply


def build_profile_container(
    profile: UserProfileData, member: discord.Member, guild: discord.Guild
) -> discord.ui.Container:
    """Build a rich Discord Components v2 Container presenting the full behavioral dossier."""
    return build_profile_reply_view(profile, member, guild)._container

