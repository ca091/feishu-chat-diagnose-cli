import pytest

from feishu_chat_diagnose.conversation import ConversationAssembler
from feishu_chat_diagnose.feishu import FeishuGateway, parse_message


def test_search_group_returns_all_same_name_candidates(fake_runner) -> None:
    fake_runner.responses.append(
        {
            "items": [
                {"chat_id": "oc_group_a", "name": "Frontend Project Group", "member_count": 20},
                {"chat_id": "oc_group_b", "name": "Frontend Project Group", "member_count": 8},
            ],
            "has_more": False,
        }
    )

    candidates = FeishuGateway(fake_runner).search_groups("Frontend Project Group")

    assert [item.chat_id for item in candidates] == ["oc_group_a", "oc_group_b"]
    assert fake_runner.calls[0] == [
        "im",
        "+chat-search",
        "--as",
        "user",
        "--disable-search-by-user",
        "--query",
        "Frontend Project Group",
        "--page-size",
        "100",
        "--json",
    ]


def test_search_person_is_restricted_to_existing_chats(fake_runner) -> None:
    fake_runner.responses.append(
        {"users": [{"open_id": "ou_zhangsan", "name": "Zhang San", "email": "z@example.com"}]}
    )

    candidates = FeishuGateway(fake_runner).search_people("Zhang San")

    assert fake_runner.calls[0] == [
        "contact",
        "+search-user",
        "--as",
        "user",
        "--query",
        "Zhang San",
        "--has-chatted",
        "--json",
    ]
    assert candidates[0].participant_open_id == "ou_zhangsan"


def test_resolve_private_chat_uses_selected_person(fake_runner) -> None:
    fake_runner.responses.append(
        {"chat_id": "oc_p2p", "items": [{"message_id": "om_latest", "chat_id": "oc_p2p"}]}
    )

    result = FeishuGateway(fake_runner).resolve_private_chat("Zhang San", "ou_zhangsan")

    assert result.chat_id == "oc_p2p"
    assert result.conversation_type == "p2p"
    assert "--user-id" in fake_runner.calls[0]


def test_fetch_messages_pages_in_ascending_order(fake_runner, time_range) -> None:
    fake_runner.responses.extend(
        [
            {
                "items": [
                    _raw_message("om_1", "first", "1752559200000"),
                    _raw_message("om_2", "second", "1752559260000"),
                ],
                "has_more": True,
                "page_token": "next",
            },
            {
                "items": [
                    _raw_message("om_2", "second", "1752559260000"),
                    _raw_message("om_3", "third", "1752559320000"),
                ],
                "has_more": False,
            },
        ]
    )

    result = FeishuGateway(fake_runner).fetch_messages("oc_group_a", time_range)

    assert [item.message_id for item in result.messages] == ["om_1", "om_2", "om_3"]
    assert "--page-token" in fake_runner.calls[1]


def test_parse_message_extracts_post_text_and_video_attachment() -> None:
    raw = _raw_message("om_video", "ignored", "1752559200000")
    raw["msg_type"] = "post"
    raw["body"] = {
        "content": {
            "title": "Export issue",
            "content": [[{"tag": "text", "text": "Text shifted"}]],
        }
    }
    raw["resources"] = [
        {
            "file_key": "file_video",
            "type": "file",
            "name": "recording.mp4",
            "content_type": "video/mp4",
        }
    ]

    message = parse_message(raw)

    assert "Export issue" in message.text
    assert "Text shifted" in message.text
    assert message.attachments[0].kind == "video"


def test_fetch_messages_expands_missing_quote_parent(fake_runner, time_range) -> None:
    child = _raw_message("om_child", "reply", "1752559260000")
    child["parent_id"] = "om_parent"
    fake_runner.responses.extend([
        {"items": [child], "has_more": False},
        {"items": [_raw_message("om_parent", "context", "1752559200000")]},
    ])
    result = FeishuGateway(fake_runner).fetch_messages("oc_group_a", time_range)
    assert [item.message_id for item in result.messages] == ["om_parent", "om_child"]
    assert fake_runner.calls[1][0:2] == ["im", "+messages-mget"]


