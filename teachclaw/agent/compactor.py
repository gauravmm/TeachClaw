"""Proactive prompt-cache compaction for long-running sessions.

When the rendered prompt approaches the model's input budget, a separate
provider call summarizes the history strictly before the latest user
message and replaces those events with a single :class:`SummaryEvent`.
The summarizer never sees the agent toolset — it can compress, not act.

The agent loop calls :meth:`Compactor.maybe_compact` once per turn, right
after the prompt is rendered. ``True`` means the caller should re-render
because the session has changed.
"""

from __future__ import annotations

import json

from loguru import logger

from teachclaw.agent.prompt import PromptBuilder
from teachclaw.bus import MessageAddress
from teachclaw.config import AgentConfig
from teachclaw.providers.base import LLMProvider
from teachclaw.session import ConversationEvent, Session, UserEvent

_SUMMARIZE_SYSTEM_PROMPT = """\
You are a conversation summarizer for an agentic AI assistant.

Compress the conversation below into a brief summary that preserves:
1. Facts the assistant has established or learned about the user or task.
2. Decisions made and the reasoning behind them.
3. Open tasks, commitments, or pending follow-ups.
4. User preferences and any context about who they are or what they want.
5. The user's most recent question or request, verbatim if short.

Drop conversational filler, repeated greetings, and routine acknowledgments.
The summary will replace the conversation in the assistant's context window,
so it must stand alone as the only record of what was discussed.
Be concise and factual. Plain prose or bulleted lists are both fine.
"""

_SUMMARIZE_MAX_TOKENS = 2048


def _estimate_tokens(messages: list[dict[str, object]]) -> int:
    """Cheap heuristic: ~4 characters per token. See spec/COMPACTION.md.

    Wide threshold margin (~18% of input budget) absorbs the heuristic's
    ~30% inaccuracy. Replace with a tokenizer-backed estimate later if
    the trigger turns out to misfire in either direction.
    """
    return len(json.dumps(messages, ensure_ascii=False)) // 4


def _last_user_event_index(events: list[ConversationEvent]) -> int:
    for i in range(len(events) - 1, -1, -1):
        if isinstance(events[i], UserEvent):
            return i
    return -1


class Compactor:
    """Decides when to summarize and runs the summarizer call."""

    def __init__(
        self,
        provider: LLMProvider,
        agent_config: AgentConfig,
        prompt_builder: PromptBuilder,
    ) -> None:
        self.provider = provider
        self.config = agent_config
        self.prompt_builder = prompt_builder

    def _input_budget(self) -> int:
        return max(self.config.context_window - self.config.max_tokens, 1)

    async def maybe_compact(
        self,
        session: Session,
        addr: MessageAddress,
        llm_messages: list[dict[str, object]],
    ) -> bool:
        """Estimate prompt size and summarize if over threshold.

        Returns True if compaction happened and the caller should re-render.
        """
        estimate = _estimate_tokens(llm_messages)
        threshold_tokens = int(self.config.compaction.threshold * self._input_budget())
        if estimate <= threshold_tokens:
            return False

        last_user_idx = _last_user_event_index(session.events)
        # Summarize everything strictly before the most recent user message;
        # if there is no prior user message (or the only user message is the
        # very first event), there is nothing useful to summarize without
        # losing the latest question, so we skip this round and let the next
        # request through unchanged.
        if last_user_idx <= 0:
            logger.warning(
                f"Session {addr} prompt {estimate} tokens > {threshold_tokens} threshold "
                f"but no compactable history before the latest user message; skipping."
            )
            return False

        to_summarize = list(session.events[:last_user_idx])
        logger.warning(
            f"Compacting session {addr}: {estimate}/{self._input_budget()} input tokens "
            f"(>{threshold_tokens} threshold); summarizing {len(to_summarize)} events."
        )
        summary = await self._summarize(session, addr, to_summarize)
        if summary is None:
            return False

        session.compact_with_summary(summary, keep_from_index=last_user_idx)
        logger.warning(
            f"Session {addr} compacted: {len(session.events)} events remain, "
            f"summary {len(summary)} chars."
        )
        return True

    async def _summarize(
        self,
        session: Session,
        addr: MessageAddress,
        events_to_summarize: list[ConversationEvent],
    ) -> str | None:
        # Render the doomed events as a one-shot conversation, swapping the
        # main system prompt for a summarization instruction. We deliberately
        # do not pass tools to this call: the summarizer is not allowed to
        # take actions, only to compress.
        history_messages = session.render_history(
            events_to_summarize,
            options=self.prompt_builder.render_options(),
        )
        summarize_messages: list[dict[str, object]] = [
            {"role": "system", "content": _SUMMARIZE_SYSTEM_PROMPT},
            *history_messages,
            {"role": "user", "content": "Now produce the summary as instructed above."},
        ]
        summarize_model = self.config.compaction.summarize_model or self.config.model
        try:
            response = await self.provider.chat(
                messages=summarize_messages,
                tools=None,
                model=summarize_model,
                max_tokens=_SUMMARIZE_MAX_TOKENS,
                temperature=0.3,
                top_p=self.config.top_p,
                top_k=self.config.top_k,
                enable_thinking=False if self.config.enable_thinking else None,
            )
        except Exception as e:
            logger.error(f"Summarization failed for {addr}: {e}")
            return None
        return (response.content or "").strip() or None
