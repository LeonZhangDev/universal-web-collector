"""一键启动: 环境自检(缺失自动安装) → 端口占用检测与自动对齐 → 启动前后端 → 打开浏览器。

用法:
  uv run python scripts/start.py            生产模式: 后端托管 frontend/dist, 单进程
  uv run python scripts/start.py --dev      开发模式: 后端 + vite dev server 双进程
  uv run python scripts/start.py --build    强制重新构建前端
  uv run python scripts/start.py --port 9000 --no-open

端口对齐: 后端端口(默认8000)被占用时自动顺延, 通过 UWC_PORT 传给后端;
dev 模式下 vite 端口(默认5173)同样顺延, 代理目标自动指向后端实际端口。
"""

import argparse
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"
DEFAULT_PORT = 8000
DEFAULT_FRONT_PORT = 5173
DEFAULT_RUNTIME_FILE = ROOT / "data" / "native-runtime.json"
NATIVE_PROTOCOL_VERSION = 1
START_UV_ENV = "UWC_START_UV"
NATIVE_BUSY_TASK_STATES = frozenset(
    {"pending", "running", "extracting", "downloading", "paused"}
)

sys.stdout.reconfigure(line_buffering=True)  # 重定向到文件时也逐行输出


def ok(msg):
    print(f"  [ok] {msg}")


def warn(msg):
    print(f"  [!]  {msg}")


def fail(msg):
    print(f"  [x]  {msg}")
    sys.exit(1)


def run(cmd, **kw):
    print(f"  $ {' '.join(map(str, cmd))}")
    return subprocess.run(cmd, **kw)


def which(name):
    p = shutil.which(name)
    if p is None:
        fail(
            f"未找到 {name}。安装方式:\n"
            f"      node/npm: https://nodejs.org  或  winget install OpenJS.NodeJS.LTS"
        )
    return p


def resolve_uv_command():
    """Resolve uv without treating an untrusted env value as a command."""
    internal = os.environ.get(START_UV_ENV)
    if internal:
        candidate = Path(internal)
        if (
            candidate.is_absolute()
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ):
            return str(candidate)
    path_uv = shutil.which("uv")
    if path_uv:
        return path_uv
    fail(
        "未找到 uv。请将 uv 加入 PATH，或通过 start-native 使用"
        " $HOME/.local/bin/uv"
    )


# ---- 环境自检 ----

def chromium_installed():
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
    ]
    return any(d.is_dir() and any(d.glob("chromium-*")) for d in candidates)


def vite_cmd(*args):
    """直接调 vite.js 入口, 绕开 npm wrapper 的 PATH 注入问题。"""
    vite_js = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite_js.is_file():
        fail("vite 未安装, 先运行 npm install (frontend/)")
    return [which("node"), str(vite_js), *args]


def check_env(dev, force_build):
    dist_ready = (DIST / "index.html").is_file()
    need_node = dev or force_build or not dist_ready
    uv = resolve_uv_command()

    print("[1/5] 后端依赖 (uv sync)")
    if run([uv, "sync"], cwd=ROOT).returncode != 0:
        fail("uv sync 失败, 检查网络或 pyproject.toml")
    ok("后端依赖就绪")

    print("[2/5] Playwright Chromium")
    if os.environ.get("UWC_SKIP_BROWSER_CHECK") == "1":
        warn("已跳过浏览器检查 (UWC_SKIP_BROWSER_CHECK=1)")
    elif chromium_installed():
        ok("Chromium 已安装")
    else:
        warn("Chromium 未安装, 开始安装(首次需数分钟)...")
        if run([sys.executable, "-m", "playwright", "install", "chromium"], cwd=ROOT).returncode != 0:
            fail("Chromium 安装失败")
        ok("Chromium 安装完成")

    print("[3/5] 前端工具链 (node/npm)")
    if not need_node:
        ok("无需构建(frontend/dist 已就绪, --build 可强制重建)")
    else:
        node = which("node")
        ver = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
        ok(f"node {ver}")

    print("[4/5] 前端产物")
    if need_node:
        npm = which("npm")
        if not (FRONTEND / "node_modules").is_dir():
            warn("npm install...")
            if run([npm, "install"], cwd=FRONTEND).returncode != 0:
                fail("npm install 失败")
        if dev:
            ok("开发模式, 无需构建")
        else:
            warn("构建前端 (vite build)...")
            if run(vite_cmd("build"), cwd=FRONTEND).returncode != 0:
                fail("前端构建失败")
            ok("构建完成")
    else:
        ok("frontend/dist 已就绪")

    print("[5/5] 视频引擎 (ffmpeg)")
    check_ffmpeg()


