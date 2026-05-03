#!/usr/bin/env python3
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BM_MCP_URL = os.environ.get("BM_MCP_URL", "http://basic-memory.shoggoth.local/mcp")
BM_MCP_TIMEOUT = int(os.environ.get("BM_MCP_TIMEOUT", "10"))


async def call_tool(tool_name: str, arguments: dict | None = None) -> str:
    async with streamablehttp_client(BM_MCP_URL, timeout=BM_MCP_TIMEOUT) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments or {})
            texts = [c.text for c in result.content if hasattr(c, "text")]
            return "\n".join(texts) if texts else json.dumps(
                [c.model_dump() for c in result.content], default=str
            )


def read_input() -> dict:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


async def ensure_project(project: str) -> None:
    try:
        result = await call_tool("list_memory_projects", {})
        if f'"{project}"' in result or f"'{project}'" in result or f"/{project}" in result:
            return
    except Exception:
        pass
    try:
        await call_tool("create_memory_project", {"project_name": project, "project_path": project})
    except Exception:
        pass


def write_note(title: str, content: str, directory: str, project: str | None = None) -> None:
    try:
        if project:
            asyncio.run(ensure_project(project))
        arguments = {
            "title": title,
            "content": content,
            "directory": directory,
        }
        if project:
            arguments["project"] = project
        asyncio.run(call_tool("write_note", arguments))
    except Exception:
        pass


def hook_session_start(data: dict) -> dict:
    session_id = data.get("session_id", "unknown")
    source = data.get("source", "unknown")
    model = data.get("model", "unknown")

    context_parts = []

    project = os.environ.get("SHOGGOTH_PROJECT")
    repo = os.environ.get("SHOGGOTH_REPO")
    if project or repo:
        context_parts.append("[Session context]")
        if project:
            context_parts.append(f"- [project] {project}")
        project_id = os.environ.get("SHOGGOTH_PROJECT_ID")
        if project_id:
            context_parts.append(f"- [project_id] {project_id}")
        if repo:
            context_parts.append(f"- [repository] {repo}")

    try:
        activity = asyncio.run(call_tool("recent_activity", {"timeframe": "1d"}))
        if activity:
            context_parts.append("[Recent activity]")
            context_parts.append(activity)
    except Exception:
        pass

    if context_parts:
        ctx = "\n".join(context_parts)[:4096]
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}

    return {}


def hook_user_prompt_submit(data: dict) -> dict:
    session_id = data.get("session_id", "unknown")
    prompt = data.get("prompt", "")
    project = os.environ.get("SHOGGOTH_PROJECT")

    write_note(
        f"prompt-{session_id}",
        f"User prompt: {prompt}",
        "prompts",
        project=project,
    )
    return {}


def hook_post_compact(data: dict) -> dict:
    session_id = data.get("session_id", "unknown")
    trigger = data.get("trigger", "unknown")
    compact_summary = data.get("compact_summary", "")
    project = os.environ.get("SHOGGOTH_PROJECT")

    if compact_summary:
        write_note(
            f"compact-summary-{session_id}",
            f"{compact_summary}",
            "compactions",
            project=project,
        )

    return {}


def hook_stop(data: dict) -> dict:
    session_id = data.get("session_id", "unknown")
    last_message = data.get("last_assistant_message", "")
    project = os.environ.get("SHOGGOTH_PROJECT")

    if last_message:
        write_note(
            f"stop-{session_id}",
            f"{last_message}",
            "sessions",
            project=project,
        )

    return {}


HOOKS = {
    "session_start": hook_session_start,
    "user_prompt_submit": hook_user_prompt_submit,
    "post_compact": hook_post_compact,
    "stop": hook_stop,
}


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <hook_name|tool_name> [json_arguments]", file=sys.stderr)
        print(f"Hook names: {', '.join(HOOKS)}", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    if command in HOOKS:
        data = read_input()
        result = HOOKS[command](data)
        print(json.dumps(result))
        return

    tool_name = command
    arguments = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
    try:
        result = asyncio.run(call_tool(tool_name, arguments))
        print(result)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
