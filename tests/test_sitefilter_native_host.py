import importlib.util
import json
import logging
import os
import struct
import subprocess
import sys
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOST_PATH = ROOT / "integrations" / "sitefilter-native-host" / "host.py"
SPEC = importlib.util.spec_from_file_location("sitefilter_native_host", HOST_PATH)
host = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(host)

ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"
PHOTO_URL = "https://xchina.co/photo/id-6664761937f5a.html"
VIDEO_URL = "https://xchina.co/video/id-6aaee7c9a12e8.html"


def framed(value):
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def base_request(action="ping", payload=None, request_id="request-01"):
    return {
        "v": 1,
        "id": request_id,
        "action": action,
        "payload": {} if payload is None else payload,
    }


def test_native_framing_round_trip_and_clean_eof():
    request = base_request(request_id="精确-id-01")
    assert host.read_message(BytesIO(framed(request))) == request

    output = BytesIO()
    host.write_message(output, {"v": 1, "id": request["id"], "ok": True, "result": {}})
    output.seek(0)
    assert host.read_message(output) == {
        "v": 1,
        "id": "精确-id-01",
        "ok": True,
        "result": {},
    }
    assert host.read_message(BytesIO()) is None


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_input_rejects_non_standard_json_numbers(constant):
    raw = b'{"v":1,"id":"id","action":"ping","payload":{"x":' + constant + b"}}"
    with pytest.raises(host.ProtocolError) as exc:
        host.read_message(BytesIO(struct.pack("<I", len(raw)) + raw))
    assert exc.value.code == "invalid-json"


def test_output_rejects_non_standard_json_numbers():
    with pytest.raises(host.ProtocolError) as exc:
        host.write_message(
            BytesIO(), {"v": 1, "id": "id", "ok": True, "result": float("nan")}
        )
    assert exc.value.code == "collector-invalid-response"


def test_response_larger_than_one_mib_is_rejected_before_write():
    output = BytesIO()
    with pytest.raises(host.ProtocolError) as exc:
        host.write_message(
            output,
            {"v": 1, "id": "large", "ok": True, "result": "x" * host.MAX_MESSAGE_BYTES},
        )
    assert exc.value.code == "response-too-large"
    assert output.getvalue() == b""


def test_oversized_nested_response_uses_bounded_encoder_not_full_dump(monkeypatch):
    def forbidden_full_dump(*args, **kwargs):
        raise AssertionError("write_message must not call json.dumps")

    monkeypatch.setattr(host.json, "dumps", forbidden_full_dump)
    output = BytesIO()
    nested = {"items": ["x" * 8192 for _ in range(200)]}

    with pytest.raises(host.ProtocolError) as exc:
        host.write_message(
            output,
            {"v": 1, "id": "bounded", "ok": True, "result": nested},
        )

    assert exc.value.code == "response-too-large"
    assert output.getvalue() == b""


def test_single_oversized_string_is_rejected_before_json_encoder(monkeypatch):
    def forbidden_encoder(*args, **kwargs):
        raise AssertionError("known-oversized string must fail during preflight")

    monkeypatch.setattr(host.json.JSONEncoder, "iterencode", forbidden_encoder)
    with pytest.raises(host.ProtocolError) as exc:
        host.write_message(
            BytesIO(),
            {"v": 1, "id": "bounded", "ok": True, "result": "x" * host.MAX_MESSAGE_BYTES},
        )
    assert exc.value.code == "response-too-large"


def test_validation_preserves_exact_request_id_and_accepts_only_exact_origin():
    request = base_request(request_id="Case-Sensitive-007")
    assert host.validate_request(request, ORIGIN, {ORIGIN})["id"] == "Case-Sensitive-007"

    with pytest.raises(host.ProtocolError) as exc:
        host.validate_request(request, ORIGIN.rstrip("/"), {ORIGIN})
    assert exc.value.code == "caller-not-allowed"


@pytest.mark.parametrize("action", ["shell", "fetch", "GET /config", "Ping"])
def test_unsupported_actions_are_rejected(action):
    with pytest.raises(host.ProtocolError) as exc:
        host.validate_request(base_request(action=action), ORIGIN, {ORIGIN})
    assert exc.value.code == "unsupported-action"