def test_parse_current_lark_cli_shape_extracts_text_and_inline_image() -> None:
    raw = {
        "message_id": "om_current",
        "content": "画布里面的 Agent 不见了\n![Image](img_v3_example)",
        "create_time": "2026-07-15 15:40",
        "message_position": "762",
        "msg_type": "post",
        "sender": {"id": "ou_sender", "name": "Sender"},
    }

    message = parse_message(raw)

    assert "Agent 不见了" in message.text
    assert message.message_position == 762
    assert message.attachments[0].file_key == "img_v3_example"
    assert message.attachments[0].kind == "image"


def test_forwarded_attachments_reach_download_and_diagnosis(
    fake_runner, time_range, tmp_path, mocker,
) -> None:
    import json

    from feishu_chat_diagnose.attachments import AttachmentManager
    from feishu_chat_diagnose.codex_app_server import CodexAppServerDiagnosis
    from feishu_chat_diagnose.models import ConversationIdentity, MessageBundle

    content = (
        '<forwarded_messages>\n[2026-07-15T14:00:00+08:00] Sender:\n'
        '导出失败\n![Image](img_forward)\n'
        '<forwarded_messages>\n'
        '<video key="file_video" name="录屏.MOV" duration="5s" cover_image_key="img_cover"/>\n'
        '<file name="错误 &amp; &quot;日志&quot;.log" key="file_log"/>\n'
        '</forwarded_messages>\n</forwarded_messages>'
    )
    raw = _raw_message("om_forward", "", "1752559200000")
    raw.update(content=content, msg_type="merge_forward")
    fake_runner.responses.append({"items": [raw], "has_more": False})
    gateway = FeishuGateway(fake_runner)

    messages = gateway.fetch_messages("oc_group_a", time_range).messages

    assert messages[0].text == content
    assert [(a.file_key, a.kind) for a in messages[0].attachments] == [
        ("img_forward", "image"), ("file_video", "video"), ("file_log", "text"),
    ]
    assert len(fake_runner.calls) == 1
    assert "--download-resources" not in fake_runner.calls[0]

    def download(args, cwd):
        assert args[:2] == ["im", "+messages-resources-download"]
        assert args[args.index("--message-id") + 1] == "om_forward"
        key = args[args.index("--file-key") + 1]
        assert args[args.index("--type") + 1] == ("image" if key == "img_forward" else "file")
        path = cwd / args[args.index("--output") + 1]
        path.write_bytes("导出失败：processing".encode() if key == "file_log" else b"media")
        return {}

    mocker.patch.object(fake_runner, "run", side_effect=download)
    bundle = MessageBundle(
        conversation=ConversationIdentity(chat_id="oc_group_a", name="Project", conversation_type="group"),
        time_range=time_range, messages=messages,
    )
    with AttachmentManager(gateway, tmp_path, 1024).materialize(messages[0].attachments) as batch:
        assert not batch.failed
        paths = [item.local_path for item in batch.available]
        assert [path.suffix for path in paths] == [".png", ".mov", ".log"]
        assert batch.available[2].attachment.name == '错误 & "日志".log'
        prompt = CodexAppServerDiagnosis._prompt(bundle, batch.available, None)
        text_inputs = json.loads(
            prompt.split("TEXT_ATTACHMENTS_JSON:\n", 1)[1].split("\n\nVIDEO_TOOL_MAPPINGS_JSON:", 1)[0]
        )
        videos = json.loads(prompt.split("VIDEO_TOOL_MAPPINGS_JSON:\n", 1)[1])
        assert text_inputs == [{
            "source_message_id": "om_forward", "name": '错误 & "日志".log',
            "content": "导出失败：processing",
        }]
        assert videos == [{
            "source_message_id": "om_forward", "name": "录屏.MOV", "path": str(paths[1]),
        }]
    assert all(not path.exists() for path in paths)


