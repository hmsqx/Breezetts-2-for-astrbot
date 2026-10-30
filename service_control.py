"""Manage local Breeze services for the AstrBot process lifetime."""

import asyncio
import ctypes
import itertools
import msvcrt
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psutil

# This controller is for the local Windows installation, not remote servers.
DEFAULT_ROOT = Path("D:/Echok/breeze-cover")
LOCAL_URL = "http://127.0.0.1:7861"


def validate_api_base(api_base, port=7861):
    """Validate the local service URL and return its canonical form.

    Args:
        api_base: Configured service URL.

    Returns:
        The canonical loopback URL.

    Raises:
        ValueError: The URL does not identify the supported local service.
    """
    try:
        url = urlsplit(api_base.strip())
        valid = (
            url.scheme == "http"
            and url.hostname in ("127.0.0.1", "localhost")
            and url.port == port
            and url.username is None
            and url.password is None
            and url.path in ("", "/")
            and not url.query
            and not url.fragment
        )
    except (AttributeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"服务控制按钮只支持本机 http://127.0.0.1:{port}")
    return f"http://127.0.0.1:{port}"


class CoverService:
    """Inspect and launch the locally installed Breeze Cover service."""

    port = 7861
    lock_name = "cover-start.lock"
    log_prefix = "server"
    immediate_start = False

    def __init__(
        self,
        root=DEFAULT_ROOT,
        api_base=LOCAL_URL,
        *,
        job=None,
        jobs=None,
        job_factory=None,
        owner_pid=None,
        owner_create_time=None,
    ):
        """Initialize the service controller.

        Args:
            root: Local service installation directory.
            api_base: Configured local service URL.
        """
        self.root = Path(root).resolve()
        self.api_base = validate_api_base(api_base, self.port)
        self._lock = asyncio.Lock()
        self._started = None
        self.jobs = jobs if jobs is not None else ([job] if job is not None else [])
        self.job = self.jobs[0] if self.jobs else None
        self.job_factory = job_factory
        self.owner_pid = owner_pid
        self.owner_create_time = owner_create_time

    def _matches(self, process):
        try:
            args = process.cmdline()
            if Path(process.cwd()).resolve() != self.root:
                return False
            if not Path(process.exe()).name.lower().startswith("python"):
                return False
            pairs = list(itertools.pairwise(args))
            return (
                ("-m", "uvicorn") in pairs
                and "server:app" in args
                and (("--host", "127.0.0.1") in pairs or "--host=127.0.0.1" in args)
                and (("--port", "7861") in pairs or "--port=7861" in args)
            )
        except (psutil.Error, OSError, ValueError):
            return False

    def _listener(self):
        listeners = [
            c
            for c in psutil.net_connections(kind="tcp")
            if c.status == psutil.CONN_LISTEN and c.laddr.port == self.port
        ]
        if not listeners:
            return None
        for listener in listeners:
            if listener.pid is None or not self._matches(psutil.Process(listener.pid)):
                raise RuntimeError(f"端口 {self.port} 已被其他程序占用或无法确认归属")
        return listeners[0].pid

    def _launching(self):
        if self._started is not None and self._started.poll() is None:
            return True
        for process in psutil.process_iter():
            if self._matches(process):
                return True
        return False

    def _in_job(self, pid, job=None):
        job = self.job if job is None else job
        if job is None:
            return False
        kernel = job._kernel
        kernel.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel.IsProcessInJob.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            result = wintypes.BOOL()
            if not kernel.IsProcessInJob(handle, job._handle, ctypes.byref(result)):
                raise ctypes.WinError(ctypes.get_last_error())
            return bool(result.value)
        finally:
            kernel.CloseHandle(handle)

    def _assign(self, pid):
        if not self.jobs:
            raise RuntimeError("缺少 AstrBot 服务托管对象")
        try:
            self.jobs[0].attach(pid)
            return
        except OSError:
            if self.job_factory is None:
                raise
        fresh = self.job_factory()
        try:
            fresh.attach(pid)
        except Exception:
            # Never close a job that might contain a live process.
            try:
                empty = fresh.active_processes() == 0
            except Exception:
                empty = False
            if empty:
                fresh.close()
            else:
                self.jobs.append(fresh)
            raise
        self.jobs.append(fresh)

    def _adopt(self, process):
        if not self.jobs:
            return False
        if any(self._in_job(process.pid, job) for job in self.jobs):
            return True
        if self.owner_pid is None or self.owner_create_time is None:
            return False
        owner = psutil.Process(self.owner_pid)
        if owner.create_time() != self.owner_create_time:
            return False
        if not any(
            p.pid == self.owner_pid and p.create_time() == self.owner_create_time
            for p in process.parents()
        ):
            return False
        self._assign(process.pid)
        return True

    def adopt_existing(self):
        """Adopt only matching services descended from this AstrBot process."""
        listener = self._listener()
        if listener is not None:
            self._management(listener)

    def _management(self, pid):
        if pid is None:
            return False
        process = psutil.Process(pid)
        if not self._matches(process):
            raise RuntimeError("服务进程身份已变化")
        return self._adopt(process)

    def _ready(self, health):
        return health.get("ready") is True

    def _command(self):
        return [
            str(self.root / ".venv" / "Scripts" / "python.exe"),
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            "7861",
        ]

    async def status(self, api_base=None):
        """Inspect the listener and report its readiness.

        Args:
            api_base: Optional current configured service URL.

        Returns:
            A dictionary containing running, ready, starting and message.
        """
        validate_api_base(self.api_base if api_base is None else api_base, self.port)
        try:
            listener = self._listener()
            if listener is None:
                starting = self._launching()
                managed = (
                    True
                    if starting
                    and self._started is not None
                    and self._started.poll() is None
                    else None
                )
                detail = "AstrBot 托管，退出时关闭" if managed else "托管状态待确认"
                return {
                    "running": False,
                    "ready": False,
                    "starting": starting,
                    "managed": managed,
                    "message": "服务启动中；" + detail if starting else "服务未运行",
                }
            management_error = None
            try:
                managed = self._management(listener)
            except (psutil.Error, OSError, RuntimeError) as exc:
                managed = None
                management_error = str(exc)
            if managed is None:
                ownership = "托管失败：" + management_error
            elif managed:
                ownership = "AstrBot 托管，退出时关闭"
            else:
                ownership = "外部服务，不随 AstrBot 关闭"
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                    response = await client.get(f"{self.api_base}/health")
                    if not (self.immediate_start and response.status_code == 503):
                        response.raise_for_status()
                    health = response.json()
                    ready = self._ready(health) and (
                        not self.immediate_start or response.status_code == 200
                    )
                    result = {
                        "running": True,
                        "ready": ready,
                        "starting": self.immediate_start and not ready,
                        "message": ("服务已就绪" if ready else "服务运行中，模型未就绪")
                        + "；"
                        + ownership,
                        "managed": managed,
                    }
                    if health.get("error"):
                        result["error"] = str(health["error"])
                    return result
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                return {
                    "running": True,
                    "ready": False,
                    "starting": self.immediate_start,
                    "message": "服务运行中，暂时无法读取状态；" + ownership,
                    "managed": managed,
                    "error": str(exc),
                }
        except (psutil.Error, OSError, RuntimeError) as exc:
            return {
                "running": False,
                "ready": False,
                "starting": False,
                "message": "无法确认服务状态",
                "error": str(exc),
            }

    async def stop(self, api_base=None, *, can_stop=None):
        """Stop only the verified service process, preserving other services.

        Args:
            api_base: Optional current configured local service URL.
            can_stop: Synchronous guard checked immediately before termination.

        Returns:
            The stopped state, or an error without stopping unrelated processes.
        """
        validate_api_base(self.api_base if api_base is None else api_base, self.port)
        deadline = time.monotonic() + 15
        async with self._lock:
            tmp = self.root / "tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                tmp / self.lock_name, os.O_CREAT | os.O_RDWR | os.O_BINARY
            )
            with os.fdopen(descriptor, "r+b", buffering=0) as lock:
                while True:
                    try:
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "另一个服务控制请求仍在处理中，请稍后重试。"
                            )
                        await asyncio.sleep(0.2)
                try:
                    if can_stop is not None and not can_stop():
                        raise RuntimeError("歌曲任务正在进行，请等任务完成后关闭服务。")
                    listeners = {
                        item.pid
                        for item in psutil.net_connections(kind="tcp")
                        if item.status == psutil.CONN_LISTEN
                        and item.laddr.port == self.port
                    }
                    if None in listeners or len(listeners) > 1:
                        raise RuntimeError("无法确认唯一的服务进程，请稍后刷新状态。")
                    listening = bool(listeners)
                    if listening:
                        process = psutil.Process(listeners.pop())
                        if not self._matches(process):
                            raise RuntimeError(
                                f"端口 {self.port} 不属于预期服务，未关闭任何程序。"
                            )
                    else:
                        candidates = [
                            p for p in psutil.process_iter() if self._matches(p)
                        ]
                        parent_ids = {p.ppid() for p in candidates}
                        candidates = [p for p in candidates if p.pid not in parent_ids]
                        if not candidates:
                            self._started = None
                            return {
                                "running": False,
                                "ready": False,
                                "starting": False,
                                "message": "服务已关闭。",
                            }
                        if len(candidates) != 1:
                            raise RuntimeError(
                                "存在多个启动中的进程，无法安全确认关闭目标。"
                            )
                        process = candidates[0]
                    identity = (
                        process.pid,
                        process.create_time(),
                        process.cmdline(),
                        process.exe(),
                    )
                    token = None
                    terminated = False
                    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                        try:
                            if self.port == 7860 and listening:
                                response = await client.post(
                                    f"{self.api_base}/internal/cover-drain"
                                )
                                if response.status_code == 409:
                                    raise RuntimeError(
                                        "普通语音正在合成，请完成后再关闭。"
                                    )
                                if response.status_code != 200:
                                    raise RuntimeError(
                                        "无法安全暂停普通语音，未关闭服务。"
                                    )
                                token = response.json()["token"]
                            if can_stop is not None and not can_stop():
                                raise RuntimeError(
                                    "歌曲任务正在进行，请等任务完成后关闭服务。"
                                )
                            current = psutil.Process(identity[0])
                            if (
                                not self._matches(current)
                                or (
                                    current.pid,
                                    current.create_time(),
                                    current.cmdline(),
                                    current.exe(),
                                )
                                != identity
                            ):
                                raise RuntimeError("服务进程已变化，未关闭任何程序。")
                            if self.port == 7861:
                                for child in current.children(recursive=True):
                                    args = child.cmdline()
                                    if (
                                        child.name().lower().startswith("ffmpeg")
                                        or "yt_dlp" in args
                                        or any(
                                            arg.endswith(
                                                (
                                                    "run_demucs.py",
                                                    "inference.py",
                                                    "transcribe_song.py",
                                                )
                                            )
                                            for arg in args
                                        )
                                    ):
                                        raise RuntimeError(
                                            "翻唱后台任务正在运行，请完成后再关闭。"
                                        )
                            # The restored TTS can be a Cover child; never terminate the tree or job.
                            current.terminate()
                            terminated = True
                            await asyncio.to_thread(current.wait, 10)
                        finally:
                            if token is not None and not terminated:
                                try:
                                    await client.post(
                                        f"{self.api_base}/internal/cover-drain/release",
                                        data={"token": token},
                                    )
                                except httpx.HTTPError:
                                    pass
                    self._started = None
                    for _ in range(20):
                        if not self._launching():
                            return {
                                "running": False,
                                "ready": False,
                                "starting": False,
                                "message": "服务已关闭。",
                            }
                        await asyncio.sleep(0.2)
                    return {
                        "running": False,
                        "ready": False,
                        "starting": False,
                        "stopping": True,
                        "message": "服务正在退出，请稍后刷新状态。",
                    }
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    async def start(self, api_base=None):
        """Start the service once, or reuse an existing local process.

        Args:
            api_base: Optional current configured service URL.

        Returns:
            A dictionary containing running, ready, starting and message.
        """
        validate_api_base(self.api_base if api_base is None else api_base, self.port)
        deadline = time.monotonic() + 15
        async with self._lock:
            tmp = self.root / "tmp"
            logs = self.root / "logs"
            try:
                tmp.mkdir(parents=True, exist_ok=True)
                logs.mkdir(parents=True, exist_ok=True)
                # Region locking also serializes independent plugin reloads.
                descriptor = os.open(
                    tmp / self.lock_name, os.O_CREAT | os.O_RDWR | os.O_BINARY
                )
                with os.fdopen(descriptor, "r+b", buffering=0) as lock:
                    while True:
                        try:
                            lock.seek(0)
                            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                return {
                                    "running": False,
                                    "ready": False,
                                    "starting": True,
                                    "message": "另一个启动请求仍在处理中",
                                }
                            await asyncio.sleep(0.2)
                    try:
                        state = await self.status()
                        if state["running"] or state["starting"] or state.get("error"):
                            return state
                        if time.monotonic() >= deadline:
                            return {
                                "running": False,
                                "ready": False,
                                "starting": True,
                                "message": "启动请求等待超时，请重试",
                            }
                        env = os.environ.copy()
                        env.update(TEMP=str(tmp), TMP=str(tmp))
                        audio_tools = self.root.parent / "tools" / "ffmpeg" / "bin"
                        env["PATH"] = (
                            str(audio_tools) + os.pathsep + env.get("PATH", "")
                        )
                        with (
                            (logs / f"{self.log_prefix}.out.log").open("ab") as out,
                            (logs / f"{self.log_prefix}.err.log").open("ab") as err,
                        ):
                            process = subprocess.Popen(  # noqa: ASYNC220
                                self._command(),
                                cwd=str(self.root),
                                env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=out,
                                stderr=err,
                                creationflags=subprocess.CREATE_NO_WINDOW
                                | subprocess.CREATE_NEW_PROCESS_GROUP
                                | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004),
                            )
                        try:
                            self._assign(process.pid)
                            psutil.Process(process.pid).resume()
                        except Exception as exc:
                            try:
                                process.terminate()
                                process.wait(timeout=10)
                            except Exception as cleanup_error:
                                raise RuntimeError(
                                    f"托管失败且清理进程失败：{cleanup_error}"
                                ) from exc
                            raise RuntimeError(
                                f"无法托管或恢复服务进程：{exc}"
                            ) from exc
                        self._started = process
                        if self.immediate_start:
                            return {
                                "running": False,
                                "ready": False,
                                "starting": True,
                                "message": "服务正在加载模型；AstrBot 托管，退出时关闭",
                                "managed": True,
                            }
                        while time.monotonic() < deadline:
                            state = await self.status()
                            if state["running"] or state.get("error"):
                                return state
                            if (
                                self._started.poll() is not None
                                and not state["starting"]
                            ):
                                return {
                                    "running": False,
                                    "ready": False,
                                    "starting": False,
                                    "message": "服务启动失败，请查看日志",
                                    "error": "进程已退出",
                                }
                            await asyncio.sleep(0.2)
                        return {
                            "running": False,
                            "ready": False,
                            "starting": True,
                            "message": "服务仍在启动，请稍后查看日志和状态",
                        }
                    finally:
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, RuntimeError, psutil.Error) as exc:
                return {
                    "running": False,
                    "ready": False,
                    "starting": False,
                    "message": "服务启动失败",
                    "error": str(exc),
                }


