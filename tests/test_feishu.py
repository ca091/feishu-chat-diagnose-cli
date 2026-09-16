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

    messages = FeishuGateway(fake_runner).fetch_messages("oc_group_a", time_range)

    assert [item.message_id for item in messages] == ["om_1", "om_2", "om_3"]
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
    messages = FeishuGateway(fake_runner).fetch_messages("oc_group_a", time_range)
    assert [item.message_id for item in messages] == ["om_parent", "om_child"]
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


def test_fetch_messages_follows_nested_data_pagination(fake_runner, time_range) -> None:
    first = _raw_message("om_first", "first", "1752559200000")
    second = _raw_message("om_second", "second", "1752559260000")
    fake_runner.responses.extend([
        {"data": {"messages": [first], "has_more": True, "page_token": "nested-next"}},
        {"data": {"messages": [second], "has_more": False, "page_token": ""}},
    ])

    messages = FeishuGateway(fake_runner).fetch_messages("oc_group_a", time_range)

    assert [item.message_id for item in messages] == ["om_first", "om_second"]
    assert fake_runner.calls[1][-2:] == ["--page-token", "nested-next"]


def _raw_message(message_id: str, text: str, create_time: str) -> dict:
    return {
        "message_id": message_id,
        "chat_id": "oc_group_a",
        "create_time": create_time,
        "sender": {"id": "ou_sender", "name": "Sender"},
        "body": {"content": {"text": text}},
        "mentions": [],
    }
