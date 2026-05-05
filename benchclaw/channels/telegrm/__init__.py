"""Telegram channel — package layout.

Files (one concern each):

* ``channel``         — :class:`TelegramChannel` lifecycle + handler wiring
* ``config``          — :class:`TelegramConfig` + slash-command lists
* ``state``           — per-user state, reply records, segment dataclasses,
                        reaction constants
* ``markdown_html``   — markdown → Telegram-HTML conversion + chunking
* ``auth_gate``       — DM-only + auth gating for inbound updates
* ``inbound``         — inbound message handler + soft rate-limit
* ``outbound``        — send pipeline (plan → dispatch → record)
* ``reactions``       — reaction-emoji handlers (sources / trace)
* ``commands``        — slash-command bodies, menu publishing, persona switch
* ``typing_loop``     — typing-indicator background loop

External callers want :class:`TelegramChannel` and :class:`TelegramConfig`;
both are re-exported here so the long-standing import
``from benchclaw.channels.telegrm import TelegramChannel, TelegramConfig``
keeps working.
"""

from benchclaw.channels.telegrm.channel import TelegramChannel
from benchclaw.channels.telegrm.config import TelegramConfig

__all__ = ["TelegramChannel", "TelegramConfig"]
