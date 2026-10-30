"""Install the local service environments and models on an explicit dashboard request."""

import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

EVENT = "BREEZE_INSTALL "
PYTHON = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    / "Programs/Python/Python312/python.exe"
)
HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
if HF not in {"https://huggingface.co", "https://hf-mirror.com"}:
    HF = "https://huggingface.co"
DEFAULT_HF = HF
MODEL_SOURCE = "auto"
SEED_REVISION = "51383efd921027683c89e5348211d93ff12ac2a8"
SEED_ARCHIVE_SHA = "72a27a1d20efec1489041eac2854e3e1c9c23d650ac15900821ad3ab367f0290"
BUNDLED = Path(__file__).resolve().parent / "runtime"
SOURCE_CHOICES = {
    "python": {"auto", "tuna", "aliyun", "official"},
    "cuda": {"auto", "aliyun", "sjtu", "official"},
    "model": {"auto", "mirror", "official"},
}


def normalize_sources(value=None):
    """Validate fixed source IDs at the HTTP and worker boundaries.

    Args:
        value: Optional source selection dictionary; URLs are never accepted.

    Returns:
        All three source IDs with auto defaults.

    Raises:
        ValueError: Unknown fields, types or source IDs were supplied.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - SOURCE_CHOICES.keys():
        raise ValueError("下载源配置无效，请从页面下拉框选择。")
    result = {}
    for key, choices in SOURCE_CHOICES.items():
        selected = value.get(key, "auto")
        if not isinstance(selected, str) or selected not in choices:
            raise ValueError(f"{key}下载源无效，请从页面下拉框选择。")
        result[key] = selected
    return result


def prerequisites(kind, tts, cover):
    """Check cheap local prerequisites before accepting an install request.

    Args:
        kind: The tts or cover service key.
        tts: Speech source root.
        cover: Singing source root.

    Returns:
        Human-readable blockers; no network or environment changes occur.
    """
    problems = []
    if kind not in {"tts", "cover"}:
        return ["未知服务类型。"]
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        problems.append("一键安装目前只支持 Windows x64。")
    if not (PYTHON.is_file() or sys.version_info[:2] == (3, 12)):
        problems.append("需要 Python 3.12；请使用含 Python 3.12 的 AstrBot 环境。")
    root = Path(tts if kind == "tts" else cover).resolve()
    source = BUNDLED / kind
    required = (
        ("breeze_infer/api.py", "models/breeze.py", "configs/fast.json")
        if kind == "tts"
        else ("server.py", "public_proxy.py", "transcribe_song.py", "run_demucs.py")
    )
    missing = [
        name
        for name in required
        if not (root / name).is_file() and not (source / name).is_file()
    ]
    if missing:
        problems.append(
            "插件缺少配套后端文件，请重新安装完整插件：" + "、".join(missing)
        )
    if Path(tts).resolve().parent != Path(cover).resolve().parent:
        problems.append("普通语音和翻唱必须位于同一个本机服务目录下。")
    return problems


class ServiceInstaller:
    """Retain progress across page refreshes and serialize with local service controls."""

    def __init__(self, tts, cover, states):
        self.tts, self.cover, self.states = tts, cover, states

    @property
    def busy(self):
        return any(state.get("active") for state in self.states.values())

    def snapshot(self, kind):
        """Return a JSON-safe progress snapshot for one service.

        Args:
            kind: The fixed tts or cover service key.

        Returns:
            Progress, recent log lines and the shared installation busy flag.
        """
        state = dict(
            self.states.get(kind)
            or {
                "active": False,
                "state": "idle",
                "stage": "尚未安装或检查",
                "step": 0,
                "steps": 5,
                "progress": None,
                "detail": "",
                "error": None,
                "logs": [],
            }
        )
        state["logs"] = list(state["logs"])
        state["busy"] = self.busy
        state["blockers"] = prerequisites(kind, self.tts.root, self.cover.root)
        state["elapsed_seconds"] = (
            round(max(0, time.time() - state["started_at"]), 1)
            if state.get("active")
            else state.get("elapsed_seconds", 0)
        )
        return state

    def begin(self, kind, sources=None):
        self.states[kind] = {
            "active": True,
            "state": "running",
            "stage": "正在准备安装",
            "step": 0,
            "steps": 5,
            "progress": None,
            "detail": "",
            "error": None,
            "logs": [],
            "started_at": time.time(),
            "sources": normalize_sources(sources),
        }

    async def run(self, kind):
        """Run the installer in a managed child without loading either model.

        Args:
            kind: The fixed tts or cover service key.
        """
        import psutil

        state, process = self.states[kind], None
        try:
            async with self.tts._lock, self.cover._lock:
                for controller in (self.tts, self.cover):
                    current = await controller.status()
                    if (
                        current.get("running")
                        or current.get("starting")
                        or current.get("error")
                    ):
                        raise RuntimeError(
                            "请先关闭普通语音和翻唱服务；无法确认服务已停止时不能安装。"
                        )
                env = dict(
                    os.environ,
                    PYTHONUTF8="1",
                    PYTHONIOENCODING="utf-8",
                    PIP_DISABLE_PIP_VERSION_CHECK="1",
                    PIP_NO_INPUT="1",
                    GIT_TERMINAL_PROMPT="0",
                    PIP_PROGRESS_BAR="off",
                    PIP_RETRIES="5",
                    PIP_TIMEOUT="45",
                    PYTHONUNBUFFERED="1",
                )
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        str(Path(__file__).resolve()),
                        kind,
                        str(self.tts.root),
                        str(self.cover.root),
                        json.dumps(state.get("sources", normalize_sources())),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                    | 0x00000004,
                )
                self.cover._assign(process.pid)
                psutil.Process(process.pid).resume()
                async with asyncio.timeout(3 * 60 * 60):
                    while line := await asyncio.to_thread(
                        process.stdout.readline, 65536
                    ):
                        line = line.rstrip()
                        state["last_output_at"] = time.time()
                        if line.startswith(EVENT):
                            event = json.loads(line[len(EVENT) :])
                            for key in ("stage", "step", "steps", "progress", "detail"):
                                if key in event:
                                    state[key] = event[key]
                        elif line:
                            state["logs"] = (state["logs"] + [line[-1500:]])[-35:]
                    code = await asyncio.to_thread(process.wait)
                if code != 0:
                    raise RuntimeError(
                        state.get("detail") or "安装进程异常退出，请展开安装日志。"
                    )
                state.update(
                    state="completed",
                    stage="安装检查通过",
                    progress=1.0,
                    detail="依赖导入、音频工具与模型文件检查通过。请点击启动服务；尚未加载模型或验证推理。",
                )
        except asyncio.CancelledError:
            state.update(
                state="cancelled",
                stage="安装已中断",
                progress=None,
                error="安装已停止。已完成文件和可续传下载保留，可点击继续安装。",
            )
            raise
        except TimeoutError:
            state.update(
                state="failed",
                stage="安装超时",
                progress=None,
                error="安装超过三小时，已停止安装进程；可检查网络后重试。",
            )
        except Exception as exc:
            state.update(
                state="failed", stage="安装失败", progress=None, error=str(exc)[:2000]
            )
        finally:
            try:
                if process is not None:
                    if process.poll() is None:

                        def stop_worker():
                            try:
                                parent = psutil.Process(process.pid)
                                children = parent.children(recursive=True)
                                for child in reversed(children):
                                    try:
                                        child.kill()
                                    except psutil.NoSuchProcess:
                                        pass
                                parent.kill()
                                psutil.wait_procs(children + [parent], timeout=10)
                            except psutil.NoSuchProcess:
                                pass

                        await asyncio.to_thread(stop_worker)
                    if process.stdout:
                        process.stdout.close()
            except (OSError, psutil.Error) as exc:
                state.update(
                    state="failed", stage="安装进程清理失败", error=str(exc)[:2000]
                )
            finally:
                state["active"] = False
                state["elapsed_seconds"] = round(time.time() - state["started_at"], 1)


def report(stage, step, detail="", progress=None):
    """Write a structured stage update consumed by the dashboard controller."""
    print(
        EVENT
        + json.dumps(
            dict(stage=stage, step=step, steps=5, detail=detail, progress=progress),
            ensure_ascii=False,
        ),
        flush=True,
    )


def command(args, cwd):
    """Stream one fixed installation command and fail on a nonzero exit code."""
    import psutil

    options = {}
    if len(args) > 3 and [str(value) for value in args[1:4]] == [
        "-m",
        "pip",
        "install",
    ]:
        env = dict(os.environ, PIP_CONFIG_FILE=os.devnull)
        for key in (
            "PIP_INDEX_URL",
            "PIP_EXTRA_INDEX_URL",
            "PIP_FIND_LINKS",
            "PIP_NO_INDEX",
        ):
            env.pop(key, None)
        options["env"] = env
    process = subprocess.Popen([str(arg) for arg in args], cwd=str(cwd), **options)
    try:
        code = process.wait(timeout=1800)
    except subprocess.TimeoutExpired as exc:
        try:
            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
            psutil.wait_procs(children + [parent], timeout=10)
        except psutil.NoSuchProcess:
            pass
        raise RuntimeError(
            "当前安装命令超过30分钟未完成，请检查网络后继续安装；具体命令见日志。"
        ) from exc
    if code:
        raise RuntimeError(
            f"安装命令执行失败（退出码 {code}）。请查看下方日志末尾的错误；网络问题可重试，DLL错误请检查显卡驱动和VC++运行库。"
        )


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            value.update(block)
    return value.hexdigest()


def pip_install(
    python, packages, root, *, cuda=False, source="auto", dependency_source="auto"
):
    """Try fixed package mirrors without changing the user's pip settings.

    Args:
        python: The service's isolated interpreter.
        packages: Requirements, including any fixed CUDA build suffixes.
        root: Working directory for installation.
        cuda: Whether to use the dedicated CUDA wheel repositories.
        source: Auto fallback or one explicitly selected source ID.
        dependency_source: Python source used for CUDA's ordinary dependencies.

    Raises:
        RuntimeError: Every allowed source failed; detailed pip output is retained.
    """
    sources = (
        [
            (
                "阿里云CUDA镜像 / 清华依赖源",
                [
                    "--index-url",
                    "https://pypi.tuna.tsinghua.edu.cn/simple",
                    "--find-links",
                    "https://mirrors.aliyun.com/pytorch-wheels/cu128/",
                ],
            ),
            (
                "上海交大CUDA镜像",
                ["--index-url", "https://mirror.sjtu.edu.cn/pytorch-wheels/cu128/"],
            ),
            (
                "PyTorch官方CUDA源",
                ["--index-url", "https://download.pytorch.org/whl/cu128"],
            ),
        ]
        if cuda
        else [
            (
                "清华PyPI镜像",
                ["--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple"],
            ),
            (
                "阿里云PyPI镜像",
                ["--index-url", "https://mirrors.aliyun.com/pypi/simple/"],
            ),
            ("PyPI官方源", ["--index-url", "https://pypi.org/simple"]),
        ]
    )
    allowed = (
        ("auto", "aliyun", "sjtu", "official")
        if cuda
        else ("auto", "tuna", "aliyun", "official")
    )
    normalize_sources({"python": dependency_source})
    if cuda:
        index = {
            "auto": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "tuna": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "aliyun": "https://mirrors.aliyun.com/pypi/simple/",
            "official": "https://pypi.org/simple",
        }[dependency_source]
        sources[0][1][1] = index
        sources[0] = ("阿里云CUDA镜像 / 已选Python依赖源", sources[0][1])
    if source not in allowed:
        raise ValueError("依赖源选择无效。")
    if source != "auto":
        sources = [sources[allowed.index(source) - 1]]
    for attempt, (label, options) in enumerate(sources, 1):
        report(
            "安装运行依赖",
            2,
            f"正在使用{label}（{attempt}/{len(sources)}）；"
            + ("不可用时自动换源。" if source == "auto" else "手动指定，仅使用此源。"),
        )
        try:
            command(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--retries",
                    "2",
                    "--timeout",
                    "20",
                    *options,
                    *packages,
                ],
                root,
            )
            return
        except RuntimeError as exc:
            if attempt == len(sources):
                raise RuntimeError(
                    f"已依次尝试{len(sources)}个依赖源，仍未安装成功。最后错误：{exc}。若日志是版本冲突、编译或DLL错误，换源也无法修复，请按具体错误处理。"
                ) from exc
            print(
                f"Package source {attempt} failed; trying the next mirror.", flush=True
            )


def download(url, path, stage, step, size=None, sha=None):
    """Retry public downloads, preserving verified files and resumable partials.

    Args:
        url: Fixed public upstream URL.
        path: Destination under the service root.
        stage: Dashboard stage label.
        step: Dashboard step number.
        size: Expected byte count, if known.
        sha: Expected SHA-256, if known.

    Returns:
        The verified destination path.

    Raises:
        RuntimeError: All attempts failed, with an actionable download error.
    """
    if MODEL_SOURCE != "auto":
        for host in ("https://huggingface.co", "https://hf-mirror.com"):
            if url.startswith(host + "/"):
                selected_host = (
                    "https://hf-mirror.com"
                    if MODEL_SOURCE == "mirror"
                    else "https://huggingface.co"
                )
                url = selected_host + url[len(host) :]
                break
    for attempt in range(3):
        endpoint = url
        if attempt == 1 and MODEL_SOURCE == "auto":
            if url.startswith("https://huggingface.co/"):
                endpoint = url.replace(
                    "https://huggingface.co/", "https://hf-mirror.com/", 1
                )
            elif url.startswith("https://hf-mirror.com/"):
                endpoint = url.replace(
                    "https://hf-mirror.com/", "https://huggingface.co/", 1
                )
        try:
            return _download_once(endpoint, path, stage, step, size, sha)
        except (OSError, URLError, RuntimeError, ValueError) as exc:
            if isinstance(exc, HTTPError) and exc.code in {400, 401, 404}:
                raise RuntimeError(
                    f"{path.name} 下载被上游拒绝（HTTP {exc.code}），请检查模型来源或更新插件。"
                ) from exc
            if isinstance(exc, OSError) and exc.errno in {13, 28}:
                raise RuntimeError(
                    f"无法写入 {path}，请检查磁盘空间和目录权限。"
                ) from exc
            if attempt == 2:
                raise RuntimeError(
                    f"{path.name} 下载失败，已尝试3次：{exc}。保留可续传文件，检查网络后点击继续安装。"
                ) from exc
            report(
                stage,
                step,
                f"{path.name} 下载中断，正在第{attempt + 2}/3次尝试；"
                + (
                    "模型源会尝试官方站与镜像。"
                    if MODEL_SOURCE == "auto"
                    else "保持手动指定的模型源。"
                ),
            )
            time.sleep(attempt + 1)


def _download_once(url, path, stage, step, size=None, sha=None):
    """Resume a public model download and atomically publish verified contents.

    Args:
        url: A URL constructed from fixed upstream repositories.
        path: Destination under the local service or model directory.
        stage: Human-readable installation stage.
        step: The current stage number.
        size: Expected file size, if supplied by the upstream manifest.
        sha: Expected SHA-256 or its published prefix, when available.

    Returns:
        The completed file path.
    """
    stage = f"{stage} · {urlsplit(url).hostname}"
    path.parent.mkdir(parents=True, exist_ok=True)
    report(stage, step, f"检查 {path.name}")
    valid_json = True
    if path.is_file() and path.suffix == ".json":
        try:
            valid_json = isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
        except (ValueError, OSError):
            valid_json = False
    if (
        path.is_file()
        and valid_json
        and path.stat().st_size > 0
        and (size is None or path.stat().st_size == size)
    ):
        if sha and digest(path).startswith(sha):
            report(stage, step, f"已校验并复用 {path.name}", 1.0)
            return path
        if not sha and size is not None:
            report(stage, step, f"复用已有 {path.name}", 1.0)
            return path
        if not sha and path.suffix == ".json":
            try:
                if isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
                    report(stage, step, f"复用有效配置 {path.name}", 1.0)
                    return path
            except (ValueError, OSError):
                pass
        if not sha and path.suffix == ".zip" and zipfile.is_zipfile(path):
            return path
    partial = path.with_name(path.name + ".download")
    offset = partial.stat().st_size if size is not None and partial.is_file() else 0
    if size is not None and offset >= size:
        partial_valid = offset == size and (not sha or digest(partial).startswith(sha))
        if partial_valid and path.suffix == ".json":
            try:
                partial_valid = isinstance(
                    json.loads(partial.read_text(encoding="utf-8")), dict
                )
            except (ValueError, OSError):
                partial_valid = False
        if partial_valid:
            os.replace(partial, path)
            report(stage, step, f"已完成 {path.name}", 1.0)
            return path
        partial.unlink()
        offset = 0
    headers = {"User-Agent": "Breeze-Local-Installer/1.0"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    with urlopen(Request(url, headers=headers), timeout=90) as response:
        if offset and response.status != 206:
            offset = 0
        if response.status == 206 and not response.headers.get(
            "Content-Range", ""
        ).startswith(f"bytes {offset}-"):
            raise RuntimeError(f"{path.name} 断点响应不正确，请重试。")
        total = size
        if total is None and response.headers.get("Content-Length"):
            total = offset + int(response.headers["Content-Length"])
        last = 0.0
        started, initial = time.monotonic(), offset
        with partial.open("ab" if offset else "wb") as stream:
            while block := response.read(1024 * 1024):
                stream.write(block)
                offset += len(block)
                if total is not None and offset > total:
                    raise RuntimeError(f"{path.name} 下载大小超过预期。")
                if time.monotonic() - last > 0.4:
                    detail = f"{path.name}：{offset / 1048576:.1f} MiB"
                    if total:
                        detail += f" / {total / 1048576:.1f} MiB"
                    elapsed = max(0.001, time.monotonic() - started)
                    detail += f" · {(offset - initial) / elapsed / 1048576:.1f} MiB/s"
                    report(
                        stage,
                        step,
                        detail,
                        min(0.999, offset / total) if total else None,
                    )
                    last = time.monotonic()
    report(stage, step, f"正在校验 {path.name}")
    if offset == 0 or (total is not None and offset != total):
        raise RuntimeError(f"{path.name} 下载不完整，可重新点击安装继续。")
    if sha and not digest(partial).startswith(sha):
        partial.unlink()
        raise RuntimeError(f"{path.name} SHA-256 校验失败，可重新下载安装。")
    if path.suffix == ".json":
        try:
            if not isinstance(json.loads(partial.read_text(encoding="utf-8")), dict):
                raise ValueError("Expected a JSON object")
        except ValueError:
            partial.unlink()
            raise
    os.replace(partial, path)
    report(stage, step, f"已完成 {path.name}", 1.0)
    return path


def install(kind, tts, cover, sources=None):
    """Prepare the existing Windows backend sources, dependencies and model files.

    Args:
        kind: The fixed tts or cover service key.
        tts: The ordinary speech source directory supplied by its controller.
        cover: The singing backend directory supplied by its controller.
        sources: Validated fixed download source IDs.
    """
    import msvcrt

    global HF, MODEL_SOURCE
    sources = normalize_sources(sources)
    MODEL_SOURCE = sources["model"]
    HF = {"mirror": "https://hf-mirror.com", "official": "https://huggingface.co"}.get(
        MODEL_SOURCE, DEFAULT_HF
    )
    tts, cover = tts.resolve(), cover.resolve()
    blockers = prerequisites(kind, tts, cover)
    if blockers:
        raise RuntimeError("；".join(blockers))
    root = tts if kind == "tts" else cover
    python = PYTHON if PYTHON.is_file() else Path(sys.executable)
    if not PYTHON.is_file() and sys.version_info[:2] != (3, 12):
        raise RuntimeError("需要先安装 Python 3.12，再点击一键安装。")
    tmp = root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    with (cover.parent / ".breeze-install.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("另一个安装程序正在运行，请稍后重试。") from exc
        report(
            "检查安装条件", 1, "检查目录、可用空间与配套源码，不改动歌库或音色配置。"
        )
        models_exist = (
            (tts.parent / "breeze-tts-2/model.safetensors.index.json").is_file()
            if kind == "tts"
            else (cover / "weights/svc.pth").is_file()
        )
        needed = (2 if models_exist else 16 if kind == "tts" else 12) * 1024**3
        free = shutil.disk_usage(root).free
        if free < needed:
            raise RuntimeError(
                f"磁盘可用空间仅{free / 1024**3:.1f} GiB，建议至少留出{needed / 1024**3:.0f} GiB用于依赖和模型。请清理其他文件后继续安装。"
            )
        bundled = BUNDLED / kind
        if bundled.is_dir():
            for source in bundled.rglob("*"):
                if not source.is_file():
                    continue
                dest = root / source.relative_to(bundled)
                if not dest.resolve().is_relative_to(root) or dest.is_symlink():
                    raise RuntimeError("后端源码目标包含无效路径或链接。")
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    pending = dest.with_name(dest.name + ".installing")
                    shutil.copyfile(source, pending)
                    os.replace(pending, dest)
        report("准备安装环境", 1, "创建隔离环境，已安装的兼容依赖会复用。")
        env_python = root / ".venv/Scripts/python.exe"
        if not env_python.is_file():
            command([python, "-m", "venv", root / ".venv"], root)
        command(
            [
                env_python,
                "-c",
                "import sys; assert sys.version_info[:2] == (3, 12), 'This environment requires Python 3.12'",
            ],
            root,
        )
        command([env_python, "-m", "ensurepip", "--upgrade"], root)
        report(
            "安装运行依赖",
            2,
            "正在处理 Python 与 CUDA 依赖；此阶段无法测量准确百分比。",
        )
        pip_install(
            env_python,
            ["torch==2.9.1+cu128", "torchaudio==2.9.1+cu128"],
            root,
            cuda=True,
            source=sources["cuda"],
            dependency_source=sources["python"],
        )
        common = [
            "transformers==4.57.3",
            "huggingface-hub>=0.34,<1",
            "numpy>=2.0,<3",
            "soundfile>=0.13",
            "fastapi>=0.115",
            "uvicorn>=0.30",
            "python-multipart>=0.0.18",
        ]
        packages = (
            ["qwen-tts==0.1.1"]
            if kind == "tts"
            else [
                "demucs==4.0.1",
                "yt-dlp>=2026.10.26",
                "httpx",
                "psutil",
                "scipy",
                "librosa>=0.10.2",
                "munch==4.0.0",
                "einops>=0.8",
                "hydra-core==1.3.2",
                "accelerate",
                "matplotlib",
            ]
        )
        pip_install(env_python, [*common, *packages], root, source=sources["python"])
        report("准备音频工具", 3)
        tools = cover.parent / "tools/ffmpeg/bin"
        if not all(
            shutil.which(name) or (tools / (name + ".exe")).is_file()
            for name in ("ffmpeg", "ffprobe")
        ):
            try:
                archive = download(
                    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
                    tmp / "ffmpeg-essentials.zip",
                    "下载音频工具（Gyan）",
                    3,
                )
            except RuntimeError:
                report(
                    "切换音频工具来源",
                    3,
                    "Gyan下载未完成，正在尝试FFmpeg官方列出的BtbN构建。",
                )
                archive = download(
                    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-8.1.zip",
                    tmp / "ffmpeg-btbn.zip",
                    "下载音频工具（BtbN）",
                    3,
                )
            tools.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as bundle:
                for name in ("ffmpeg.exe", "ffprobe.exe"):
                    members = [
                        item
                        for item in bundle.infolist()
                        if Path(item.filename).name == name
                        and item.file_size <= 300 * 1024 * 1024
                    ]
                    if len(members) != 1:
                        raise RuntimeError("FFmpeg 安装包内容不正确。")
                    target = tools / (name + ".download")
                    with bundle.open(members[0]) as src, target.open("wb") as dest:
                        shutil.copyfileobj(src, dest)
                    os.replace(target, tools / name)
        if kind == "tts":
            models = tts.parent / "breeze-tts-2"
            report("下载普通语音模型", 4, f"正在从 {HF} 读取 Breeze TTS 2 模型清单。")
            manifest_path = tmp / "tts-model-manifest.json"
            manifest_code = """
