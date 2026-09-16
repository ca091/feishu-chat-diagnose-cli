from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .lark_runner import LarkCliRunner
from .models import (
    AttachmentRef,
    ConversationCandidate,
    ConversationIdentity,
    NormalizedMessage,
    Sender,
    TimeRange,
)


class FeishuGateway:
    def __init__(self, runner: LarkCliRunner) -> None:
        self.runner = runner
        self.last_missing_message_ids: list[str] = []

    def search_groups(self, name: str) -> list[ConversationCandidate]:
        candidates: list[ConversationCandidate] = []
        page_token = ""
        while True:
            args = [
                "im",
                "+chat-search",
                "--as",
                "user",
                "--disable-search-by-user",
                "--query",
                name,
                "--page-size",
                "100",
                "--json",
            ]
            if page_token:
                args.extend(["--page-token", page_token])
            payload = self.runner.run(args)
            candidates.extend(
                ConversationCandidate(
                    chat_id=str(item.get("chat_id", "")),
                    name=str(item.get("name", name)),
                    conversation_type="group",
                    detail=_candidate_detail(item),
                )
                for item in _items(payload, "items", "chats")
            )
            if not _payload_value(payload, "has_more", False):
                return candidates
            page_token = str(_payload_value(payload, "page_token", ""))
            if not page_token:
                return candidates

    def search_people(self, name: str) -> list[ConversationCandidate]:
        payload = self.runner.run(
            [
                "contact",
                "+search-user",
                "--as",
                "user",
                "--query",
                name,
                "--has-chatted",
                "--json",
            ]
        )
        return [
            ConversationCandidate(
                name=str(item.get("name", name)),
                conversation_type="p2p",
                participant_open_id=str(item.get("open_id", item.get("user_id", ""))),
                detail=str(item.get("email", "")),
            )
            for item in _items(payload, "users", "items")
        ]

    def resolve_private_chat(self, name: str, participant_open_id: str) -> ConversationIdentity:
        payload = self.runner.run(
            [
                "im",
                "+chat-messages-list",
                "--as",
                "user",
                "--user-id",
                participant_open_id,
                "--page-size",
                "1",
                "--json",
            ]
        )
        chat_id = str(_payload_value(payload, "chat_id", ""))
        if not chat_id:
            messages = _items(payload, "items", "messages")
            chat_id = str(messages[0].get("chat_id", "")) if messages else ""
        if not chat_id:
            raise RuntimeError(f"Could not resolve an existing private chat with {name}")
        return ConversationIdentity(
            chat_id=chat_id,
            name=name,
            conversation_type="p2p",
            participant_open_id=participant_open_id,
        )

    def fetch_messages(self, chat_id: str, time_range: TimeRange) -> list[NormalizedMessage]:
        self.last_missing_message_ids = []
        messages: dict[str, NormalizedMessage] = {}
        page_token = ""
        while True:
            args = [
                "im",
                "+chat-messages-list",
                "--as",
                "user",
                "--chat-id",
                chat_id,
                "--start",
                time_range.start.isoformat(),
                "--end",
                time_range.end.isoformat(),
                "--order",
                "asc",
                "--page-size",
                "50",
                "--no-reactions",
                "--json",
            ]
            if page_token:
                args.extend(["--page-token", page_token])
            payload = self.runner.run(args)
            for raw in _items(payload, "items", "messages"):
                message = parse_message(raw)
                messages[message.message_id] = message
            if not _payload_value(payload, "has_more", False):
                break
            page_token = str(_payload_value(payload, "page_token", ""))
            if not page_token:
                break
        self._expand_quote_parents(messages)
        self._expand_threads(messages)
        return sorted(messages.values(), key=_message_sort_key)

    def _expand_quote_parents(self, messages: dict[str, NormalizedMessage]) -> None:
        attempted: set[str] = set()
        while True:
            missing = {
                item.reply_to_message_id for item in messages.values()
                if item.reply_to_message_id and item.reply_to_message_id not in messages
                and item.reply_to_message_id not in attempted
            }
            if not missing:
                return
            for offset in range(0, len(missing), 50):
                batch = sorted(missing)[offset:offset + 50]
                attempted.update(batch)
                try:
                    payload = self.runner.run([
                        "im", "+messages-mget", "--as", "user",
                        "--message-ids", ",".join(batch), "--no-reactions", "--json",
                    ])
                    found = {str(item.get("message_id", "")): item
                             for item in _items(payload, "items", "messages")}
                    for message_id in batch:
                        raw = found.get(message_id)
                        if raw:
                            messages[message_id] = parse_message(raw)
                        else:
                            self.last_missing_message_ids.append(message_id)
                except Exception:
                    self.last_missing_message_ids.extend(batch)

    def _expand_threads(self, messages: dict[str, NormalizedMessage]) -> None:
        roots = sorted({item.thread_root_id for item in messages.values() if item.thread_root_id})
        for root_id in roots:
            page_token = ""
            try:
                while True:
                    args = [
                        "im", "+threads-messages-list", "--as", "user", "--thread", root_id,
                        "--order", "asc", "--page-size", "500", "--no-reactions", "--json",
                    ]
                    if page_token:
                        args.extend(["--page-token", page_token])
                    payload = self.runner.run(args)
                    for raw in _items(payload, "items", "messages"):
                        message = parse_message(raw)
                        messages[message.message_id] = message
                    if not _payload_value(payload, "has_more", False):
                        break
                    page_token = str(_payload_value(payload, "page_token", ""))
                    if not page_token:
                        break
            except Exception:
                self.last_missing_message_ids.append(root_id)

    def download_attachment(
        self, attachment: AttachmentRef, root: Path, output_name: str
    ) -> Path:
        root = root.resolve()
        output_path = (root / output_name).resolve()
        if not output_path.is_relative_to(root):
            raise ValueError("attachment output path escapes temporary root")
        self.runner.run(
            [
                "im",
                "+messages-resources-download",
                "--as",
                "user",
                "--message-id",
                attachment.source_message_id,
                "--file-key",
                attachment.file_key,
                "--type",
                attachment.resource_type,
                "--output",
                output_name,
                "--json",
            ],
            cwd=root,
        )
        if not output_path.exists():
            raise FileNotFoundError(f"lark-cli did not create {output_name}")
        return output_path