def test_inline_attachments_are_deduplicated_and_resources_take_precedence() -> None:
    raw = _raw_message("om_1", "", "1752559200000")
    raw["content"] = (
        '<video key="file_video" name="fallback.mp4"/>'
        '<video key="file_video" name="fallback.mp4"/>'
        '<file key="file_text" name="console.txt"/>'
        '<file key="file_text" name="console.txt"/>'
        '![Image](img_one) ![Image](img_one)'
    )
    raw["resources"] = [{
        "file_key": "file_video", "content_type": "video/webm", "name": "original.webm",
    }]

    message = parse_message(raw)

    assert {a.file_key for a in message.attachments} == {"file_video", "file_text", "img_one"}
    assert len(message.attachments) == 3
    assert message.attachments[0].name == "original.webm"
    assert message.attachments[0].media_type == "video/webm"


def test_inline_video_without_filename_and_unsupported_files() -> None:
    from feishu_chat_diagnose.attachments import _safe_suffix

    raw = _raw_message("om_1", "", "1752559200000")
    raw["content"] = (
        '<video key="file_video" name="file_video"/>'
        '<file key="file_document" name="需求.pdf"/>'
        '<video name="missing-key.mp4"/> <file key=""/> <file key="   "/>'
    )

    message = parse_message(raw)

    assert [(a.file_key, a.kind) for a in message.attachments] == [
        ("file_video", "video"), ("file_document", "unsupported"),
    ]
    assert _safe_suffix(message.attachments[0]) == ".mp4"


def test_fetch_messages_follows_nested_data_pagination(fake_runner, time_range) -> None:
    first = _raw_message("om_first", "first", "1752559200000")
    second = _raw_message("om_second", "second", "1752559260000")
    fake_runner.responses.extend([
        {"data": {"messages": [first], "has_more": True, "page_token": "nested-next"}},
        {"data": {"messages": [second], "has_more": False, "page_token": ""}},
    ])

    result = FeishuGateway(fake_runner).fetch_messages("oc_group_a", time_range)

    assert [item.message_id for item in result.messages] == ["om_first", "om_second"]
    assert fake_runner.calls[1][-2:] == ["--page-token", "nested-next"]


def test_fetch_and_assemble_close_over_thread_quotes_and_connected_threads(fake_runner, time_range) -> None:
    match = _raw_message("match", "Bug", "1752559260000") | {"root_id": "root_a"}
    root_a = _raw_message("root_a", "First topic", "1752559200000")
    sibling_a = _raw_message("sibling_a", "See related context", "1752559320000") | {
        "root_id": "root_a", "parent_id": "linked",
    }
    linked = _raw_message("linked", "Related discussion", "1752559140000") | {
        "root_id": "root_b", "parent_id": "ancestor",
    }
    ancestor = _raw_message("ancestor", "Earlier context", "1752559080000") | {"parent_id": "sibling_a"}
    root_b = _raw_message("root_b", "Second topic", "1752559020000")
    sibling_b = _raw_message("sibling_b", "More evidence", "1752559380000") | {
        "root_id": "root_b", "parent_id": "external",
    }
    external = _raw_message("external", "Original evidence", "1752558960000")
    fake_runner.responses.extend([
        {"items": [match]},
        {"items": [root_a], "has_more": True, "page_token": "next-thread-page"},
        {"items": [sibling_a]},
        {"items": [linked]},
        {"items": [ancestor]},
        {"items": [root_b, sibling_b]},
        {"items": [external]},
    ])
    gateway = FeishuGateway(fake_runner)

    result = gateway.fetch_messages("oc_group_a", time_range)
    selected = ConversationAssembler(max_messages=8).assemble(result.messages, keyword="bug")

    assert {item.message_id for item in selected} == {
        "match", "root_a", "sibling_a", "linked", "ancestor", "root_b", "sibling_b", "external",
    }
    assert result.missing_message_ids == []
    assert [args[1] for args in fake_runner.calls] == [
        "+chat-messages-list", "+threads-messages-list", "+threads-messages-list",
        "+messages-mget", "+messages-mget", "+threads-messages-list", "+messages-mget",
    ]
    assert fake_runner.calls[2][-2:] == ["--page-token", "next-thread-page"]
    assert not fake_runner.responses