import json, sys
from pathlib import Path
from huggingface_hub import HfApi
endpoint = sys.argv[2]
attempts = 2 if sys.argv[3] == 'auto' else 1
for attempt in range(attempts):
    try:
        files = []
        for item in HfApi(endpoint=endpoint, token=False).list_repo_tree('BreezeBlue/breeze-tts-2', recursive=True):
            if hasattr(item, 'size'):
                files.append(dict(type='file', path=item.path, size=item.size, lfs=dict(oid=item.lfs.sha256) if item.lfs else {}))
        Path(sys.argv[1]).write_text(json.dumps(files), encoding='utf-8')
        break
    except Exception:
        if attempt + 1 == attempts:
            raise
        endpoint = 'https://hf-mirror.com' if endpoint == 'https://huggingface.co' else 'https://huggingface.co'
        print('Model manifest unavailable; trying the alternate public endpoint.', flush=True)
"""
            command(
                [env_python, "-c", manifest_code, manifest_path, HF, MODEL_SOURCE], root
            )
            files = json.loads(manifest_path.read_text(encoding="utf-8"))
            selected = []
            for item in files:
                name = item.get("path", "")
                if item.get("type") != "file" or Path(name).suffix not in {
                    ".json",
                    ".safetensors",
                    ".txt",
                    ".model",
                    ".bin",
                }:
                    continue
                if name.startswith("assets/"):
                    continue
                dest = (models / name).resolve()
                if not dest.is_relative_to(models.resolve()):
                    raise RuntimeError("模型清单包含无效路径。")
                sha = (item.get("lfs") or {}).get("oid")
                selected.append((name, dest, item.get("size"), sha))
            names = {item[0] for item in selected}
            if not {
                "config.json",
                "model.safetensors.index.json",
                "tokenizer.json",
                "audio_tokenizer/config.json",
                "audio_tokenizer/model.safetensors",
            }.issubset(names):
                raise RuntimeError("Breeze TTS 2 模型清单不完整。")
            for index, (name, dest, size, sha) in enumerate(selected, 1):
                download(
                    HF + "/BreezeBlue/breeze-tts-2/resolve/main/" + name,
                    dest,
                    f"下载普通语音模型 {index}/{len(selected)}",
                    4,
                    size,
                    sha,
                )
            report("整理模型文件", 5)
            index = json.loads(
                (models / "model.safetensors.index.json").read_text(encoding="utf-8")
            )
            for name in set(index["weight_map"].values()):
                path = (models / name).resolve()
                if not path.is_relative_to(models.resolve()) or not path.is_file():
                    raise RuntimeError("模型分片缺失或路径无效。")
        else:
            prepare_cover(cover)
        report(
            "检查依赖与音频工具",
            5,
            "检查实际导入与CUDA版本，不加载模型、不占用推理显存。",
        )
        env = dict(
            os.environ,
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            CUDA_VISIBLE_DEVICES="",
            PYTHONUTF8="1",
        )
        modules = (
            "import breeze_infer.api"
            if kind == "tts"
            else "import server, demucs.separate, librosa, hydra; sys.path.insert(0, 'seed-vc'); import inference; from modules.flow_matching import CFM; from modules.length_regulator import InterpolateRegulator; from modules.rmvpe import RMVPE; from modules.campplus.DTDNN import CAMPPlus; from modules.bigvgan import bigvgan; from transformers import WhisperModel, WhisperForConditionalGeneration"
        )
        probe = (
            "import sys, torch, torchaudio; assert torch.version.cuda is not None, 'CPU-only PyTorch'; assert torch.__version__.split('+')[0] == torchaudio.__version__.split('+')[0], 'PyTorch/torchaudio mismatch'; "
            + modules
        )
        result = subprocess.run(
            [str(env_python), "-c", probe], cwd=root, env=env, timeout=180, check=False
        )
        if result.returncode:
            raise RuntimeError(
                "文件已下载，但依赖导入检查失败。请查看下方日志修复缺失依赖或DLL后继续安装，不能视为安装成功。"
            )
        tool_env = dict(os.environ)
        tool_env["PATH"] = str(tools) + os.pathsep + tool_env.get("PATH", "")
        for name in ("ffmpeg", "ffprobe"):
            executable = shutil.which(name, path=tool_env["PATH"])
            if not executable:
                raise RuntimeError(f"未找到音频工具 {name}，请继续安装补齐。")
            subprocess.run(
                [executable, "-version"],
                env=tool_env,
                timeout=15,
                check=True,
                stdout=subprocess.DEVNULL,
            )
        report(
            "安装检查通过",
            5,
            "依赖导入、音频工具与模型文件检查通过，未自动启动或验证推理。",
            1.0,
        )


def prepare_cover(root):
    """Fetch singing assets and record file integrity without running inference."""
    seed = root / "seed-vc"
    required = (
        "inference.py",
        "hf_utils.py",
        "modules/commons.py",
        "modules/rmvpe.py",
        "configs/presets/config_dit_mel_seed_uvit_whisper_base_f0_44k.yml",
    )
    if not all((seed / name).is_file() for name in required):
        report("下载翻唱执行代码", 4)
        archive = BUNDLED / "seed-vc.zip"
        manifest = BUNDLED / "seed-vc.json"
        valid_bundle = False
        if archive.is_file() and manifest.is_file():
            try:
                info = json.loads(manifest.read_text(encoding="utf-8"))
                valid_bundle = (
                    info.get("revision") == SEED_REVISION
                    and info.get("upstream_sha256") == SEED_ARCHIVE_SHA
                    and info.get("sha256") == digest(archive)
                )
            except (ValueError, OSError):
                pass
        if valid_bundle:
            report("补齐翻唱执行代码", 4, "使用插件自带的已校验源码，无需连接GitHub。")
        else:
            archive = download(
                f"https://codeload.github.com/Plachtaa/seed-vc/zip/{SEED_REVISION}",
                root / "tmp/seed-vc.zip",
                "下载翻唱执行代码",
                4,
                sha=SEED_ARCHIVE_SHA,
            )
        with zipfile.ZipFile(archive) as bundle:
            prefix = f"seed-vc-{SEED_REVISION}/"
            if sum(item.file_size for item in bundle.infolist()) > 256 * 1024**2:
                raise RuntimeError("Seed-VC 源码包大小异常。")
            for item in bundle.infolist():
                if item.is_dir():
                    continue
                if not item.filename.startswith(prefix) or "\\" in item.filename:
                    raise RuntimeError("Seed-VC 源码包路径异常。")
                relative = Path(item.filename[len(prefix) :])
                dest = seed / relative
                if (
                    not dest.resolve().is_relative_to(seed.resolve())
                    or dest.is_symlink()
                ):
                    raise RuntimeError("Seed-VC 源码目标路径异常。")
                if dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                pending = dest.with_name(dest.name + ".installing")
                with bundle.open(item) as src, pending.open("wb") as out:
                    shutil.copyfileobj(src, out)
                os.replace(pending, dest)
        if not all((seed / name).is_file() for name in required):
            raise RuntimeError("Seed-VC 源码包缺少必要文件，请更新插件后继续安装。")
    weights = root / "weights"
    jobs = [
        (
            "Plachta/Seed-VC",
            "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth",
            "weights/svc.pth",
            820865494,
            "42aef93ffe65857c840d270252fa040f7ba04514945ec460f3ac1ac2a96de684",
        ),
        (
            "lj1995/VoiceConversionWebUI",
            "rmvpe.pt",
            "weights/rmvpe.pt",
            181184272,
            "6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193",
        ),
        (
            "nvidia/bigvgan_v2_44khz_128band_512x",
            "bigvgan_generator.pt",
            "weights/bigvgan/bigvgan_generator.pt",
            489041291,
            "d9fe7ec6bd0b44ed9d66973d5012d8181c1570b01e5c72df51973e241dccd357",
        ),
        (
            "openai/whisper-small",
            "model.safetensors",
            "weights/whisper/model.safetensors",
            966995080,
            "1d7734884874f1a1513ed9aa760a4f8e97aaa02fd6d93a3a85d27b2ae9ca596b",
        ),
        (
            "funasr/campplus",
            "campplus_cn_common.bin",
            "seed-vc/campplus_cn_common.bin",
            28036335,
            "3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8",
        ),
    ]
    models = []
    for i, (repo, name, relative, size, sha) in enumerate(jobs, 1):
        path = download(
            HF + "/" + repo + "/resolve/main/" + name,
            root / relative,
            f"下载翻唱模型 {i}/{len(jobs)}",
            4,
            size,
            sha,
        )
        models.append(dict(name=path.name, path=relative, size=size, sha256=sha))
    configs = [
        (
            "nvidia/bigvgan_v2_44khz_128band_512x",
            "config.json",
            weights / "bigvgan/config.json",
        )
    ]
    configs += [
        ("openai/whisper-small", name, weights / "whisper" / name)
        for name in (
            "config.json",
            "preprocessor_config.json",
            "generation_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "added_tokens.json",
            "normalizer.json",
            "merges.txt",
            "vocab.json",
        )
    ]
    for repo, name, path in configs:
        download(HF + "/" + repo + "/resolve/main/" + name, path, "下载翻唱模型配置", 4)
        models.append(
            dict(
                name=path.name,
                path=path.relative_to(root).as_posix(),
                size=path.stat().st_size,
                sha256=digest(path),
            )
        )
    demucs = root / "cache/torch/hub/checkpoints/955717e8-8726e21a.th"
    demucs_sha = "8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4"
    try:
        download(
            (HF + "/deepvoice/uvr/resolve/main/955717e8-8726e21a.th")
            if MODEL_SOURCE != "auto"
            else "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th",
            demucs,
            "下载人声分离模型",
            4,
            84141911,
            demucs_sha,
        )
    except RuntimeError:
        if MODEL_SOURCE != "auto":
            raise
        report(
            "切换人声分离模型来源",
            4,
            "上游下载未完成，尝试模型仓库副本；使用同一个完整SHA-256校验。",
        )
        download(
            HF + "/deepvoice/uvr/resolve/main/955717e8-8726e21a.th",
            demucs,
            "下载人声分离模型副本",
            4,
            84141911,
            demucs_sha,
        )
    report("整理离线模型配置", 5)
    config = weights / "svc_config.yml"
    if not config.is_file():
        content = (
            seed / "configs/presets/config_dit_mel_seed_uvit_whisper_base_f0_44k.yml"
        ).read_text(encoding="utf-8")
        content = content.replace(
            "nvidia/bigvgan_v2_44khz_128band_512x", (weights / "bigvgan").as_posix()
        )
        content = content.replace(
            "openai/whisper-small", (weights / "whisper").as_posix()
        )
        pending_config = config.with_suffix(".yml.installing")
        pending_config.write_text(content, encoding="utf-8")
        os.replace(pending_config, config)
    models.append(
        dict(
            name=config.name,
            path="weights/svc_config.yml",
            size=config.stat().st_size,
            sha256=digest(config),
        )
    )
    helper = seed / "hf_utils.py"
    text = helper.read_text(encoding="utf-8")
    if (
        '"rmvpe.pt"): weights / "rmvpe.pt"' not in text
        and "# Breeze local assets" not in text
    ):
        anchor = 'def load_custom_model_from_hf(repo_id, model_filename="pytorch_model.bin", config_filename=None):'
        if anchor not in text:
            raise RuntimeError("Seed-VC 下载器版本不兼容，未改写现有源码。")
        insert = """
    # Breeze local assets must remain usable with offline inference.
    from pathlib import Path
    base = Path(__file__).resolve().parent
    local = {
        ("lj1995/VoiceConversionWebUI", "rmvpe.pt"): base.parent / "weights/rmvpe.pt",
        ("funasr/campplus", "campplus_cn_common.bin"): base / "campplus_cn_common.bin",
    }.get((repo_id, model_filename))
    if local is not None and config_filename is None:
        if not local.is_file():
            raise FileNotFoundError(str(local))
        return str(local)
"""
        pending_helper = helper.with_suffix(".py.installing")
        pending_helper.write_text(
            text.replace(anchor, anchor + insert, 1), encoding="utf-8"
        )
        os.replace(pending_helper, helper)
    marker = root / "seed-ready.json"
    try:
        previous = (
            json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
        )
        if not isinstance(previous, dict):
            previous = {}
    except (ValueError, OSError):
        previous = {}
    if previous.get("models") != models:
        pending = marker.with_suffix(".json.installing")
        pending.write_text(
            json.dumps(
                dict(version=1, verification="file_integrity_only", models=models),
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(pending, marker)


if __name__ == "__main__":
    try:
        install(
            sys.argv[1],
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            json.loads(sys.argv[4]) if len(sys.argv) > 4 else None,
        )
    except Exception as exc:
        report("安装失败", 0, str(exc)[:2000])
        print(str(exc), flush=True)
        raise SystemExit(1)