def parse_message(raw: dict[str, Any]) -> NormalizedMessage:
    body = raw.get("body", {})
    if "content" in raw:
        content = raw["content"]
    else:
        content = body.get("content", body) if isinstance(body, dict) else body
    content = _decode_json(content)
    sender_data = raw.get("sender", {}) or {}
    message_id = str(raw.get("message_id", raw.get("message_id_type", "")))
    resources = [item for item in raw.get("resources", []) if isinstance(item, dict)]
    attachments = [_parse_attachment(message_id, item) for item in resources]
    known_keys = {item.file_key for item in attachments}
    for index, key in enumerate(_inline_image_keys(content), start=1):
        if key not in known_keys:
            attachments.append(
                AttachmentRef(
                    source_message_id=message_id,
                    file_key=key,
                    resource_type="image",
                    media_type="image/*",
                    name=f"inline-image-{index}.png",
                    kind="image",
                )
            )
            known_keys.add(key)
    position = raw.get("message_position")
    return NormalizedMessage(
        message_id=message_id,
        sender=Sender(
            open_id=str(sender_data.get("id", sender_data.get("open_id", ""))),
            name=str(sender_data.get("name", sender_data.get("sender_name", ""))),
        ),
        created_at=_parse_timestamp(raw.get("create_time", raw.get("created_at"))),
        message_position=int(position) if str(position or "").isdigit() else None,
        text="\n".join(_text_fragments(content)).strip(),
        reply_to_message_id=str(raw.get("parent_id", raw.get("reply_to_message_id", "")) or ""),
        thread_root_id=str(raw.get("root_id", raw.get("thread_root_id", "")) or ""),
        mentions=[
            str(item.get("name", item.get("key", "")))
            for item in raw.get("mentions", [])
            if isinstance(item, dict)
        ],
        attachments=attachments,
    )


def _items(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    containers = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        for key in keys:
            value = container.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _payload_value(payload: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in payload:
        return payload[key]
    data = payload.get("data")
    return data.get(key, default) if isinstance(data, dict) else default


def _decode_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _text_fragments(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_text_fragments(item))
        return result
    if not isinstance(value, dict):
        return []

    result = []
    for key, item in value.items():
        if key in {"text", "title"} and isinstance(item, str):
            result.append(item)
        elif key not in {"file_key", "image_key"}:
            result.extend(_text_fragments(item))
    return result


def _inline_image_keys(content: Any) -> list[str]:
    text = "\n".join(_text_fragments(content))
    return re.findall(r"!\[Image\]\((img_[^)\s]+)\)", text, flags=re.IGNORECASE)


def _parse_attachment(message_id: str, raw: dict[str, Any]) -> AttachmentRef:
    media_type = str(raw.get("content_type", raw.get("media_type", raw.get("mime_type", ""))))
    name = str(raw.get("name", raw.get("file_name", "")))
    resource_type = "image" if raw.get("type") == "image" or media_type.startswith("image/") else "file"
    suffix = Path(name).suffix.lower()
    if media_type.startswith("image/"):
        kind = "image"
    elif media_type.startswith("video/") or suffix in {".mp4", ".mov", ".webm", ".mkv"}:
        kind = "video"
    elif media_type.startswith("text/") or suffix in {".txt", ".log", ".json", ".md"}:
        kind = "text"
    else:
        kind = "unsupported"
    return AttachmentRef(
        source_message_id=message_id,
        file_key=str(raw.get("file_key", raw.get("image_key", raw.get("key", "")))),
        resource_type=resource_type,
        media_type=media_type,
        name=name,
        kind=kind,
    )


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai")) if parsed.tzinfo is None else parsed


def _message_sort_key(message: NormalizedMessage) -> tuple[datetime, int, str]:
    position = message.message_position if message.message_position is not None else 2**63 - 1
    return message.created_at, position, message.message_id


def _candidate_detail(item: dict[str, Any]) -> str:
    count = item.get("member_count")
    return f"{count} members" if count is not None else ""