@pytest.mark.parametrize(
    "url",
    [
        "http://xchina.co/photo/id-6664761937f5a.html",
        "https://www.xchina.co/photo/id-6664761937f5a.html",
        "https://xchina.co:443/photo/id-6664761937f5a.html",
        "https://user@xchina.co/photo/id-6664761937f5a.html",
        "https://xchina.co\\@evil.test/photo/id-6664761937f5a.html",
        "https://xchina.co/other/id-6664761937f5a.html",
        "https://xchina.co/photo/id-not-an-id.html",
    ],
)
def test_non_xchina_or_ambiguous_urls_are_rejected(url):
    with pytest.raises(host.ProtocolError) as exc:
        host.validate_request(
            base_request("preview", {"url": url, "media": "auto"}), ORIGIN, {ORIGIN}
        )
    assert exc.value.code == "unsupported-url"


def test_query_and_fragment_are_stripped_before_forwarding():
    calls = []
    variant = PHOTO_URL + "?next=https://evil.test/token#private-secret"

    response = host.process_request(
        base_request("preview", {"url": variant, "media": "image"}),
        caller_origin=ORIGIN,
        config={"allowed_origins": [ORIGIN]},
        ensure=lambda _: 8123,
        requester=lambda port, method, path, body=None, *, timeout: calls.append(body) or {},
    )

    assert response["ok"] is True
    assert calls == [{"url": PHOTO_URL, "collector": "auto", "media": "image"}]

    response = host.process_request(
        base_request("create-or-reuse", {"url": variant, "force_new": False}),
        caller_origin=ORIGIN,
        config={"allowed_origins": [ORIGIN]},
        ensure=lambda _: 8123,
        requester=lambda port, method, path, body=None, *, timeout: calls.append(body) or {},
    )
    assert response["ok"] is True
    assert calls[-1]["url"] == PHOTO_URL


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("ping", {"endpoint": "/healthz"}),
        ("preview", {"url": PHOTO_URL, "shell": "rm -rf /"}),
        ("create-or-reuse", {"url": PHOTO_URL, "command": "calc.exe"}),
        ("get-task", {"task_id": 1, "path": "/config"}),
        ("open-task", {"task_id": 1, "port": 9999}),
        ("start-login", {"url": VIDEO_URL, "executable": "cmd.exe"}),
    ],
)
def test_arbitrary_endpoint_shell_and_process_fields_are_rejected(action, payload):
    with pytest.raises(host.ProtocolError) as exc:
        host.validate_request(base_request(action, payload), ORIGIN, {ORIGIN})
    assert exc.value.code == "invalid-payload"


@pytest.mark.parametrize("task_id", [True, 0, -1, 1.2, "1"])
def test_task_ids_are_positive_integers(task_id):
    with pytest.raises(host.ProtocolError):
        host.validate_request(
            base_request("get-task", {"task_id": task_id}), ORIGIN, {ORIGIN}
        )


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("v", 1.0, "unsupported-version"),
        ("v", True, "unsupported-version"),
        ("id", ["request"], "invalid-request-id"),
        ("id", {"request": 1}, "invalid-request-id"),
        ("action", ["ping"], "unsupported-action"),
        ("action", {"name": "ping"}, "unsupported-action"),
    ],
)
def test_envelope_values_require_exact_types(field, value, expected_code):
    message = base_request()
    message[field] = value
    with pytest.raises(host.ProtocolError) as exc:
        host.validate_request(message, ORIGIN, {ORIGIN})
    assert exc.value.code == expected_code


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("preview", {"url": [PHOTO_URL]}),
        ("preview", {"url": {"value": PHOTO_URL}}),
        ("preview", {"url": PHOTO_URL, "media": ["image"]}),
        ("preview", {"url": PHOTO_URL, "media": {"value": "image"}}),
        ("create-or-reuse", {"url": PHOTO_URL, "force_new": 1}),
        ("create-or-reuse", {"url": PHOTO_URL, "force_new": "false"}),
        ("start-login", {"url": 123}),
    ],
)
def test_payload_values_require_exact_types(action, payload):
    with pytest.raises(host.ProtocolError) as exc:
        host.validate_request(base_request(action, payload), ORIGIN, {ORIGIN})
    assert exc.value.code in {"invalid-payload", "unsupported-url"}


