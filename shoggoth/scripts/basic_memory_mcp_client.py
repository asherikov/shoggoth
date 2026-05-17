#!/usr/bin/env python3

import asyncio
import json
import os
import sys

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BM_MCP_URL = os.environ.get("BM_MCP_URL", "http://basic-memory.shoggoth.local/mcp")
BM_MCP_TIMEOUT = int(os.environ.get("BM_MCP_TIMEOUT", "10"))
BM_OTEL_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otelcol.shoggoth.local:4317")
SHOGGOTH_PROJECT = os.environ.get("SHOGGOTH_PROJECT", "")

_resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "basic-memory-mcp-client")})
_tracer_provider = TracerProvider(resource=_resource)
_tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=BM_OTEL_ENDPOINT))
)
trace.set_tracer_provider(_tracer_provider)
_tracer = trace.get_tracer(__name__)


async def call_tool(tool_name: str, arguments: dict | None = None) -> str:
    with _tracer.start_as_current_span(
        f"mcp.call_tool.{tool_name}", attributes={"mcp.tool_name": tool_name}
    ) as span:
        try:
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
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
            raise


def read_input() -> dict:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


async def ensure_project() -> None:
    try:
        result = await call_tool("list_memory_projects", {})
        if f'"{SHOGGOTH_PROJECT}"' in result or f"'{SHOGGOTH_PROJECT}'" in result or f"/{SHOGGOTH_PROJECT}" in result:
            return
    except Exception:
        pass
    try:
        await call_tool("create_memory_project", {"project_name": SHOGGOTH_PROJECT, "project_path": SHOGGOTH_PROJECT})
    except Exception:
        pass


def write_note(title: str, content: str, directory: str) -> None:
    try:
        asyncio.run(ensure_project())
        asyncio.run(call_tool("write_note", {
            "title": title,
            "content": content,
            "directory": directory,
            "project": SHOGGOTH_PROJECT,
        }))
    except Exception:
        pass


def hook_session_start(data: dict) -> dict:
    with _tracer.start_as_current_span("hook.session_start") as span:
        session_id = data.get("session_id", "unknown")
        source = data.get("source", "unknown")
        model = data.get("model", "unknown")
        repo = os.environ.get("SHOGGOTH_REPO", "")
        span.set_attributes({
            "session.id": session_id,
            "session.source": source,
            "session.model": model,
            "project": SHOGGOTH_PROJECT,
        })

        context_parts = ["[Session context]", f"- [project] {SHOGGOTH_PROJECT}"]
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

        ctx = "\n".join(context_parts)[:4096]
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}


def hook_user_prompt_submit(data: dict) -> dict:
    with _tracer.start_as_current_span("hook.user_prompt_submit"):
        session_id = data.get("session_id", "unknown")
        prompt = data.get("prompt", "")
        write_note(
            f"prompt-{session_id}",
            f"User prompt: {prompt}",
            "prompts",
        )
        return {}


def hook_post_compact(data: dict) -> dict:
    with _tracer.start_as_current_span("hook.post_compact"):
        session_id = data.get("session_id", "unknown")
        compact_summary = data.get("compact_summary", "")

        if compact_summary:
            write_note(
                f"compact-summary-{session_id}",
                f"{compact_summary}",
                "compactions",
            )

        return {}


def hook_stop(data: dict) -> dict:
    with _tracer.start_as_current_span("hook.stop"):
        session_id = data.get("session_id", "unknown")
        last_message = data.get("last_assistant_message", "")

        if last_message:
            write_note(
                f"stop-{session_id}",
                f"{last_message}",
                "sessions",
            )

        return {}


HOOKS = {
    "session_start": hook_session_start,
    "user_prompt_submit": hook_user_prompt_submit,
    "post_compact": hook_post_compact,
    "stop": hook_stop,
}


def main():
    if not SHOGGOTH_PROJECT:
        with _tracer.start_as_current_span("init") as span:
            exc = EnvironmentError("SHOGGOTH_PROJECT environment variable is not set")
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
        _tracer_provider.force_flush()
        print("Error: SHOGGOTH_PROJECT environment variable is not set", file=sys.stderr)
        sys.exit(1)

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