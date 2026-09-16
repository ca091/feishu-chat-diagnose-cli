from __future__ import annotations

from .errors import ContextTooLarge
from .models import NormalizedMessage


class ConversationAssembler:
    def __init__(self, max_messages: int) -> None:
        self.max_messages = max_messages

    def assemble(
        self, messages: list[NormalizedMessage], keyword: str = ""
    ) -> list[NormalizedMessage]:
        by_id = {message.message_id: message for message in messages}
        selected = set(by_id)

        if keyword:
            needle = keyword.casefold()
            selected = {
                message.message_id
                for message in by_id.values()
                if needle in message.text.casefold()
            }
            self._include_quote_ancestors(selected, by_id)
            roots = {
                by_id[message_id].thread_root_id
                for message_id in selected
                if by_id[message_id].thread_root_id
            }
            selected.update(roots)
            selected.update(
                message.message_id
                for message in by_id.values()
                if message.thread_root_id in roots
            )

        result = sorted(
            (by_id[message_id] for message_id in selected if message_id in by_id),
            key=lambda item: (
                item.created_at,
                item.message_position if item.message_position is not None else 2**63 - 1,
                item.message_id,
            ),
        )
        if len(result) > self.max_messages:
            raise ContextTooLarge(f"{len(result)} messages exceeds limit {self.max_messages}")
        return result

    @staticmethod
    def _include_quote_ancestors(
        selected: set[str], by_id: dict[str, NormalizedMessage]
    ) -> None:
        pending = list(selected)
        while pending:
            message = by_id.get(pending.pop())
            if not message or not message.reply_to_message_id:
                continue
            parent_id = message.reply_to_message_id
            if parent_id in by_id and parent_id not in selected:
                selected.add(parent_id)
                pending.append(parent_id)
