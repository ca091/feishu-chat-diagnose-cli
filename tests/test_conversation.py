import pytest

from feishu_chat_diagnose.conversation import ConversationAssembler
from feishu_chat_diagnose.errors import ContextTooLarge


def test_keyword_seeds_keep_quote_parent_and_full_thread(message_factory) -> None:
    parent = message_factory("om_parent", "Earlier export discussion", minute=0)
    root = message_factory("om_root", "Export bug", thread_root_id="om_root", minute=1)
    match = message_factory(
        "om_match",
        "Text shifted",
        reply_to_message_id="om_parent",
        thread_root_id="om_root",
        minute=2,
    )
    sibling = message_factory("om_sibling", "I reproduced it", thread_root_id="om_root", minute=3)
    unrelated = message_factory("om_other", "Lunch", minute=4)

    result = ConversationAssembler(max_messages=300).assemble(
        [parent, root, match, sibling, unrelated], keyword="shifted"
    )

    assert [item.message_id for item in result] == ["om_parent", "om_root", "om_match", "om_sibling"]


def test_message_limit_fails_without_truncation(message_factory) -> None:
    messages = [message_factory(f"om_{index}", str(index), minute=index) for index in range(4)]

    with pytest.raises(ContextTooLarge, match="4 messages exceeds limit 3"):
        ConversationAssembler(max_messages=3).assemble(messages, keyword="")


def test_messages_are_deduplicated_and_sorted(message_factory) -> None:
    later = message_factory("om_later", "later", minute=2)
    earlier = message_factory("om_earlier", "earlier", minute=1)

    result = ConversationAssembler(max_messages=3).assemble([later, earlier, later], keyword="")

    assert [item.message_id for item in result] == ["om_earlier", "om_later"]