def check_ffmpeg():
    """ffmpeg 是可选的, 但它决定视频产物是 .mp4 还是 .ts, 所以显式报下状态。

    这里用的是 core.ffmpeg.find_ffmpeg() 而**不是** shutil.which():
    PATH 是进程启动时的快照, 服务运行期间新装的 ffmpeg 用 which 读不到。
    """
    backend_dir = str(ROOT / "backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    try:
        from core.config import settings
        from core.ffmpeg import find_ffmpeg
    except Exception as e:
        warn(f"跳过视频引擎检查: {e}")
        return

    engine = (settings.video_engine or "auto").lower()
    ff = find_ffmpeg()

    if engine == "builtin":
        ok("视频引擎: builtin (强制内置分片器, 不使用 ffmpeg 拉流)")
        return

    if ff:
        ok(f"ffmpeg 已找到: {ff}")
        note = ("优先用 ffmpeg 拉流, 失败降级内置分片器" if engine == "auto"
                else "强制使用, 失败直接报错不降级")
        ok(f"视频引擎: {engine} ({note})")
        return

    if engine == "ffmpeg":
        warn("video_engine=ffmpeg 但未找到 ffmpeg —— 视频下载会直接报错")
    else:
        warn("未找到 ffmpeg: m3u8 走内置分片器, 产物为 .ts "
             "(VLC/PotPlayer 可播, 浏览器不可播)")
    warn("装好 ffmpeg 后产物即为 .mp4; 合并只是 remux, 不重编码")
    warn("  winget install Gyan.FFmpeg  或  在 config.yaml 里写 ffmpeg_path")


# ---- 端口检测与对齐 ----

def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_free_port(start, label):
    p = start
    for _ in range(20):
        if not port_in_use(p):
            if p != start:
                warn(f"{label} 端口 {start} 已被占用, 自动切换到 {p}")
            return p
        p += 1
    fail(f"{label} 端口 {start}~{p - 1} 全部被占用")


def wait_http(url, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def collector_healthy(port, opener=urllib.request.urlopen):
    """Require both generic health and Collector's stable config schema."""
    try:
        base = f"http://127.0.0.1:{port}"
        with opener(f"{base}/healthz", timeout=1) as response:
            if json.load(response) != {"status": "ok"}:
                return False
        with opener(f"{base}/config", timeout=1) as response:
            config = json.load(response)
        return (
            isinstance(config, dict)
            and isinstance(config.get("resource_types"), list)
            and isinstance(config.get("collectors"), list)
            and isinstance(config.get("auto_collector"), str)
        )
    except Exception:
        return False


# ---- native runtime contract ----

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="一键启动前后端")
    ap.add_argument("--dev", action="store_true", help="开发模式(vite dev + 后端)")
    ap.add_argument("--build", action="store_true", help="强制重新构建前端")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="后端起始端口(默认8000)")
    ap.add_argument("--front-port", type=int, default=DEFAULT_FRONT_PORT,
                    help="dev 模式前端起始端口(默认5173)")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--native", action="store_true", help="Native Host 启动模式")
    ap.add_argument(
        "--runtime-file",
        type=Path,
        default=Path(os.environ.get("UWC_RUNTIME_FILE", DEFAULT_RUNTIME_FILE)),
        help="Native Host 读取的运行时描述文件",
    )
    ap.add_argument(
        "--idle-minutes", type=float, default=30,
        help="Native 模式连续空闲退出分钟数(默认30)",
    )
    args = ap.parse_args(argv)
    if args.native:
        args.no_open = True
        if args.dev:
            ap.error("--native 不支持 --dev")
        if args.idle_minutes <= 0:
            ap.error("--idle-minutes 必须大于 0")
    return args


def build_runtime_descriptor(port, *, owned, ready, pid=None):
    descriptor = {
        "protocol_version": NATIVE_PROTOCOL_VERSION,
        "port": int(port),
        "owned": bool(owned),
        "ready": bool(ready),
    }
    if owned and pid is not None:
        descriptor["pid"] = int(pid)
    return descriptor


def discover_existing_runtime(port, health_probe=collector_healthy):
    if not health_probe(port):
        return None
    return build_runtime_descriptor(port, owned=False, ready=True)


