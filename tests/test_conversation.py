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


def test_keyword_context_closes_over_sibling_quotes_and_linked_threads(message_factory) -> None:
    messages = [
        message_factory("parent", "Earlier context"),
        message_factory("root", "First thread", minute=1),
        message_factory("match", "Bug", thread_root_id="root", minute=2),
        message_factory("sibling", "See this", thread_root_id="root",
                        reply_to_message_id="linked", minute=3),
        message_factory("linked", "Related", thread_root_id="root2", minute=4),
        message_factory("root2", "Second thread", minute=5),
        message_factory("sibling2", "More context", thread_root_id="root2",
                        reply_to_message_id="parent", minute=6),
        message_factory("other", "Unrelated", minute=7),
    ]

    result = ConversationAssembler(max_messages=7).assemble(messages, keyword="bug")

    assert [item.message_id for item in result] == [item.message_id for item in messages[:-1]]
    with pytest.raises(ContextTooLarge, match="7 messages exceeds limit 6"):
        ConversationAssembler(max_messages=6).assemble(messages, keyword="bug")


def test_keyword_on_thread_root_includes_replies_without_root_self_reference(message_factory) -> None:
    messages = [
        message_factory("root", "Bug"),
        message_factory("reply", "Reproduced", thread_root_id="root", minute=1),
    ]

    assert ConversationAssembler(max_messages=2).assemble(messages, keyword="bug") == messages


def test_quote_cycles_terminate(message_factory) -> None:
    messages = [
        message_factory("first", "Bug", reply_to_message_id="second"),
        message_factory("second", "Context", reply_to_message_id="first", minute=1),
    ]

    assert ConversationAssembler(max_messages=2).assemble(messages, keyword="bug") == messages


def test_explicit_seeds_keep_recursive_ancestors_without_thread_siblings(message_factory) -> None:
    messages = [
        message_factory("ancestor", "Earlier background"),
        message_factory("root", "Thread background", reply_to_message_id="ancestor", minute=1),
        message_factory("match", "Code question", thread_root_id="root", minute=2),
        message_factory("sibling", "Lunch", thread_root_id="root", minute=3),
    ]
    assembler = ConversationAssembler(max_messages=3)
    assert assembler.assemble(messages, seed_ids={"match"}) == messages[:3]
    assert assembler.assemble(messages, seed_ids=set()) == []