@pytest.mark.parametrize(
    "message",
    [
        {"v": 1.0, "id": "id", "action": "ping", "payload": {}},
        {"v": 1, "id": "id", "action": ["ping"], "payload": {}},
        {
            "v": 1,
            "id": "id",
            "action": "preview",
            "payload": {"url": PHOTO_URL, "media": ["image"]},
        },
    ],
)
def test_malformed_types_return_stable_protocol_errors(message):
    response = host.process_request(
        message,
        caller_origin=ORIGIN,
        config={"allowed_origins": [ORIGIN]},
        ensure=lambda _: (_ for _ in ()).throw(AssertionError("must not start")),
    )
    assert response["ok"] is False
    assert response["id"] == "id"
    assert response["error"]["code"] != "internal-error"


def test_action_dispatch_uses_only_fixed_collector_endpoints():
    calls = []

    def request_json(port, method, path, body=None, *, timeout):
        calls.append((port, method, path, body, timeout))
        return {"path": path}

    config = {"allowed_origins": [ORIGIN]}
    cases = [
        ("ping", {}, ("GET", "/healthz", None, host.DEFAULT_HTTP_TIMEOUT_SECONDS)),
        ("preview", {"url": PHOTO_URL, "media": "image"}, ("POST", "/tasks/preview", {"url": PHOTO_URL, "collector": "auto", "media": "image"}, host.PREVIEW_HTTP_TIMEOUT_SECONDS)),
        ("create-or-reuse", {"url": VIDEO_URL, "media": "video", "force_new": False}, ("POST", "/tasks/create", {"url": VIDEO_URL, "collector": "auto", "media": "video", "deduplicate": True, "force_new": False, "incremental": True}, host.DEFAULT_HTTP_TIMEOUT_SECONDS)),
        ("get-task", {"task_id": 42}, ("GET", "/tasks/42", None, host.DEFAULT_HTTP_TIMEOUT_SECONDS)),
        ("start-login", {"url": VIDEO_URL}, ("POST", "/sessions/login", {"url": VIDEO_URL}, host.DEFAULT_HTTP_TIMEOUT_SECONDS)),
    ]
    for action, payload, expected in cases:
        response = host.process_request(
            base_request(action, payload, request_id=f"id-{action}"),
            caller_origin=ORIGIN,
            config=config,
            ensure=lambda _: 8123,
            requester=request_json,
        )
        assert response["id"] == f"id-{action}"
        assert response["ok"] is True
        assert calls[-1] == (8123, *expected)

    assert host.PREVIEW_HTTP_TIMEOUT_SECONDS == 150
    assert host.DEFAULT_HTTP_TIMEOUT_SECONDS < host.PREVIEW_HTTP_TIMEOUT_SECONDS


def test_collector_capability_probe_rejects_generic_impostor_and_accepts_real_shape():
    def response(value):
        return BytesIO(json.dumps(value).encode("utf-8"))

    def generic_opener(url, timeout):
        if url.endswith("/healthz"):
            return response({"status": "ok"})
        return response({"resource_types": [], "collectors": [], "auto_collector": "auto"})

    assert host.collector_healthy(8123, opener=generic_opener) is False

    def collector_opener(url, timeout):
        if url.endswith("/healthz"):
            return response({"status": "ok"})
        return response(
            {
                "resource_types": ["audio", "doc", "image", "text", "video"],
                "collectors": ["generic", "xchina_gallery", "xchina_video"],
                "auto_collector": "auto",
            }
        )

    assert host.collector_healthy(8123, opener=collector_opener) is True


def test_discovery_rejects_oversized_body_before_json_parse():
    oversized = b"{" + b" " * host.MAX_MESSAGE_BYTES + b"}"

    def opener(url, timeout):
        if url.endswith("/healthz"):
            return BytesIO(b'{"status":"ok"}')
        return BytesIO(oversized)

    assert host.collector_healthy(8123, opener=opener) is False


