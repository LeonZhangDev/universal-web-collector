"""Allowlisted Native Messaging bridge from SiteFilter to Collector.

Stdout is reserved for Chrome/Edge Native Messaging frames.  All diagnostics
are written to stderr and, when LocalAppData is available, a rotating log.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path, PureWindowsPath
import re
import struct
import subprocess
import sys
import time
import unicodedata
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit
import webbrowser


PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_REQUEST_ID_CHARS = 128
MAX_JSON_NESTING = 256
DEFAULT_HTTP_TIMEOUT_SECONDS = 15
PREVIEW_HTTP_TIMEOUT_SECONDS = 150
DISCOVERY_HTTP_TIMEOUT_SECONDS = 3
ALLOWED_ACTIONS = frozenset(
    {"ping", "preview", "create-or-reuse", "get-task", "open-task", "start-login"}
)
ALLOWED_MEDIA = frozenset({"auto", "image", "video", "both"})
CONFIG_RELATIVE_PATH = Path("SiteFilter") / "NativeHost" / "config.json"
_CONTENT_PATH = re.compile(
    r"^/(?:photo/id-[0-9a-f]{13}(?:/\d+)?|video/id-[0-9a-f]{13})\.html$",
    re.IGNORECASE,
)
_EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}/$")
_CONFIG_KEYS = frozenset(
    {
        "protocol_version",
        "allowed_origins",
        "distro",
        "project_path",
        "runtime_file",
        "startup_timeout_seconds",
    }
)
_REQUIRED_CONFIG_KEYS = _CONFIG_KEYS - {"startup_timeout_seconds"}


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_LOCAL_OPENER = request.build_opener(_NoRedirectHandler())


class ProtocolError(Exception):
    def __init__(self, code: str, message: str, retriable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retriable = retriable


def _reject_json_constant(value):
    raise ValueError(f"non-standard JSON number: {value}")


def _decode_json(raw: bytes):
    try:
        _reject_deep_json(raw)
        return json.loads(
            raw.decode("utf-8"), parse_constant=_reject_json_constant
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds decoder limit") from exc


def _reject_deep_json(raw: bytes) -> None:
    """Apply a deterministic nesting limit before platform JSON decoders run."""
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in {0x5B, 0x7B}:  # [ {
            depth += 1
            if depth > MAX_JSON_NESTING:
                raise ValueError("JSON nesting exceeds host limit")
        elif byte in {0x5D, 0x7D} and depth:
            depth -= 1


def _read_bounded_file(path: Path) -> bytes:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_MESSAGE_BYTES + 1)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("JSON file exceeds size limit")
    return raw


def _read_exact(stream, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ProtocolError("invalid-frame", "本机消息不完整，请重试。")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream):
    """Read one four-byte little-endian length-prefixed JSON object."""
    header = stream.read(4)
    if header == b"":
        return None
    if len(header) != 4:
        raise ProtocolError("invalid-frame", "本机消息头不完整，请重试。")
    size = struct.unpack("<I", header)[0]
    if size == 0 or size > MAX_MESSAGE_BYTES:
        raise ProtocolError("request-too-large", "本机请求大小无效，请重试。")
    raw = _read_exact(stream, size)
    try:
        value = _decode_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError("invalid-json", "本机请求格式无效，请重试。") from exc
    if not isinstance(value, dict):
        raise ProtocolError("invalid-request", "本机请求必须是对象。")
    return value


def write_message(stream, value) -> None:
    """Write one response, refusing to emit a frame larger than one MiB."""
    raw = _bounded_json_bytes(value)
    stream.write(struct.pack("<I", len(raw)))
    stream.write(raw)
    stream.flush()


def _bounded_json_bytes(value) -> bytes:
    """Encode incrementally without accumulating more than the frame limit."""
    encoder = json.JSONEncoder(
        ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    output = bytearray()
    try:
        _reject_known_oversized_strings(value)
        for chunk in encoder.iterencode(value):
            encoded = chunk.encode("utf-8")
            if len(output) + len(encoded) > MAX_MESSAGE_BYTES:
                raise ProtocolError(
                    "response-too-large", "Collector 返回内容过大，请在管理页面查看。"
                )
            output.extend(encoded)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolError(
            "collector-invalid-response", "Collector 返回格式异常，请重试。", True
        ) from exc
    return bytes(output)


def _reject_known_oversized_strings(value) -> None:
    """Reject a single huge JSON string before the encoder can allocate it."""
    pending = [value]
    seen = set()
    while pending:
        current = pending.pop()
        if type(current) is str:
            size = 2  # surrounding JSON quotes
            for char in current:
                codepoint = ord(char)
                if char in {'"', "\\"} or char in "\b\f\n\r\t":
                    size += 2
                elif codepoint < 0x20:
                    size += 6
                else:
                    size += len(char.encode("utf-8"))
                if size > MAX_MESSAGE_BYTES:
                    raise ProtocolError(
                        "response-too-large",
                        "Collector 返回内容过大，请在管理页面查看。",
                    )
        elif type(current) is dict:
            identity = id(current)
            if identity not in seen:
                seen.add(identity)
                pending.extend(current.keys())
                pending.extend(current.values())
        elif type(current) in {list, tuple}:
            identity = id(current)
            if identity not in seen:
                seen.add(identity)
                pending.extend(current)


def _fail_payload(message="请求参数无效，请刷新页面后重试。"):
    raise ProtocolError("invalid-payload", message)


def _validate_xchina_url(value) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise ProtocolError("unsupported-url", "仅支持 XChina 的照片或视频详情页。")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ProtocolError("unsupported-url", "仅支持 XChina 的照片或视频详情页。")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            "unsupported-url", "仅支持 XChina 的照片或视频详情页。"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "xchina.co"
        or parsed.hostname != "xchina.co"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not _CONTENT_PATH.fullmatch(parsed.path)
    ):
        raise ProtocolError("unsupported-url", "仅支持 XChina 的照片或视频详情页。")
    return urlunsplit(("https", "xchina.co", parsed.path, "", ""))


def _valid_request_id(value) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= MAX_REQUEST_ID_CHARS
        and not any(
            unicodedata.category(char) in {"Cc", "Cs"} for char in value
        )
    )


def _require_exact_fields(payload: dict, allowed: set[str]) -> None:
    if set(payload) - allowed:
        _fail_payload()


def validate_request(message, caller_origin: str, allowed_origins) -> dict:
    """Validate protocol version, caller, action, and the exact action schema."""
    if (
        type(caller_origin) is not str
        or caller_origin not in set(allowed_origins or ())
    ):
        raise ProtocolError("caller-not-allowed", "此扩展无权使用本机桥接服务。")
    if type(message) is not dict or set(message) != {"v", "id", "action", "payload"}:
        raise ProtocolError("invalid-request", "本机请求结构无效，请重新安装扩展。")
    if type(message["v"]) is not int or message["v"] != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported-version", "本机桥接版本不兼容，请重新运行安装程序。"
        )
    request_id = message["id"]
    if not _valid_request_id(request_id):
        raise ProtocolError("invalid-request-id", "请求编号无效，请重试。")
    action = message["action"]
    if type(action) is not str or action not in ALLOWED_ACTIONS:
        raise ProtocolError("unsupported-action", "此操作不受本机桥接服务支持。")
    payload = message["payload"]
    if type(payload) is not dict:
        _fail_payload()

    if action == "ping":
        _require_exact_fields(payload, set())
    elif action in {"preview", "create-or-reuse"}:
        allowed = {"url", "media"}
        if action == "create-or-reuse":
            allowed.add("force_new")
        _require_exact_fields(payload, allowed)
        normalized_url = _validate_xchina_url(payload.get("url"))
        if "media" in payload:
            if (
                type(payload["media"]) is not str
                or payload["media"] not in ALLOWED_MEDIA
            ):
                _fail_payload("媒体类型无效，请重新选择。")
        if "force_new" in payload and type(payload["force_new"]) is not bool:
            _fail_payload()
    elif action in {"get-task", "open-task"}:
        _require_exact_fields(payload, {"task_id"})
        task_id = payload.get("task_id")
        if type(task_id) is not int or task_id <= 0:
            _fail_payload("任务编号无效，请刷新后重试。")
    elif action == "start-login":
        _require_exact_fields(payload, {"url"})
        normalized_url = _validate_xchina_url(payload.get("url"))

    normalized = dict(message)
    normalized["payload"] = dict(payload)
    if action in {"preview", "create-or-reuse", "start-login"}:
        normalized["payload"]["url"] = normalized_url
    return normalized


def config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ProtocolError(
            "installation-invalid", "找不到本机桥接配置，请重新运行安装程序。"
        )
    return Path(local_app_data) / CONFIG_RELATIVE_PATH


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    try:
        value = _decode_json(_read_bounded_file(Path(path)))
    except (OSError, ValueError) as exc:
        raise ProtocolError(
            "installation-invalid", "本机桥接配置无效，请重新运行安装程序。"
        ) from exc
    if (
        type(value) is not dict
        or set(value) - _CONFIG_KEYS
        or not _REQUIRED_CONFIG_KEYS.issubset(value)
    ):
        raise ProtocolError(
            "installation-invalid", "本机桥接配置无效，请重新运行安装程序。"
        )
    if (
        type(value["protocol_version"]) is not int
        or value["protocol_version"] != PROTOCOL_VERSION
    ):
        raise ProtocolError(
            "unsupported-version", "本机桥接版本不兼容，请重新运行安装程序。"
        )
    if (
        type(value["allowed_origins"]) is not list
        or len(value["allowed_origins"]) != 1
        or not _EXTENSION_ORIGIN.fullmatch(value["allowed_origins"][0])
        or type(value["distro"]) is not str
        or not value["distro"]
        or len(value["distro"]) > 128
        or value["distro"].startswith("-")
        or any(char in value["distro"] for char in "/\\")
        or any(
            unicodedata.category(char) in {"Cc", "Cs"}
            for char in value["distro"]
        )
        or type(value["project_path"]) is not str
        or not value["project_path"].startswith("/")
        or value["project_path"] == "/"
        or len(value["project_path"]) > 2048
        or "\\" in value["project_path"]
        or ".." in Path(value["project_path"]).parts
        or any(
            unicodedata.category(char) in {"Cc", "Cs"}
            for char in value["project_path"]
        )
        or type(value["runtime_file"]) is not str
        or not value["runtime_file"]
        or len(value["runtime_file"]) > 2048
        or any(
            unicodedata.category(char) in {"Cc", "Cs"}
            for char in value["runtime_file"]
        )
    ):
        raise ProtocolError(
            "installation-invalid", "本机桥接配置无效，请重新运行安装程序。"
        )
    runtime = value["runtime_file"]
    runtime_is_absolute = (
        PureWindowsPath(runtime).is_absolute()
        if os.name == "nt"
        else Path(runtime).is_absolute()
    )
    timeout = value.get("startup_timeout_seconds", 60)
    if (
        not runtime_is_absolute
        or ".." in PureWindowsPath(runtime).parts
        or not PureWindowsPath(runtime).name
        or type(timeout) not in {int, float}
        or not 1 <= timeout <= 120
    ):
        raise ProtocolError(
            "installation-invalid", "本机桥接配置无效，请重新运行安装程序。"
        )
    return value


def _read_bounded_body(response) -> bytes:
    output = bytearray()
    while len(output) <= MAX_MESSAGE_BYTES:
        chunk = response.read(min(64 * 1024, MAX_MESSAGE_BYTES + 1 - len(output)))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
    raise ProtocolError("response-too-large", "Collector 返回内容过大，请在管理页面查看。")


def _open_local_json(url: str, *, timeout, opener=None):
    open_url = opener or _LOCAL_OPENER.open
    expected_url = url.full_url if isinstance(url, request.Request) else url
    with open_url(url, timeout=timeout) as response:
        final_url = response.geturl() if hasattr(response, "geturl") else expected_url
        if final_url != expected_url:
            raise ProtocolError(
                "collector-invalid-response", "Collector 返回了不安全的跳转。"
            )
        raw = _read_bounded_body(response)
    try:
        return _decode_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError(
            "collector-invalid-response", "Collector 返回格式异常，请重试。", True
        ) from exc


def _http_json(
    port: int,
    method: str,
    path: str,
    body=None,
    *,
    timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
    opener=None,
):
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        return _open_local_json(req, timeout=timeout, opener=opener)
    except error.HTTPError as exc:
        code = "collector-request-failed"
        if exc.code == 401 or exc.code == 403:
            code = "login-required"
        raise ProtocolError(code, "Collector 无法完成请求，请打开管理页面查看。", exc.code >= 500) from exc
    except (OSError, error.URLError) as exc:
        raise ProtocolError("collector-unavailable", "Collector 暂时不可用，请重试。", True) from exc


def collector_healthy(port: int, opener=None) -> bool:
    try:
        base = f"http://127.0.0.1:{port}"
        health = _open_local_json(
            f"{base}/healthz",
            timeout=DISCOVERY_HTTP_TIMEOUT_SECONDS,
            opener=opener,
        )
        config = _open_local_json(
            f"{base}/config",
            timeout=DISCOVERY_HTTP_TIMEOUT_SECONDS,
            opener=opener,
        )
        collectors = config.get("collectors") if isinstance(config, dict) else None
        resource_types = config.get("resource_types") if isinstance(config, dict) else None
        return health == {"status": "ok"} and (
            isinstance(config, dict)
            and config.get("auto_collector") == "auto"
            and isinstance(collectors, list)
            and {"xchina_gallery", "xchina_video"}.issubset(collectors)
            and isinstance(resource_types, list)
            and {"image", "video"}.issubset(resource_types)
        )
    except Exception:
        return False


def _runtime_port(runtime_file: Path, health_probe) -> int | None:
    try:
        descriptor = _decode_json(_read_bounded_file(runtime_file))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None
    if type(descriptor) is not dict:
        return None
    required = {"protocol_version", "port", "owned", "ready"}
    if not required.issubset(descriptor):
        return None
    port = descriptor["port"]
    owned = descriptor["owned"]
    pid = descriptor.get("pid")
    if (
        type(descriptor["protocol_version"]) is not int
        or descriptor["protocol_version"] != PROTOCOL_VERSION
        or type(descriptor["ready"]) is not bool
        or descriptor["ready"] is not True
        or type(owned) is not bool
        or type(port) is not int
        or not 1 <= port <= 65535
        or (pid is not None and (type(pid) is not int or pid <= 0 or not owned))
    ):
        return None
    return port if health_probe(port) else None


def ensure_collector(
    config: dict,
    *,
    health_probe=collector_healthy,
    popen=subprocess.Popen,
    sleeper=time.sleep,
) -> int:
    """Discover Collector or start only the installer-configured WSL project."""
    runtime_file = Path(config["runtime_file"])
    port = _runtime_port(runtime_file, health_probe)
    if port is not None:
        return port

    log_path = config_path().with_name("collector-startup.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_stream = log_path.open("ab")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = popen(
            [
                "wsl.exe",
                "-d",
                config["distro"],
                "--cd",
                config["project_path"],
                "--",
                "make",
                "start-native",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=log_stream,
            shell=False,
            creationflags=creationflags,
        )
    except OSError as exc:
        log_stream.close()
        raise ProtocolError("wsl-unavailable", "无法启动 WSL，请确认 WSL 已安装。", True) from exc

    timeout = config.get("startup_timeout_seconds", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
        timeout = 60
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            port = _runtime_port(runtime_file, health_probe)
            if port is not None:
                return port
            if process.poll() is not None:
                raise ProtocolError(
                    "collector-startup-failed",
                    "Collector 启动进程提前退出，请检查桥接日志。",
                    True,
                )
            sleeper(0.25)
    finally:
        log_stream.close()
    raise ProtocolError("collector-startup-failed", "Collector 未能及时启动，请检查桥接日志。", True)


def _success(request_id: str, result):
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result}


def _error(request_id: str, exc: ProtocolError):
    return {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": {"code": exc.code, "message": exc.message, "retriable": exc.retriable},
    }


def _safe_request_id(message) -> str:
    request_id = message.get("id", "") if isinstance(message, dict) else ""
    return request_id if _valid_request_id(request_id) else ""


def process_request(
    message,
    *,
    caller_origin: str,
    config: dict,
    ensure=ensure_collector,
    requester=_http_json,
    browser_open=webbrowser.open,
):
    request_id = _safe_request_id(message)
    try:
        validated = validate_request(message, caller_origin, config.get("allowed_origins"))
        request_id = validated["id"]
        action = validated["action"]
        payload = validated["payload"]
        port = ensure(config)
        if action == "ping":
            result = requester(
                port, "GET", "/healthz", timeout=DEFAULT_HTTP_TIMEOUT_SECONDS
            )
            result = {"protocol_version": PROTOCOL_VERSION, "port": port, "collector": result}
        elif action == "preview":
            body = {"url": payload["url"], "collector": "auto"}
            if "media" in payload:
                body["media"] = payload["media"]
            result = requester(
                port,
                "POST",
                "/tasks/preview",
                body,
                timeout=PREVIEW_HTTP_TIMEOUT_SECONDS,
            )
        elif action == "create-or-reuse":
            body = {
                "url": payload["url"],
                "collector": "auto",
                "deduplicate": True,
                "force_new": payload.get("force_new", False),
                "incremental": True,
            }
            if "media" in payload:
                body["media"] = payload["media"]
            result = requester(
                port,
                "POST",
                "/tasks/create",
                body,
                timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
        elif action == "get-task":
            result = requester(
                port,
                "GET",
                f"/tasks/{payload['task_id']}",
                timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
        elif action == "open-task":
            destination = f"http://127.0.0.1:{port}/?task={payload['task_id']}"
            result = {"opened": bool(browser_open(destination))}
        else:  # start-login
            result = requester(
                port,
                "POST",
                "/sessions/login",
                {"url": payload["url"]},
                timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
        return _success(request_id, result)
    except ProtocolError as exc:
        return _error(request_id, exc)
    except Exception as exc:
        logging.getLogger("sitefilter-native-host").error(
            "native-host failure stage=process_request type=%s",
            type(exc).__name__,
        )
        return _error(
            request_id,
            ProtocolError(
                "internal-error", "本机桥接服务发生内部错误，请重试。", True
            ),
        )


def configure_logging() -> None:
    logger = logging.getLogger("sitefilter-native-host")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(sys.stderr))
    try:
        path = config_path().with_name("native-host.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.addHandler(RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"))
    except (OSError, ProtocolError):
        pass


def main(argv=None, stdin=None, stdout=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    configure_logging()
    caller_origin = argv[0] if argv else ""
    try:
        config = load_config()
    except ProtocolError as exc:
        config_error = exc
        config = {"allowed_origins": []}
    else:
        config_error = None

    while True:
        try:
            message = read_message(stdin)
        except ProtocolError as exc:
            write_message(stdout, _error("", exc))
            return 1
        if message is None:
            return 0
        if config_error is not None:
            response = _error(_safe_request_id(message), config_error)
        else:
            response = process_request(message, caller_origin=caller_origin, config=config)
        try:
            write_message(stdout, response)
        except ProtocolError as exc:
            write_message(stdout, _error(_safe_request_id(response), exc))


if __name__ == "__main__":
    raise SystemExit(main())