@pytest.mark.parametrize("parent_response", [{"items": []}, RuntimeError("permission denied")])
def test_thread_sibling_missing_quote_is_reported_without_retry(mocker, time_range, parent_response) -> None:
    match = _raw_message("match", "Bug", "1752559260000") | {"root_id": "root"}
    sibling = _raw_message("sibling", "Related evidence", "1752559320000") | {
        "root_id": "root", "parent_id": "unavailable",
    }
    runner = mocker.Mock()
    runner.run.side_effect = [{"items": [match]}, {"items": [sibling]}, parent_response]
    gateway = FeishuGateway(runner)

    result = gateway.fetch_messages("oc_group_a", time_range)
    selected = ConversationAssembler(max_messages=2).assemble(result.messages, keyword="bug")

    assert {item.message_id for item in selected} == {"match", "sibling"}
    assert result.missing_message_ids == ["unavailable"]
    assert runner.run.call_count == 3
    assert runner.run.call_args.args[0][0:2] == ["im", "+messages-mget"]


def test_partial_thread_failure_is_not_retried_after_new_quotes_are_fetched(mocker, time_range) -> None:
    root = _raw_message("root", "Topic", "1752559200000")
    match = _raw_message("match", "Bug", "1752559260000") | {"root_id": "root"}
    sibling = _raw_message("sibling", "Evidence", "1752559320000") | {
        "root_id": "root", "parent_id": "parent",
    }
    parent = _raw_message("parent", "Original evidence", "1752559140000")
    runner = mocker.Mock()
    runner.run.side_effect = [
        {"items": [root, match]},
        {"items": [sibling], "has_more": True, "page_token": "next"},
        RuntimeError("thread unavailable"),
        {"items": [parent]},
    ]
    gateway = FeishuGateway(runner)

    result = gateway.fetch_messages("oc_group_a", time_range)
    selected = ConversationAssembler(max_messages=4).assemble(result.messages, keyword="bug")

    assert {item.message_id for item in selected} == {"root", "match", "sibling", "parent"}
    assert result.missing_message_ids == ["root"]
    assert runner.run.call_count == 4


def test_quote_obtained_from_thread_is_not_reported_as_missing(fake_runner, time_range) -> None:
    match = _raw_message("match", "Bug", "1752559260000") | {"root_id": "root", "parent_id": "parent"}
    parent = _raw_message("parent", "Original evidence", "1752559200000") | {"root_id": "root"}
    fake_runner.responses.extend([
        {"items": [match]}, {"items": []}, {"items": [parent]},
    ])
    gateway = FeishuGateway(fake_runner)

    result = gateway.fetch_messages("oc_group_a", time_range)

    assert {item.message_id for item in result.messages} == {"match", "parent"}
    assert result.missing_message_ids == []
    assert len(fake_runner.calls) == 3


def test_failed_context_is_retried_only_on_next_fetch(fake_runner, time_range) -> None:
    match = _raw_message("match", "Bug", "1752559260000") | {"root_id": "root", "parent_id": "parent"}
    parent = _raw_message("parent", "Original evidence", "1752559200000")
    fake_runner.responses.extend([
        {"items": [match]}, {"items": []}, {"items": []},
        {"items": [match]}, {"items": [parent]}, {"items": []},
    ])
    gateway = FeishuGateway(fake_runner)

    first = gateway.fetch_messages("oc_group_a", time_range)
    assert first.missing_message_ids == ["parent"]
    result = gateway.fetch_messages("oc_group_a", time_range)

    assert {item.message_id for item in result.messages} == {"match", "parent"}
    assert result.missing_message_ids == []
    assert first.missing_message_ids == ["parent"]
    assert len(fake_runner.calls) == 6


def _raw_message(message_id: str, text: str, create_time: str) -> dict:
    return {
        "message_id": message_id,
        "chat_id": "oc_group_a",
        "create_time": create_time,
        "sender": {"id": "ou_sender", "name": "Sender"},
        "body": {"content": {"text": text}},
        "mentions": [],
    }