def test_discovery_reads_partial_body_and_forwards_timeout():
    seen = []

    class PartialResponse:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def read(self, size=-1):
            return self.chunks.pop(0) if self.chunks else b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    bodies = {
        "/healthz": [b'{"status":', b'"ok"}'],
        "/config": [
            b'{"resource_types":["image","video"],',
            b'"collectors":["xchina_gallery","xchina_video"],',
            b'"auto_collector":"auto"}',
        ],
    }

    def opener(url, timeout):
        seen.append(timeout)
        suffix = "/healthz" if url.endswith("/healthz") else "/config"
        return PartialResponse(bodies[suffix])

    assert host.collector_healthy(8123, opener=opener) is True
    assert seen == [host.DISCOVERY_HTTP_TIMEOUT_SECONDS] * 2


def test_discovery_timeout_is_rejected():
    def opener(url, timeout):
        assert timeout == host.DISCOVERY_HTTP_TIMEOUT_SECONDS
        raise TimeoutError("secret-token")

    assert host.collector_healthy(8123, opener=opener) is False


def test_discovery_rejects_incomplete_json_body():
    def opener(url, timeout):
        if url.endswith("/healthz"):
            return BytesIO(b'{"status":"ok"}')
        return BytesIO(b'{"resource_types":["image","video"]')

    assert host.collector_healthy(8123, opener=opener) is False


def test_collector_response_rejects_deep_json_as_stable_error():
    deep = (b"[" * 5000) + b"0" + (b"]" * 5000)
    with pytest.raises(host.ProtocolError) as exc:
        host._http_json(
            8123,
            "GET",
            "/healthz",
            opener=lambda url, timeout: BytesIO(deep),
        )
    assert exc.value.code == "collector-invalid-response"


