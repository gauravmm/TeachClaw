"""Auth gating + group-eligibility / admin helpers for inbound updates.

`gate(channel, update, *, allow_unauth=False)` returns True if the
message may proceed. Groups are first-class:

- DMs use the existing per-address auth marker (one user = one chat).
- Groups use the same marker mechanism keyed on the group's chat_id;
  eligibility (i.e. whether the room is *allowed* to authenticate) is
  enforced at /auth time only, see :func:`is_group_eligible`.

Unauthenticated DMs get a one-line nudge. Unauthenticated groups stay
silent — the bot doesn't pester N members about an /auth state one of
them needs to fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from telegram import Update

from benchclaw import auth as auth_module

if TYPE_CHECKING:
    from benchclaw.channels.telegrm.channel import TelegramChannel


def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type != "private")


async def gate(channel: "TelegramChannel", update: Update, *, allow_unauth: bool = False) -> bool:
    if not update.effective_user or not update.effective_chat:
        return False
    if allow_unauth:
        return True
    addr = channel.addr(update.effective_chat.id)
    if auth_module.is_authenticated(channel.workspace, addr):
        return True
    if is_group_chat(update):
        # Don't reply: an unauth group is a room of N people; one stray
        # message shouldn't trigger an auth nudge for everyone.
        logger.debug(
            f"Telegram group {update.effective_chat.id} not authenticated; dropping inbound."
        )
        return False
    await update.effective_chat.send_message(
        "This is the class assistant. Send /auth <code> — the code is on the slide."
    )
    return False


async def is_group_eligible(channel: "TelegramChannel", chat_id: int) -> bool:
    """A group is eligible to authenticate iff at least one of its current
    chat admins is in ``channels.telegram.admin_user_ids``.

    Called only at /auth time — re-checking on every inbound message would
    add a `getChatAdministrators` round-trip per turn for no real safety
    gain (the slide-code marker still rotates auth on its own).
    """
    if not channel._app:
        return False
    operator_ids = set(channel.config.admin_user_ids or [])
    if not operator_ids:
        return False
    try:
        admins = await channel._app.bot.get_chat_administrators(chat_id)
    except Exception as e:
        logger.warning(f"getChatAdministrators({chat_id}) failed: {e}")
        return False
    return any(a.user.id in operator_ids for a in admins)


async def is_caller_group_admin(channel: "TelegramChannel", chat_id: int, user_id: int) -> bool:
    """True if the caller is a creator/administrator of the given group.

    Anonymous-admin posts (where Telegram surfaces the user as the
    GroupAnonymousBot, id 1087968824) count as admin: the post is
    attributable to the group's anonymous-admin toggle, not a specific
    human, but it is by definition an admin action.
    """
    if user_id == 1087968824:  # GroupAnonymousBot
        return True
    if not channel._app:
        return False
    try:
        member = await channel._app.bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        logger.warning(f"getChatMember({chat_id}, {user_id}) failed: {e}")
        return False
    return member.status in ("creator", "administrator")