class BreezeService(CoverService):
    """Inspect and launch the local ordinary Breeze TTS service."""

    port = 7860
    lock_name = "tts-start.lock"
    log_prefix = "tts"
    immediate_start = True

    def __init__(
        self,
        root=Path("D:/Echok/breeze-tts"),
        api_base="http://127.0.0.1:7860",
        *,
        job=None,
        jobs=None,
        job_factory=None,
        owner_pid=None,
        owner_create_time=None,
    ):
        super().__init__(
            root,
            api_base,
            job=job,
            jobs=jobs,
            job_factory=job_factory,
            owner_pid=owner_pid,
            owner_create_time=owner_create_time,
        )

    def _matches(self, process):
        try:
            args = process.cmdline()
            pairs = list(itertools.pairwise(args))
            return (
                Path(process.cwd()).resolve() == self.root
                and Path(process.exe()).name.lower().startswith("python")
                and ("-m", "breeze_infer.api") in pairs
                and "../breeze-tts-2" in args
                and (("--host", "127.0.0.1") in pairs or "--host=127.0.0.1" in args)
                and (("--port", "7860") in pairs or "--port=7860" in args)
            )
        except (psutil.Error, OSError, ValueError):
            return False

    def _ready(self, health):
        return health.get("status") == "ok"

    def _command(self):
        installed_python = self.root / ".venv" / "Scripts" / "python.exe"
        return [
            str(installed_python)
            if installed_python.is_file()
            else str(
                Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
                / "Programs/Python/Python312/python.exe"
            ),
            "-u",
            "-m",
            "breeze_infer.api",
            "../breeze-tts-2",
            "--host",
            "127.0.0.1",
            "--port",
            "7860",
        ]
