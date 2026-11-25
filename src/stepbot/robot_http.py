"""Zero-dependency Opentrons Robot Server HTTP Run API v2 hardware adapter (`:31950`)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any, Protocol, runtime_checkable
import urllib.error
import urllib.parse
import urllib.request
import uuid

from stepbot.models import McpSecurityViolation

OPENTRONS_HTTP_API_VERSION = "3"

_ACTIVE_RUN_STATUSES: frozenset[str] = frozenset(
    {"running", "paused", "finishing", "awaiting-recovery", "awaiting-recovery-paused"}
)
_TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "stopped", "blocked-by-open-door"}
)
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class HardwareRunResult:
    """Terminal execution summary returned by an Opentrons Robot Server HTTP v2 run."""

    ok: bool
    protocol_id: str
    run_id: str
    run_status: str
    robot_name: str
    robot_model: str
    api_version: str
    command_count: int = 0
    errors: tuple[str, ...] = ()


@runtime_checkable
class HardwareRunAdapter(Protocol):
    """Transport interface for hardware-in-the-loop Opentrons protocol execution."""

    def preflight_check(self) -> dict[str, Any]:
        """Verify robot health and ensure no active run is currently executing."""

    def upload_and_create_run(
        self,
        python_code: str,
        *,
        expected_sha256: str,
        filename: str = "stepbot_protocol.py",
    ) -> tuple[str, str]:
        """Upload LF-normalized protocol bytes and create an idle run `(protocol_id, run_id)`."""

    def play_and_wait(
        self,
        protocol_id: str,
        run_id: str,
        health: Mapping[str, Any],
    ) -> HardwareRunResult:
        """Start physical execution (`play`) and poll until a terminal run state is reached."""


def _encode_multipart_protocol(field_name: str, filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = f"stepbotBoundary{uuid.uuid4().hex}"
    header_lines = [
        f"--{boundary}".encode("ascii"),
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode("ascii"),
        b"Content-Type: text/x-python; charset=utf-8",
        b"",
        file_bytes,
        f"--{boundary}--".encode("ascii"),
        b"",
    ]
    body = b"\r\n".join(header_lines)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


class OpentronsHttpRunAdapter:
    """Stdlib `urllib.request` client for the Opentrons Robot Server HTTP Run API v2 (`:31950`).

    Note: Physical robot execution requires a reachable OT-2 or Flex robot server
    (`http://<robot-ip>:31950`). Automated CI exercises this adapter deterministically
    against a local loopback `ThreadingHTTPServer` implementing `/health`, `/protocols`,
    and `/runs`.
    """

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 300.0,
        poll_interval_s: float = 0.5,
        *,
        request_timeout_s: float = 15.0,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty HTTP(S) URL")
        parsed = urllib.parse.urlsplit(base_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(
                f"Invalid robot base_url {base_url!r}: expected 'http://<host>:31950' or 'https://...'"
            )
        if isinstance(timeout_s, bool) or timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if isinstance(poll_interval_s, bool) or poll_interval_s < 0:
            raise ValueError("poll_interval_s must be >= 0")
        if isinstance(request_timeout_s, bool) or request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be > 0")

        self.base_url: str = base_url.strip().rstrip("/")
        self.timeout_s: float = float(timeout_s)
        self.poll_interval_s: float = float(poll_interval_s)
        self.request_timeout_s: float = float(request_timeout_s)
        if parsed.hostname.lower() in _LOOPBACK_HOSTS:
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {
            "Opentrons-Version": OPENTRONS_HTTP_API_VERSION,
            "Accept": "application/json",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.request_timeout_s) as resp:
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            suffix = f" ({detail})" if detail else ""
            raise McpSecurityViolation(
                "mcp_hardware_http_error",
                f"Opentrons HTTP {method} {path} failed with status {exc.code}{suffix}",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise McpSecurityViolation(
                "mcp_hardware_unreachable",
                f"Cannot reach Opentrons Robot Server at {url}: {exc}",
            ) from exc

        try:
            decoded = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpSecurityViolation(
                "mcp_hardware_protocol_error",
                f"Non-JSON response from Opentrons {method} {path}: {exc}",
            ) from exc
        if not isinstance(decoded, dict):
            raise McpSecurityViolation(
                "mcp_hardware_protocol_error",
                f"Expected JSON object from Opentrons {method} {path}",
            )
        result: dict[str, Any] = {str(k): v for k, v in decoded.items()}
        return result

    def preflight_check(self) -> dict[str, Any]:
        health = self._request_json("GET", "/health")
        runs_payload = self._request_json("GET", "/runs")
        raw_runs = runs_payload.get("data", [])
        if not isinstance(raw_runs, list):
            raise McpSecurityViolation(
                "mcp_hardware_protocol_error",
                "Expected 'data' list in Opentrons GET /runs response",
            )
        for entry in raw_runs:
            if isinstance(entry, Mapping):
                status = str(entry.get("status", ""))
                if status in _ACTIVE_RUN_STATUSES:
                    active_id = str(entry.get("id", "<unknown>"))
                    raise McpSecurityViolation(
                        "mcp_hardware_busy",
                        f"Robot server already has an active run '{active_id}' in state {status!r}",
                    )
        return {
            "name": str(health.get("name", "opentrons-robot")),
            "api_version": str(health.get("api_version", "")),
            "robot_model": str(health.get("robot_model", health.get("robot_serial", "OT-2"))),
            "robot_serial": str(health.get("robot_serial", "")),
        }

    def upload_and_create_run(
        self,
        python_code: str,
        *,
        expected_sha256: str,
        filename: str = "stepbot_protocol.py",
    ) -> tuple[str, str]:
        normalized_bytes = python_code.replace("\r\n", "\n").encode("utf-8")
        actual_sha = hashlib.sha256(normalized_bytes).hexdigest()
        if actual_sha.lower() != expected_sha256.lower():
            raise McpSecurityViolation(
                "mcp_payload_binding",
                f"Protocol upload bytes SHA-256 ({actual_sha}) != verified envelope ({expected_sha256})",
            )
        multipart_body, content_type = _encode_multipart_protocol(
            "files", filename, normalized_bytes
        )
        proto_resp = self._request_json(
            "POST",
            "/protocols",
            body=multipart_body,
            content_type=content_type,
        )
        proto_data = proto_resp.get("data")
        if not isinstance(proto_data, Mapping) or not str(proto_data.get("id", "")).strip():
            raise McpSecurityViolation(
                "mcp_hardware_protocol_error",
                "Opentrons POST /protocols response missing data.id",
            )
        protocol_id = str(proto_data["id"])

        run_req_bytes = json.dumps({"data": {"protocolId": protocol_id}}).encode("utf-8")
        run_resp = self._request_json(
            "POST",
            "/runs",
            body=run_req_bytes,
            content_type="application/json",
        )
        run_data = run_resp.get("data")
        if not isinstance(run_data, Mapping) or not str(run_data.get("id", "")).strip():
            raise McpSecurityViolation(
                "mcp_hardware_protocol_error",
                "Opentrons POST /runs response missing data.id",
            )
        run_id = str(run_data["id"])
        return protocol_id, run_id

    def _post_run_action(self, run_id: str, action_type: str) -> None:
        payload = json.dumps({"data": {"actionType": action_type}}).encode("utf-8")
        self._request_json(
            "POST",
            f"/runs/{urllib.parse.quote(run_id, safe='')}/actions",
            body=payload,
            content_type="application/json",
        )

    def _safe_stop_run(self, run_id: str) -> None:
        try:
            self._post_run_action(run_id, "stop")
        except McpSecurityViolation:
            return

    def play_and_wait(
        self,
        protocol_id: str,
        run_id: str,
        health: Mapping[str, Any],
    ) -> HardwareRunResult:
        self._post_run_action(run_id, "play")
        deadline = time.monotonic() + self.timeout_s
        quoted_run = urllib.parse.quote(run_id, safe="")

        while True:
            poll_resp = self._request_json("GET", f"/runs/{quoted_run}")
            run_data = poll_resp.get("data")
            if not isinstance(run_data, Mapping):
                raise McpSecurityViolation(
                    "mcp_hardware_protocol_error",
                    f"Opentrons GET /runs/{run_id} missing data object",
                )
            status = str(run_data.get("status", ""))
            raw_errors = run_data.get("errors", [])
            err_msgs: list[str] = []
            if isinstance(raw_errors, list):
                for item in raw_errors:
                    if isinstance(item, Mapping):
                        err_msgs.append(
                            str(item.get("detail") or item.get("errorType") or item)
                        )
                    else:
                        err_msgs.append(str(item))
            cmd_summary = run_data.get("commands", [])
            cmd_count = len(cmd_summary) if isinstance(cmd_summary, list) else int(
                run_data.get("commandCount", 0)
                if isinstance(run_data.get("commandCount"), int)
                else 0
            )

            if status in _TERMINAL_RUN_STATUSES:
                if status == "blocked-by-open-door":
                    self._safe_stop_run(run_id)
                return HardwareRunResult(
                    ok=(status == "succeeded"),
                    protocol_id=protocol_id,
                    run_id=run_id,
                    run_status=status,
                    robot_name=str(health.get("name", "opentrons-robot")),
                    robot_model=str(health.get("robot_model", "OT-2")),
                    api_version=str(health.get("api_version", "")),
                    command_count=cmd_count,
                    errors=tuple(err_msgs),
                )

            now = time.monotonic()
            if now >= deadline:
                self._safe_stop_run(run_id)
                return HardwareRunResult(
                    ok=False,
                    protocol_id=protocol_id,
                    run_id=run_id,
                    run_status="timeout",
                    robot_name=str(health.get("name", "opentrons-robot")),
                    robot_model=str(health.get("robot_model", "OT-2")),
                    api_version=str(health.get("api_version", "")),
                    command_count=cmd_count,
                    errors=(
                        *err_msgs,
                        f"Opentrons run '{run_id}' exceeded timeout_s={self.timeout_s:g}s; stop action issued",
                    ),
                )

            if self.poll_interval_s > 0:
                time.sleep(min(self.poll_interval_s, max(0.0, deadline - now)))