def test_local_http_redirects_are_not_followed():
    destination_hits = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            destination_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, format, *args):
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)
    destination_thread = Thread(target=destination.serve_forever, daemon=True)
    destination_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{destination.server_port}/stolen"
            )
            self.end_headers()

        def log_message(self, format, *args):
            pass

    redirector = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = Thread(target=redirector.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        with pytest.raises(host.ProtocolError):
            host._http_json(redirector.server_port, "GET", "/healthz")
        assert host.collector_healthy(redirector.server_port) is False
        assert destination_hits == []
    finally:
        redirector.shutdown()
        redirector.server_close()
        destination.shutdown()
        destination.server_close()


def test_open_task_constructs_only_fixed_local_deep_link():
    opened = []
    response = host.process_request(
        base_request("open-task", {"task_id": 42}),
        caller_origin=ORIGIN,
        config={"allowed_origins": [ORIGIN]},
        ensure=lambda _: 8123,
        browser_open=lambda url: opened.append(url) or True,
    )
    assert response["ok"] is True
    assert opened == ["http://127.0.0.1:8123/?task=42"]


def test_unexpected_failures_are_redacted_and_keep_request_id():
    response = host.process_request(
        base_request("ping", request_id="exact-id"),
        caller_origin=ORIGIN,
        config={"allowed_origins": [ORIGIN]},
        ensure=lambda _: (_ for _ in ()).throw(RuntimeError("token=secret C:\\private\\db")),
    )
    assert response == {
        "v": 1,
        "id": "exact-id",
        "ok": False,
        "error": {
            "code": "internal-error",
            "message": "本机桥接服务发生内部错误，请重试。",
            "retriable": True,
        },
    }
    assert "secret" not in json.dumps(response, ensure_ascii=False)
    assert "private" not in json.dumps(response, ensure_ascii=False)


def test_unexpected_failure_logs_only_safe_type(
    tmp_path, monkeypatch, caplog, capsys
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    logger = logging.getLogger("sitefilter-native-host")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    host.configure_logging()
    caplog.set_level(logging.ERROR, logger="sitefilter-native-host")

    host.process_request(
        base_request("ping"),
        caller_origin=ORIGIN,
        config={"allowed_origins": [ORIGIN]},
        ensure=lambda _: (_ for _ in ()).throw(
            RuntimeError("token=secret C:\\private\\collector")
        ),
    )
    for handler in logger.handlers:
        handler.flush()
    log_file = tmp_path / "SiteFilter" / "NativeHost" / "native-host.log"
    stderr = capsys.readouterr().err
    logged = caplog.text + stderr + log_file.read_text(encoding="utf-8")
    assert "RuntimeError" in logged
    assert "secret" not in logged
    assert "private" not in logged
    assert "collector" not in logged.lower()


def test_ensure_collector_uses_config_owned_wsl_arguments_and_descriptor(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    runtime = tmp_path / "runtime.json"
    config = {
        "distro": "Ubuntu",
        "project_path": "/mnt/c/collector",
        "runtime_file": str(runtime),
        "startup_timeout_seconds": 1,
    }
    commands = []
    probes = []

    def popen(args, **kwargs):
        commands.append((args, kwargs))
        runtime.write_text(
            json.dumps({"protocol_version": 1, "port": 8123, "owned": True, "ready": True}),
            encoding="utf-8",
        )
        return type("Process", (), {"poll": lambda self: None})()

    def healthy(port):
        probes.append(port)
        return port == 8123

    assert host.ensure_collector(config, health_probe=healthy, popen=popen, sleeper=lambda _: None) == 8123
    assert commands[0][0] == [
        "wsl.exe", "-d", "Ubuntu", "--cd", "/mnt/c/collector", "--", "make", "start-native",
    ]
    assert commands[0][1]["shell"] is False
    assert probes[-1] == 8123


def test_ensure_collector_fails_early_when_launcher_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    config = {
        "distro": "Ubuntu",
        "project_path": "/mnt/c/collector",
        "runtime_file": str(tmp_path / "runtime.json"),
        "startup_timeout_seconds": 60,
    }
    sleeps = []
    process = type("Process", (), {"poll": lambda self: 7})()

    with pytest.raises(host.ProtocolError) as exc:
        host.ensure_collector(
            config,
            health_probe=lambda port: False,
            popen=lambda *args, **kwargs: process,
            sleeper=sleeps.append,
        )
    assert exc.value.code == "collector-startup-failed"
    assert sleeps == []


@pytest.mark.parametrize(
    "descriptor_bytes",
    [
        b"{" + b" " * (host.MAX_MESSAGE_BYTES + 1) + b"}",
        b'{"protocol_version":1,"port":NaN,"owned":false,"ready":true}',
        (b"[" * 5000) + b"0" + (b"]" * 5000),
        b'{"protocol_version":1.0,"port":8123,"owned":false,"ready":true}',
        b'{"protocol_version":1,"port":8123,"owned":"false","ready":true}',
        b'{"protocol_version":1,"port":8123,"owned":false,"ready":1}',
    ],
    ids=["oversized", "nan", "deep", "float-version", "bad-owned", "bad-ready"],
)
def test_runtime_descriptor_rejects_oversized_nonstandard_deep_and_bad_types(
    tmp_path, descriptor_bytes
):
    runtime = tmp_path / "runtime.json"
    runtime.write_bytes(descriptor_bytes)
    assert host._runtime_port(runtime, lambda port: True) is None


def test_runtime_descriptor_accepts_documented_owned_pid_shape(tmp_path):
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "port": 8123,
                "owned": True,
                "ready": True,
                "pid": 456,
            }
        ),
        encoding="utf-8",
    )
    assert host._runtime_port(runtime, lambda port: port == 8123) == 8123


def test_load_config_rejects_extra_relative_and_unsafe_values(tmp_path):
    valid = {
        "protocol_version": 1,
        "allowed_origins": [ORIGIN],
        "distro": "Ubuntu-24.04",
        "project_path": "/mnt/c/collector",
        "runtime_file": str((tmp_path / "runtime.json").resolve()),
        "startup_timeout_seconds": 60,
    }
    invalid = [
        {**valid, "endpoint": "/healthz"},
        {**valid, "runtime_file": "relative.json"},
        {**valid, "allowed_origins": ["chrome-extension://abc/"]},
        {**valid, "distro": "Ubuntu\n--exec"},
        {**valid, "project_path": "/mnt/c/project\x00evil"},
        {**valid, "startup_timeout_seconds": 0},
        {**valid, "startup_timeout_seconds": True},
    ]
    for index, value in enumerate(invalid):
        path = tmp_path / f"invalid-{index}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(host.ProtocolError) as exc:
            host.load_config(path)
        assert exc.value.code == "installation-invalid"


