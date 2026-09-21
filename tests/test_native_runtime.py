import json
import os
import signal
import shutil
import subprocess
import sys
import textwrap
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from types import SimpleNamespace

import pytest

from scripts import start

POSIX_TERMINATION_SIGNALS = [signal.SIGTERM]
if hasattr(signal, "SIGHUP"):
    POSIX_TERMINATION_SIGNALS.append(signal.SIGHUP)


def _fake_uv(path, label):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"{label}:$*\" > \"$UWC_UV_CAPTURE\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _fake_runtime_uv(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$0:$*\" >> \"$UWC_UV_CAPTURE\"\n"
        "if [ \"$1\" = run ] && [ \"$2\" = python ]; then\n"
        "  shift 2\n"
        "  exec \"$UWC_TEST_PYTHON\" \"$@\"\n"
        "fi\n"
        "if [ \"$1\" = sync ]; then exit 17; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_native_make(tmp_path, *, path_uv=False, home_uv=False, unset_home=False):
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is unavailable")
    home = tmp_path / "home"
    path_dir = tmp_path / "path"
    capture = tmp_path / "uv-call.txt"
    path_dir.mkdir()
    if path_uv:
        _fake_uv(path_dir / "uv", "path")
    if home_uv:
        _fake_uv(home / ".local" / "bin" / "uv", "home")
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(path_dir),
        "UWC_UV_CAPTURE": str(capture),
    }
    if unset_home:
        env.pop("HOME", None)
    result = subprocess.run(
        [make, "start-native"],
        cwd=start.ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    call = capture.read_text(encoding="utf-8").strip() if capture.exists() else None
    return result, call


@pytest.mark.skipif(os.name == "nt", reason="POSIX Makefile recipe")
def test_start_native_falls_back_to_user_uv(tmp_path):
    result, call = _run_native_make(tmp_path, home_uv=True)

    assert result.returncode == 0, result.stderr
    assert call == "home:run python scripts/start.py --native --no-open --idle-minutes 30"


@pytest.mark.skipif(os.name == "nt", reason="POSIX Makefile recipe")
def test_start_native_prefers_path_uv(tmp_path):
    result, call = _run_native_make(tmp_path, path_uv=True, home_uv=True)

    assert result.returncode == 0, result.stderr
    assert call == "path:run python scripts/start.py --native --no-open --idle-minutes 30"


@pytest.mark.skipif(os.name == "nt", reason="POSIX Makefile recipe")
def test_start_native_reports_missing_uv(tmp_path):
    result, call = _run_native_make(tmp_path)

    assert result.returncode != 0
    assert call is None
    assert "$HOME/.local/bin/uv" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX Makefile recipe")
def test_start_native_reports_missing_uv_with_unset_home(tmp_path):
    result, call = _run_native_make(tmp_path, unset_home=True)

    assert result.returncode != 0
    assert call is None
    assert "start-native: uv not found" in result.stderr
    assert "parameter not set" not in result.stderr.lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX Makefile recipe")
def test_native_fallback_passes_uv_to_internal_sync(tmp_path):
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is unavailable")
    home = tmp_path / "home"
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    uv = home / ".local" / "bin" / "uv"
    capture = tmp_path / "uv-calls.txt"
    _fake_runtime_uv(uv)

    result = subprocess.run(
        [make, "start-native"],
        cwd=start.ROOT,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": str(path_dir),
            "UWC_RUNTIME_FILE": str(tmp_path / "runtime.json"),
            "UWC_SKIP_BROWSER_CHECK": "1",
            "UWC_TEST_PYTHON": sys.executable,
            "UWC_UV_CAPTURE": str(capture),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    calls = capture.read_text(encoding="utf-8").splitlines()
    assert result.returncode != 0
    assert calls == [
        f"{uv}:run python scripts/start.py --native --no-open --idle-minutes 30",
        f"{uv}:sync",
    ]


@pytest.mark.parametrize("invalid", ["relative/uv", "/missing/uv"])
def test_invalid_internal_uv_falls_back_to_path(tmp_path, monkeypatch, invalid):
    path_uv = tmp_path / "path-uv"
    _fake_uv(path_uv, "path")
    monkeypatch.setenv("UWC_START_UV", invalid)
    monkeypatch.setattr(start.shutil, "which", lambda name: str(path_uv))

    assert start.resolve_uv_command() == str(path_uv)


def test_nonexecutable_internal_uv_falls_back_to_path(tmp_path, monkeypatch):
    internal_uv = tmp_path / "internal-uv"
    internal_uv.write_text("not executable", encoding="utf-8")
    path_uv = tmp_path / "path-uv"
    _fake_uv(path_uv, "path")
    monkeypatch.setenv("UWC_START_UV", str(internal_uv.resolve()))
    monkeypatch.setattr(start.shutil, "which", lambda name: str(path_uv))

    assert start.resolve_uv_command() == str(path_uv)


def test_path_uv_is_used_by_check_env(monkeypatch):
    calls = []
    monkeypatch.delenv("UWC_START_UV", raising=False)
    monkeypatch.setattr(start.shutil, "which", lambda name: "/opt/bin/uv")
    monkeypatch.setattr(
        start,
        "run",
        lambda cmd, **kwargs: (
            calls.append(cmd) or SimpleNamespace(returncode=1)
        ),
    )

    with pytest.raises(SystemExit):
        start.check_env(dev=False, force_build=False)

    assert calls == [["/opt/bin/uv", "sync"]]


def test_parse_native_runtime_arguments(tmp_path):
    runtime_file = tmp_path / "collector-runtime.json"

    args = start.parse_args(
        [
            "--native",
            "--runtime-file",
            str(runtime_file),
            "--idle-minutes",
            "30",
        ]
    )

    assert args.native is True
    assert args.no_open is True
    assert args.runtime_file == runtime_file
    assert args.idle_minutes == 30

    default_args = start.parse_args([])
    assert default_args.native is False
    assert default_args.no_open is False


def test_runtime_descriptor_is_atomic_machine_readable_json(tmp_path):
    runtime_file = tmp_path / "collector-runtime.json"
    descriptor = start.build_runtime_descriptor(
        port=8123, owned=True, ready=True, pid=456
    )

    start.write_runtime_descriptor(runtime_file, descriptor)

    assert json.loads(runtime_file.read_text(encoding="utf-8")) == {
        "protocol_version": 1,
        "port": 8123,
        "owned": True,
        "ready": True,
        "pid": 456,
    }
    assert not runtime_file.with_suffix(runtime_file.suffix + ".tmp").exists()


def test_activity_resets_idle_countdown():
    for statuses, watches in [
        ([status], [])
        for status in ("pending", "running", "extracting", "downloading", "paused")
    ] + [
        ([], [True]),
    ]:
        decision = start.idle_decision(
            now=2000,
            idle_since=100,
            idle_minutes=30,
            task_statuses=statuses,
            watch_enabled_flags=watches,
        )

        assert decision.shutdown is False
        assert decision.idle_since == 2000


def test_thirty_continuous_idle_minutes_requests_shutdown():
    decision = start.idle_decision(
        now=1900,
        idle_since=100,
        idle_minutes=30,
        task_statuses=["success", "failed"],
        watch_enabled_flags=[False],
    )

    assert decision.shutdown is True
    assert decision.idle_since == 100


def test_discovered_manual_instance_is_not_owned():
    descriptor = start.discover_existing_runtime(
        port=8000, health_probe=lambda port: port == 8000
    )

    assert descriptor["owned"] is False
    assert descriptor["ready"] is True
    assert "pid" not in descriptor


def test_collector_discovery_requires_collector_specific_config():
    def opener(url, timeout):
        if url.endswith("/healthz"):
            return BytesIO(b'{"status":"ok"}')
        raise OSError("not a Collector")

    assert start.collector_healthy(8000, opener=opener) is False

    def collector_opener(url, timeout):
        if url.endswith("/healthz"):
            return BytesIO(b'{"status":"ok"}')
        if url.endswith("/config"):
            return BytesIO(
                b'{"resource_types":[],"collectors":[],"auto_collector":"auto"}'
            )
        raise AssertionError(url)

    assert start.collector_healthy(8000, opener=collector_opener) is True


@pytest.mark.parametrize("owned", [True, False])
def test_stale_ready_descriptor_is_replaced_before_startup(tmp_path, owned):
    runtime_file = tmp_path / "runtime.json"
    start.write_runtime_descriptor(
        runtime_file,
        start.build_runtime_descriptor(8999, owned=owned, ready=True, pid=1234),
    )

    start.prepare_startup_descriptor(runtime_file, port=8000)

    assert json.loads(runtime_file.read_text(encoding="utf-8")) == {
        "protocol_version": 1,
        "port": 8000,
        "owned": True,
        "ready": False,
    }


def test_pre_child_failure_removes_startup_descriptor_and_releases_lock(
    tmp_path, monkeypatch
):
    runtime_file = tmp_path / "runtime.json"
    args = SimpleNamespace(
        runtime_file=runtime_file,
        port=8123,
        build=False,
        idle_minutes=30,
    )
    monkeypatch.setattr(start, "discover_existing_runtime", lambda port: None)
    monkeypatch.setattr(
        start,
        "check_env",
        lambda dev, build: (_ for _ in ()).throw(RuntimeError("preflight failed")),
    )

    with pytest.raises(RuntimeError, match="preflight failed"):
        start.run_native(args)

    assert not runtime_file.exists()
    replacement = start.SingleInstanceLock(runtime_file.with_suffix(".json.lock"))
    assert replacement.acquire() is True
    replacement.release()


def test_startup_cleanup_does_not_remove_replaced_descriptor(tmp_path):
    runtime_file = tmp_path / "runtime.json"
    start.prepare_startup_descriptor(runtime_file, port=8000)
    replacement = start.build_runtime_descriptor(8001, owned=False, ready=True)
    start.write_runtime_descriptor(runtime_file, replacement)

    start.remove_startup_descriptor(runtime_file, port=8000)

    assert json.loads(runtime_file.read_text(encoding="utf-8")) == replacement


def test_activity_read_error_restarts_full_idle_window():
    messages = []
    reporter = start.ActivityFailureReporter(messages.append, log_interval=60)

    first = start.supervised_idle_decision(
        now=1900,
        idle_since=100,
        idle_minutes=30,
        activity_reader=lambda: (_ for _ in ()).throw(OSError("database busy")),
        failure_reporter=reporter,
    )
    repeated_error = start.supervised_idle_decision(
        now=1910,
        idle_since=first.idle_since,
        idle_minutes=30,
        activity_reader=lambda: (_ for _ in ()).throw(OSError("still busy")),
        failure_reporter=reporter,
    )
    second = start.supervised_idle_decision(
        now=3709,
        idle_since=repeated_error.idle_since,
        idle_minutes=30,
        activity_reader=lambda: ([], []),
        failure_reporter=reporter,
    )
    third = start.supervised_idle_decision(
        now=3710,
        idle_since=second.idle_since,
        idle_minutes=30,
        activity_reader=lambda: ([], []),
        failure_reporter=reporter,
    )

    assert first.shutdown is False and first.idle_since == 1900
    assert repeated_error.shutdown is False and repeated_error.idle_since == 1910
    assert second.shutdown is False
    assert third.shutdown is True
    assert len(messages) == 1
    assert "database busy" in messages[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal semantics")
@pytest.mark.parametrize("signum", POSIX_TERMINATION_SIGNALS)
def test_posix_signal_cleans_owned_child_descriptor_and_lock(tmp_path, signum):
    runtime_file = tmp_path / "runtime.json"
    lock_file = tmp_path / "runtime.json.lock"
    code = textwrap.dedent(
        """
        import subprocess, sys
        from pathlib import Path
        from scripts import start

        runtime = Path(sys.argv[1])
        lock = start.SingleInstanceLock(sys.argv[2])
        assert lock.acquire()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        start.write_runtime_descriptor(
            runtime,
            start.build_runtime_descriptor(8123, owned=True, ready=True, pid=child.pid),
        )
        try:
            start.supervise_owned_backend(
                child,
                runtime,
                idle_minutes=30,
                activity_reader=lambda: (["running"], []),
                poll_seconds=0.05,
                ready_callback=lambda: print(f"ready {child.pid}", flush=True),
            )
        finally:
            start.kill_tree(child)
            start.remove_owned_runtime_descriptor(runtime, child.pid)
            lock.release()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(runtime_file), str(lock_file)],
        cwd=start.ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    marker, child_pid_text = proc.stdout.readline().strip().split()
    assert marker == "ready"
    child_pid = int(child_pid_text)

    proc.send_signal(signum)
    stdout, stderr = proc.communicate(timeout=10)

    assert proc.returncode == 0, (stdout, stderr)
    assert not runtime_file.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    replacement = start.SingleInstanceLock(lock_file)
    assert replacement.acquire() is True
    replacement.release()


@pytest.mark.skipif(os.name != "nt", reason="Windows byte-range lock regression")
def test_second_windows_launcher_waits_for_locked_runtime(tmp_path):
    class CollectorHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            bodies = {
                "/healthz": b'{"status":"ok"}',
                "/config": (
                    b'{"resource_types":[],"collectors":[],'
                    b'"auto_collector":"auto"}'
                ),
            }
            body = bodies.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CollectorHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    runtime_file = tmp_path / "runtime.json"
    lock_file = runtime_file.with_suffix(".json.lock")
    holder_code = textwrap.dedent(
        """
        import sys, time
        from scripts import start
        lock = start.SingleInstanceLock(sys.argv[1])
        assert lock.acquire()
        print("locked", flush=True)
        try:
            time.sleep(30)
        finally:
            lock.release()
        """
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(lock_file)],
        cwd=start.ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        start.write_runtime_descriptor(
            runtime_file,
            start.build_runtime_descriptor(
                server.server_port, owned=True, ready=True, pid=holder.pid
            ),
        )
        second = subprocess.run(
            [
                sys.executable,
                "scripts/start.py",
                "--native",
                "--runtime-file",
                str(runtime_file),
                "--port",
                str(server.server_port),
            ],
            cwd=start.ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert second.returncode == 0, (second.stdout, second.stderr)
    finally:
        holder.terminate()
        holder.communicate(timeout=10)
        server.shutdown()
        server.server_close()