def write_runtime_descriptor(path, descriptor):
    """Atomically publish a descriptor so readers never observe partial JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(descriptor, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def prepare_startup_descriptor(path, port):
    """Replace any stale ready descriptor before startup work can block."""
    write_runtime_descriptor(
        path, build_runtime_descriptor(port, owned=True, ready=False)
    )


def remove_startup_descriptor(path, port):
    """Remove only our exact pre-child marker, never a replaced descriptor."""
    path = Path(path)
    expected = build_runtime_descriptor(port, owned=True, ready=False)
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        if descriptor == expected:
            path.unlink()
    except (FileNotFoundError, OSError, ValueError):
        pass


def remove_owned_runtime_descriptor(path, pid):
    """Remove only the descriptor that names our owned child."""
    path = Path(path)
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        if descriptor.get("owned") is True and descriptor.get("pid") == pid:
            path.unlink()
    except (FileNotFoundError, OSError, ValueError):
        pass


class IdleDecision:
    def __init__(self, shutdown, idle_since):
        self.shutdown = shutdown
        self.idle_since = idle_since


def idle_decision(
    *, now, idle_since, idle_minutes, task_statuses, watch_enabled_flags
):
    busy = any(status in NATIVE_BUSY_TASK_STATES for status in task_statuses)
    busy = busy or any(watch_enabled_flags)
    if busy:
        return IdleDecision(False, now)
    if idle_since is None:
        return IdleDecision(False, now)
    return IdleDecision(now - idle_since >= idle_minutes * 60, idle_since)


class ActivityFailureReporter:
    def __init__(self, sink=warn, log_interval=60):
        self.sink = sink
        self.log_interval = log_interval
        self.last_logged = None

    def report(self, exc, now):
        if self.last_logged is not None and now - self.last_logged < self.log_interval:
            return
        message = f"活动状态读取失败, 按忙碌处理并重试: {type(exc).__name__}: {exc}"
        self.sink(message[:300])
        self.last_logged = now


def supervised_idle_decision(
    *, now, idle_since, idle_minutes, activity_reader,
    failure_reporter
):
    try:
        task_statuses, enabled_watches = activity_reader()
        return idle_decision(
            now=now,
            idle_since=idle_since,
            idle_minutes=idle_minutes,
            task_statuses=task_statuses,
            watch_enabled_flags=enabled_watches,
        )
    except Exception as exc:
        failure_reporter.report(exc, now)
        return IdleDecision(False, now)


def native_activity():
    backend_dir = str(ROOT / "backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from core import database as db

    statuses = db.count_tasks_by_status()
    watches = db.list_watches()
    return tuple(statuses), tuple(bool(w["enabled"]) for w in watches)


class SingleInstanceLock:
    """Small cross-platform advisory lock held for the supervisor lifetime."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file.seek(0)
            if self.file.read(1) == b"":
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            return True
        except (OSError, BlockingIOError):
            self.file.close()
            self.file = None
            return False

    def release(self):
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


def wait_for_existing_runtime(path, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            descriptor = json.loads(Path(path).read_text(encoding="utf-8"))
            if descriptor.get("ready") and collector_healthy(descriptor["port"]):
                return True
        except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.25)
    return False


class NativeShutdown(Exception):
    pass


def _request_native_shutdown(signum, frame):
    raise NativeShutdown()


def install_native_signal_handlers():
    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, _request_native_shutdown)
    return previous


def restore_signal_handlers(previous):
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def supervise_owned_backend(
    backend,
    runtime_file,
    *,
    idle_minutes,
    activity_reader=native_activity,
    poll_seconds=1,
    clock=time.monotonic,
    sleeper=time.sleep,
    manage_signals=True,
    ready_callback=None,
):
    previous_handlers = install_native_signal_handlers() if manage_signals else {}
    reporter = ActivityFailureReporter()
    try:
        if ready_callback is not None:
            ready_callback()
        idle_since = clock()
        while backend.poll() is None:
            decision = supervised_idle_decision(
                now=clock(),
                idle_since=idle_since,
                idle_minutes=idle_minutes,
                activity_reader=activity_reader,
                failure_reporter=reporter,
            )
            idle_since = decision.idle_since
            if decision.shutdown:
                break
            sleeper(poll_seconds)
    except (KeyboardInterrupt, NativeShutdown):
        pass
    finally:
        kill_tree(backend)
        remove_owned_runtime_descriptor(runtime_file, backend.pid)
        restore_signal_handlers(previous_handlers)


# ---- 进程管理 ----

