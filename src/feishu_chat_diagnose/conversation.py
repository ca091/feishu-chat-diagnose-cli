from __future__ import annotations

from .errors import ContextTooLarge
from .models import NormalizedMessage


class ConversationAssembler:
    def __init__(self, max_messages: int) -> None:
        self.max_messages = max_messages

    def assemble(
        self, messages: list[NormalizedMessage], keyword: str = "",
        *, seed_ids: set[str] | None = None, check_limit: bool = True,
    ) -> list[NormalizedMessage]:
        """Keywords retain full threads; explicit seeds retain only roots and quote ancestors."""
        by_id = {message.message_id: message for message in messages}
        selected = set(by_id)

        if seed_ids is not None:
            selected = seed_ids & set(by_id)
        elif keyword:
            needle = keyword.casefold()
            selected = {
                message.message_id
                for message in by_id.values()
                if needle in message.text.casefold()
            }
        if keyword or seed_ids is not None:
            while True:
                previous_count = len(selected)
                self._include_quote_ancestors(selected, by_id)
                roots = {
                    message.thread_root_id
                    for message in by_id.values()
                    if message.thread_root_id and (
                        message.message_id in selected or message.thread_root_id in selected
                    )
                }
                selected.update(root for root in roots if root in by_id)
                if seed_ids is None:
                    selected.update(
                        message.message_id
                        for message in by_id.values()
                        if message.thread_root_id in roots
                    )
                if len(selected) == previous_count:
                    break

        result = sorted(
            (by_id[message_id] for message_id in selected if message_id in by_id),
            key=lambda item: (
                item.created_at,
                item.message_position if item.message_position is not None else 2**63 - 1,
                item.message_id,
            ),
        )
        if check_limit and len(result) > self.max_messages:
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
