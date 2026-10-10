"""Module split out of shoggoth.py; see package __init__."""
import json
import time
import uuid

from .http import HttpApiClient
from .util import log

class OtlpLogger:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.session_id = None
        self.service_name = None
        self.last_result = None
        self.last_error = None
        self.assistant_text = []
        self._pushed_count = 0
        self._failed_count = 0
        self._logged_endpoint = False
        log(f"otlp: OtlpLogger created endpoint={self.endpoint}")

    def _push_line(self, line, severity="INFO"):
        if not self._logged_endpoint:
            log(f"otlp: endpoint={self.endpoint} (POST {self.endpoint}/v1/logs)")
            self._logged_endpoint = True
        ts = str(time.time_ns())
        severity_number = {
            "TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13,
            "ERROR": 17, "FATAL": 21,
        }.get(severity, 9)
        body = {
            "resourceLogs": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name",
                         "value": {"stringValue": self.service_name}},
                        {"key": "session.id",
                         "value": {"stringValue": self.session_id}},
                    ]
                },
                "scopeLogs": [{
                    "scope": {},
                    "logRecords": [{
                        "timeUnixNano": ts,
                        "observedTimeUnixNano": ts,
                        "severityNumber": severity_number,
                        "severityText": severity,
                        "body": {"stringValue": line},
                    }],
                }],
            }]
        }
        result = HttpApiClient.http_post_json(f"{self.endpoint}/v1/logs", body, quiet=True)
        self._pushed_count += 1
        if result is None:
            self._failed_count += 1
            log(f"otlp: POST failed (count={self._pushed_count} "
                f"failed={self._failed_count}); line: {line[:120]!r}")

    def start_session(self, event_type):
        self.service_name = f"qwen-{event_type}"
        self.reset()
        self.session_id = str(uuid.uuid4())
        self._pushed_count = 0
        self._failed_count = 0
        self._logged_endpoint = False
        log(f"otlp: start_session session_id={self.session_id} "
            f"service={self.service_name} endpoint={self.endpoint}")

    def reset(self):
        self.last_result = None
        self.last_error = None
        self.assistant_text = []
        # NOTE: do not clear ``session_id`` here. The session id is the
        # qwen conversation identity and must persist across prompt()
        # calls within the same session so subsequent prompts that pass
        # ``resume=True`` can still find the id to pass as ``--resume``.
        # ``prompt()`` calls ``reset()`` right before launching qwen; if
        # reset() nuked session_id, the next prompt() in the workflow
        # (``pr-comment`` chains several ``resume=True`` prompts after
        # ``start_session``) would build a cmd with ``--resume None`` and
        # crash the logging join. Callers that want a fresh session
        # (``start_session``, ``prompt_with_retry`` on leaked-tag retry)
        # assign ``session_id`` themselves.

    def stop_session(self):
        log(f"otlp: stop_session pushed={self._pushed_count} "
            f"failed={self._failed_count}")

    def _capture_stream_json(self, line):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        etype = event.get("type")
        if etype == "assistant":
            content = event.get("message", {}).get("content", [])
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        self.assistant_text.append(text)
        elif etype == "result":
            result_text = event.get("result", "") or ""
            if event.get("is_error"):
                self.last_error = result_text
                self.last_result = None
            elif result_text.lstrip().startswith("[API Error") or result_text.lstrip().startswith("API Error"):
                self.last_error = result_text
                self.last_result = None
            else:
                self.last_result = result_text

    def get_review_text(self):
        if self.last_error:
            return ""
        if self.last_result:
            return str(self.last_result)
        return "\n\n".join(self.assistant_text) if self.assistant_text else ""