def kill_tree(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def banner(lines):
    print("\n" + "=" * 52)
    for l in lines:
        print(f"  {l}")
    print("=" * 52 + "\n")


def run_native(args):
    runtime_file = args.runtime_file
    lock = SingleInstanceLock(runtime_file.with_suffix(runtime_file.suffix + ".lock"))
    if not lock.acquire():
        if wait_for_existing_runtime(runtime_file):
            return
        fail("已有 Native 启动正在进行, 但 30s 内未发现就绪实例")

    backend = None
    startup_port = args.port
    previous_handlers = install_native_signal_handlers()
    try:
        discovered = discover_existing_runtime(args.port)
        if discovered is not None:
            write_runtime_descriptor(runtime_file, discovered)
            return

        prepare_startup_descriptor(runtime_file, args.port)
        print("\n=== 环境自检 ===")
        check_env(False, args.build)

        print("\n=== 端口检测 ===")
        port = find_free_port(args.port, "后端")
        startup_port = port
        prepare_startup_descriptor(runtime_file, port)
        env = {**os.environ, "UWC_PORT": str(port)}

        print("\n=== 启动 ===")
        backend = subprocess.Popen(
            [sys.executable, "backend/main.py"], cwd=ROOT, env=env
        )
        write_runtime_descriptor(
            runtime_file,
            build_runtime_descriptor(port, owned=True, ready=False, pid=backend.pid),
        )
        url = f"http://127.0.0.1:{port}"
        if not wait_http(f"{url}/healthz", 30):
            fail("后端 30s 内未就绪, 查看上方日志")
        write_runtime_descriptor(
            runtime_file,
            build_runtime_descriptor(port, owned=True, ready=True, pid=backend.pid),
        )
        banner([
            f"应用:     {url}",
            "模式:     Native Host",
            f"空闲退出: {args.idle_minutes:g} 分钟",
        ])

        supervise_owned_backend(
            backend,
            runtime_file,
            idle_minutes=args.idle_minutes,
            manage_signals=False,
        )
        backend = None
    except (KeyboardInterrupt, NativeShutdown):
        pass
    finally:
        if backend is not None:
            kill_tree(backend)
            remove_owned_runtime_descriptor(runtime_file, backend.pid)
        remove_startup_descriptor(runtime_file, startup_port)
        restore_signal_handlers(previous_handlers)
        lock.release()


def main():
    args = parse_args()

    if args.native:
        run_native(args)
        return

    print("\n=== 环境自检 ===")
    check_env(args.dev, args.build)

    print("\n=== 端口检测 ===")
    port = find_free_port(args.port, "后端")
    env = {**os.environ, "UWC_PORT": str(port)}

    print("\n=== 启动 ===")
    if not args.dev:
        backend = subprocess.Popen(
            [sys.executable, "backend/main.py"], cwd=ROOT, env=env
        )
        url = f"http://127.0.0.1:{port}"
        if not wait_http(f"{url}/healthz", 30):
            kill_tree(backend)
            fail("后端 30s 内未就绪, 查看上方日志")
        banner([f"应用:     {url}", "模式:     生产(后端托管前端)", "停止:     Ctrl+C"])
        if not args.no_open:
            webbrowser.open(url)
        try:
            backend.wait()
        except KeyboardInterrupt:
            pass
        finally:
            kill_tree(backend)
        return

    # dev 模式
    fport = find_free_port(args.front_port, "前端")
    backend = subprocess.Popen(
        [sys.executable, "backend/main.py"], cwd=ROOT, env=env
    )
    front = subprocess.Popen(
        # --host: vite 默认仅绑 IPv6 localhost, 加上后 127.0.0.1 可达
        vite_cmd("--host", "--port", str(fport), "--strictPort"),
        cwd=FRONTEND,
        env={**env, "UWC_FRONT_PORT": str(fport)},
    )

    furl = f"http://127.0.0.1:{fport}"
    burl = f"http://127.0.0.1:{port}"
    if not wait_http(f"{burl}/healthz", 30) or not wait_http(furl, 60):
        kill_tree(front)
        kill_tree(backend)
        fail("前后端未在时限内就绪, 查看上方日志")
    banner([
        f"后端:     {burl}",
        f"前端:     {furl}  (代理已对齐后端端口)",
        "模式:     开发(热更新)",
        "停止:     Ctrl+C",
    ])
    if not args.no_open:
        webbrowser.open(furl)
    try:
        front.wait()
    except KeyboardInterrupt:
        pass
    finally:
        kill_tree(front)
        kill_tree(backend)


if __name__ == "__main__":
    main()
