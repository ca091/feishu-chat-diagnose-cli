from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .codex_protocol import JsonRpcTransport, strict_json_schema


# Reusable baseline for sessions that consume supplied evidence without tools.
# Callers own their effective isolation settings and event/error policies.
TOOL_FREE_CONFIG: dict[str, Any] = {
    **{f"features.{name}": False for name in (
        "shell_tool", "shell_snapshot", "apps", "plugins", "remote_plugin",
        "multi_agent", "multi_agent_v2", "code_mode", "code_mode_host",
        "computer_use", "browser_use", "in_app_browser", "image_generation",
        "view_image", "skill_search", "skill_mcp_dependency_install", "hooks",
        "memories", "memory_tool", "goals", "sleep_tool", "tool_suggest",
        "workspace_dependencies", "default_mode_request_user_input",
    )},
    "features.skip_host_skill_discovery": True,
    "skills.include_instructions": False,
    "skills.bundled.enabled": False,
    "tools.update_plan.enabled": False,
    "tools.experimental_request_user_input.enabled": False,
    "project_doc_max_bytes": 0,
    "include_environment_context": False,
    "web_search": "disabled",
    "notify": [],
}


def config_matches(config: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, expected_value in expected.items():
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict):
                return False
            value = value.get(part)
        if value != expected_value:
            return False
    return True


class ReadOnlyCodexSession:
    """Shared session lifecycle; callers validate configuration, events and output."""

    def __init__(self, transport: JsonRpcTransport, workspace: Path, model: str = "") -> None:
        self.transport = transport
        self.workspace = workspace
        self.model = model

    def initialize(self, client_name: str) -> dict[str, Any]:
        self.transport.request("initialize", {
            "clientInfo": {"name": client_name, "version": "0.1.0"},
        })
        self.transport.notify("initialized", {})
        return self.transport.request("config/read", {"includeLayers": False})["config"]

    def start_turn(
        self, instructions: str, inputs: list[dict[str, Any]], output_schema: dict[str, Any],
        *, thread_config: dict[str, Any],
    ) -> None:
        thread = self.transport.request("thread/start", {
            "cwd": str(self.workspace), "sandbox": "read-only", "approvalPolicy": "never",
            "model": self.model or None, "ephemeral": True,
            "baseInstructions": instructions, "developerInstructions": instructions,
            "config": thread_config,
        })
        self.transport.request("turn/start", {
            "threadId": thread["thread"]["id"], "cwd": str(self.workspace),
            "sandboxPolicy": {"type": "readOnly"}, "approvalPolicy": "never",
            "input": inputs, "outputSchema": strict_json_schema(output_schema),
        })

    def final_response(self, validate_event: Callable[[dict[str, Any]], None]) -> str:
        final = ""
        for event in self.transport.events():
            validate_event(event)
            method, params = event.get("method"), event.get("params", {})
            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                    final = str(item.get("text", ""))
                    if item.get("phase") == "final_answer":
                        break
            if method == "turn/completed":
                break
        return final