def test_load_config_rejects_deep_json_as_installation_invalid(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes((b"[" * 5000) + b"0" + (b"]" * 5000))
    with pytest.raises(host.ProtocolError) as exc:
        host.load_config(path)
    assert exc.value.code == "installation-invalid"


def test_main_subprocess_ping_writes_exactly_one_frame(tmp_path):
    class CollectorHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            bodies = {
                "/healthz": {"status": "ok"},
                "/config": {
                    "resource_types": ["image", "video"],
                    "collectors": ["xchina_gallery", "xchina_video"],
                    "auto_collector": "auto",
                },
            }
            body = bodies.get(self.path)
            if body is None:
                self.send_error(404)
                return
            raw = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CollectorHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    local_app_data = tmp_path / "local"
    config_dir = local_app_data / "SiteFilter" / "NativeHost"
    config_dir.mkdir(parents=True)
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps({"protocol_version": 1, "port": server.server_port, "owned": False, "ready": True}),
        encoding="utf-8",
    )
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "allowed_origins": [ORIGIN],
                "distro": "Ubuntu",
                "project_path": "/mnt/c/collector",
                "runtime_file": str(runtime),
            }
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "LOCALAPPDATA": str(local_app_data)}
    try:
        proc = subprocess.run(
            [sys.executable, str(HOST_PATH), ORIGIN],
            input=framed(base_request("ping", request_id="subprocess-id")),
            capture_output=True,
            env=env,
            timeout=10,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    stream = BytesIO(proc.stdout)
    response = host.read_message(stream)
    assert response["id"] == "subprocess-id"
    assert response == {
        "v": 1,
        "id": "subprocess-id",
        "ok": True,
        "result": {
            "protocol_version": 1,
            "port": server.server_port,
            "collector": {"status": "ok"},
        },
    }
    assert stream.read() == b""


@pytest.mark.parametrize("request_id", ["\\ud800", "\\udc00"])
def test_main_subprocess_rejects_surrogate_id_without_crashing(tmp_path, request_id):
    local_app_data = tmp_path / "local"
    config_dir = local_app_data / "SiteFilter" / "NativeHost"
    config_dir.mkdir(parents=True)
    runtime = tmp_path / "runtime.json"
    config = {
        "protocol_version": 1,
        "allowed_origins": [ORIGIN],
        "distro": "Ubuntu",
        "project_path": "/mnt/c/collector",
        "runtime_file": str(runtime.resolve()),
    }
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    raw = (
        b'{"v":1,"id":"'
        + request_id.encode("ascii")
        + b'","action":"ping","payload":{}}'
    )
    proc = subprocess.run(
        [sys.executable, str(HOST_PATH), ORIGIN],
        input=struct.pack("<I", len(raw)) + raw,
        capture_output=True,
        env={**os.environ, "LOCALAPPDATA": str(local_app_data)},
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    stream = BytesIO(proc.stdout)
    response = host.read_message(stream)
    assert response["id"] == ""
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid-request-id"
    assert stream.read() == b""


def test_main_subprocess_rejects_deep_json_without_traceback(tmp_path):
    local_app_data = tmp_path / "local"
    config_dir = local_app_data / "SiteFilter" / "NativeHost"
    config_dir.mkdir(parents=True)
    config = {
        "protocol_version": 1,
        "allowed_origins": [ORIGIN],
        "distro": "Ubuntu",
        "project_path": "/mnt/c/collector",
        "runtime_file": str((tmp_path / "runtime.json").resolve()),
    }
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    raw = (b"[" * 5000) + b"0" + (b"]" * 5000)
    proc = subprocess.run(
        [sys.executable, str(HOST_PATH), ORIGIN],
        input=struct.pack("<I", len(raw)) + raw,
        capture_output=True,
        env={**os.environ, "LOCALAPPDATA": str(local_app_data)},
        timeout=10,
    )
    assert proc.returncode == 1
    stream = BytesIO(proc.stdout)
    response = host.read_message(stream)
    assert response["id"] == ""
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid-json"
    assert stream.read() == b""
    assert b"Traceback" not in proc.stderr
