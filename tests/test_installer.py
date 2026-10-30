"""Exercise installation recovery using local fixtures, without downloading models."""

import asyncio
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

spec = importlib.util.spec_from_file_location(
    "tested_installer",
    Path(__file__).resolve().parents[1] / "breeze/services/installer.py",
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Reply(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(data))}


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.report = patch.object(installer, "report").start()
        self.addCleanup(patch.stopall)

    def test_retry_resumes_a_known_size_without_overwriting_good_file(self):
        destination = self.root / "weights.bin"
        data = b"verified weight fixture"
        destination.write_bytes(b"old working file")
        partial = destination.with_name(destination.name + ".download")
        urls = []

        def fetch(request, **kwargs):
            urls.append(request.full_url)
            self.assertEqual(destination.read_bytes(), b"old working file")
            if len(urls) == 1:
                partial.write_bytes(data[:5])
                raise URLError("connection reset")
            self.assertEqual(request.get_header("Range"), "bytes=5-")
            return Reply(
                data[5:], 206, {"Content-Range": f"bytes 5-{len(data) - 1}/{len(data)}"}
            )

        with (
            patch.object(installer, "urlopen", fetch),
            patch.object(installer.time, "sleep"),
        ):
            installer.download(
                "https://huggingface.co/repo/weight",
                destination,
                "test",
                4,
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
        self.assertIn("hf-mirror.com", urls[1])
        self.assertEqual(destination.read_bytes(), data)
        self.assertFalse(partial.exists())

    def test_server_ignoring_range_restarts_without_appending(self):
        destination = self.root / "weight.bin"
        destination.with_name("weight.bin.download").write_bytes(b"partial")
        data = b"complete model fixture"
        with patch.object(installer, "urlopen", return_value=Reply(data)):
            installer.download(
                "https://example.invalid/model",
                destination,
                "test",
                4,
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
        self.assertEqual(destination.read_bytes(), data)

    def test_corrupt_json_and_complete_partial_are_not_reused(self):
        destination = self.root / "config.json"
        destination.write_bytes(b"not-json")
        partial = destination.with_name("config.json.download")
        partial.write_bytes(b"not-json")
        data = b'{"ok":1}'
        with patch.object(installer, "urlopen", return_value=Reply(data)) as fetch:
            installer.download(
                "https://example.invalid/config", destination, "test", 4, len(data)
            )
        fetch.assert_called_once()
        self.assertEqual(json.loads(destination.read_text()), {"ok": 1})

    def test_invalid_hash_never_replaces_existing_file(self):
        destination = self.root / "weight.bin"
        destination.write_bytes(b"keep")
        with (
            patch.object(
                installer, "urlopen", side_effect=lambda *a, **k: Reply(b"wrong")
            ),
            patch.object(installer.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "3次"):
                installer.download(
                    "https://example.invalid/weight",
                    destination,
                    "test",
                    4,
                    5,
                    "0" * 64,
                )
        self.assertEqual(destination.read_bytes(), b"keep")

    def test_verified_existing_file_does_not_use_network(self):
        destination = self.root / "weight.bin"
        data = b"already installed"
        destination.write_bytes(data)
        with patch.object(
            installer, "urlopen", side_effect=AssertionError("unexpected network")
        ):
            installer.download(
                "https://example.invalid/weight",
                destination,
                "test",
                4,
                len(data),
                hashlib.sha256(data).hexdigest(),
            )

    def test_missing_upstream_file_fails_without_pointless_retries(self):
        with patch.object(
            installer, "urlopen", side_effect=HTTPError("url", 404, "missing", {}, None)
        ) as fetch:
            with self.assertRaisesRegex(RuntimeError, "404"):
                installer.download(
                    "https://example.invalid/missing",
                    self.root / "weight.bin",
                    "test",
                    4,
                )
        fetch.assert_called_once()

    def test_partial_seed_source_is_repaired_without_git_or_custom_file_loss(self):
        seed = self.root / "seed-vc"
        seed.mkdir()
        (seed / "custom.py").write_text("custom = True")
        archive = self.root / "seed.zip"
        names = (
            "inference.py",
            "hf_utils.py",
            "modules/commons.py",
            "modules/rmvpe.py",
            "configs/presets/config_dit_mel_seed_uvit_whisper_base_f0_44k.yml",
        )
        with zipfile.ZipFile(archive, "w") as bundle:
            for name in names:
                bundle.writestr(f"seed-vc-{installer.SEED_REVISION}/{name}", "fixture")

        def fetch(url, *args, **kwargs):
            if "codeload.github.com" in url:
                return archive
            raise InterruptedError("stop before any model downloads")

        with (
            patch.object(installer, "download", fetch),
            patch.object(installer, "BUNDLED", self.root / "no-bundle"),
            patch.object(
                installer, "command", side_effect=AssertionError("Git must not run")
            ),
        ):
            with self.assertRaises(InterruptedError):
                installer.prepare_cover(self.root)
        self.assertTrue(all((seed / name).is_file() for name in names))
        self.assertEqual((seed / "custom.py").read_text(), "custom = True")

    def test_seed_archive_path_traversal_is_rejected(self):
        archive = self.root / "bad.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(f"seed-vc-{installer.SEED_REVISION}/../../escape.py", "bad")
        with patch.object(installer, "download", return_value=archive):
            with patch.object(installer, "BUNDLED", self.root / "no-bundle"):
                with self.assertRaisesRegex(RuntimeError, "路径异常"):
                    installer.prepare_cover(self.root)
        self.assertFalse((self.root.parent / "escape.py").exists())

    def test_clean_cover_install_copies_sources_and_checks_real_imports(self):
        tts, cover = self.root / "breeze-tts", self.root / "breeze-cover"
        calls = []

        def command(args, cwd):
            values = [str(value) for value in args]
            calls.append(values)
            if "venv" in values:
                executable = Path(values[-1]) / "Scripts/python.exe"
                executable.parent.mkdir(parents=True)
                executable.touch()

        with (
            patch.object(installer, "command", command),
            patch.object(
                installer.shutil,
                "disk_usage",
                return_value=types.SimpleNamespace(free=100 * 1024**3),
            ),
            patch.object(installer.shutil, "which", return_value="fixture.exe"),
            patch.object(installer, "prepare_cover"),
            patch.object(
                installer.subprocess,
                "run",
                return_value=types.SimpleNamespace(returncode=0),
            ) as run,
        ):
            installer.install("cover", tts, cover)
        self.assertTrue((cover / "server.py").is_file())
        self.assertTrue(
            (cover / "yt_dlp_plugins/extractor/breeze_bilibili.py").is_file()
        )
        self.assertFalse(any("--system-site-packages" in values for values in calls))
        self.assertTrue(any("torch==2.9.1+cu128" in values for values in calls))
        probe = run.call_args_list[0]
        self.assertIn("import inference", probe.args[0][-1])
        self.assertEqual(probe.kwargs["env"]["HF_HUB_OFFLINE"], "1")
        self.assertEqual(probe.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "")

    def test_import_failure_is_not_reported_as_install_success(self):
        tts, cover = self.root / "breeze-tts", self.root / "breeze-cover"
        with (
            patch.object(installer, "command"),
            patch.object(
                installer.shutil,
                "disk_usage",
                return_value=types.SimpleNamespace(free=100 * 1024**3),
            ),
            patch.object(installer.shutil, "which", return_value="fixture.exe"),
            patch.object(installer, "prepare_cover"),
            patch.object(
                installer.subprocess,
                "run",
                return_value=types.SimpleNamespace(returncode=1),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "导入检查失败"):
                installer.install("cover", tts, cover)
        self.assertFalse(
            any(call.args[0] == "安装检查通过" for call in self.report.call_args_list)
        )

    def test_install_reuses_existing_source_without_overwriting_customizations(self):
        tts, cover = self.root / "breeze-tts", self.root / "breeze-cover"
        cover.mkdir()
        (cover / "server.py").write_text("existing customization")
        with (
            patch.object(installer, "command"),
            patch.object(
                installer.shutil,
                "disk_usage",
                return_value=types.SimpleNamespace(free=100 * 1024**3),
            ),
            patch.object(installer.shutil, "which", return_value="fixture.exe"),
            patch.object(installer, "prepare_cover"),
            patch.object(
                installer.subprocess,
                "run",
                return_value=types.SimpleNamespace(returncode=0),
            ),
        ):
            installer.install("cover", tts, cover)
        self.assertEqual((cover / "server.py").read_text(), "existing customization")

    def test_clean_tts_install_checks_shards_and_imports(self):
        tts, cover = self.root / "breeze-tts", self.root / "breeze-cover"
        assets = {
            "config.json": b"{}",
            "model.safetensors.index.json": b'{"weight_map":{"weight":"model-00001.safetensors"}}',
            "tokenizer.json": b"{}",
            "audio_tokenizer/config.json": b"{}",
            "audio_tokenizer/model.safetensors": b"audio fixture",
            "model-00001.safetensors": b"model fixture",
        }

        def command(args, cwd):
            if "list_repo_tree" in str(args[2] if len(args) > 2 else ""):
                Path(args[3]).write_text(
                    json.dumps(
                        [
                            {"type": "file", "path": name, "size": len(data)}
                            for name, data in assets.items()
                        ]
                    ),
                    encoding="utf-8",
                )

        def download(url, destination, *args):
            name = url.split("/resolve/main/", 1)[1]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(assets[name])
            return destination

        with (
            patch.object(installer, "command", command),
            patch.object(installer, "download", download),
            patch.object(
                installer.shutil,
                "disk_usage",
                return_value=types.SimpleNamespace(free=100 * 1024**3),
            ),
            patch.object(installer.shutil, "which", return_value="fixture.exe"),
            patch.object(
                installer.subprocess,
                "run",
                return_value=types.SimpleNamespace(returncode=0),
            ) as run,
        ):
            installer.install("tts", tts, cover)
        self.assertTrue((tts / "models/breeze.py").is_file())
        self.assertTrue((self.root / "breeze-tts-2/model-00001.safetensors").is_file())
        self.assertIn("import breeze_infer.api", run.call_args_list[0].args[0][-1])

    def test_packaged_seed_source_needs_no_github_connection(self):
        def fetch(url, *args, **kwargs):
            self.assertNotIn("github", url)
            raise InterruptedError("stop before model downloads")

        with patch.object(installer, "download", fetch):
            with self.assertRaises(InterruptedError):
                installer.prepare_cover(self.root)
        self.assertTrue((self.root / "seed-vc/inference.py").is_file())
        self.assertTrue((self.root / "seed-vc/modules/bigvgan/bigvgan.py").is_file())

    def test_pypi_source_failure_switches_without_changing_requirement(self):
        with patch.object(
            installer,
            "command",
            side_effect=[RuntimeError("network unavailable"), None],
        ) as run:
            installer.pip_install("python.exe", ["transformers==4.57.3"], self.root)
        self.assertEqual(run.call_count, 2)
        first, second = (call.args[0] for call in run.call_args_list)
        self.assertIn("https://pypi.tuna.tsinghua.edu.cn/simple", first)
        self.assertIn("https://mirrors.aliyun.com/pypi/simple/", second)
        self.assertEqual(first[-1], second[-1])

    def test_cuda_source_fallback_preserves_cuda_build(self):
        with patch.object(
            installer,
            "command",
            side_effect=[
                RuntimeError("network unavailable"),
                RuntimeError("mirror unavailable"),
                None,
            ],
        ) as run:
            installer.pip_install(
                "python.exe",
                ["torch==2.9.1+cu128", "torchaudio==2.9.1+cu128"],
                self.root,
                cuda=True,
            )
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            self.assertIn("torch==2.9.1+cu128", call.args[0])
            self.assertIn("torchaudio==2.9.1+cu128", call.args[0])
        self.assertIn("--find-links", run.call_args_list[0].args[0])
        self.assertIn(
            "https://download.pytorch.org/whl/cu128", run.call_args_list[-1].args[0]
        )

    def test_all_pip_sources_failing_is_not_success(self):
        with patch.object(
            installer, "command", side_effect=RuntimeError("no network")
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "3个依赖源"):
                installer.pip_install("python.exe", ["transformers==4.57.3"], self.root)
        self.assertEqual(run.call_count, 3)

    def test_pip_source_settings_do_not_leak_from_global_environment(self):
        from unittest.mock import Mock

        process = Mock()
        process.wait.return_value = 0
        with (
            patch.dict(
                installer.os.environ,
                {"PIP_EXTRA_INDEX_URL": "https://broken.invalid", "PIP_NO_INDEX": "1"},
            ),
            patch.object(installer.subprocess, "Popen", return_value=process) as spawn,
        ):
            installer.command(
                ["python.exe", "-m", "pip", "install", "example"], self.root
            )
        env = spawn.call_args.kwargs["env"]
        self.assertNotIn("PIP_EXTRA_INDEX_URL", env)
        self.assertNotIn("PIP_NO_INDEX", env)
        self.assertEqual(env["PIP_CONFIG_FILE"], installer.os.devnull)

    def test_ffmpeg_download_failure_uses_the_other_build_provider(self):
        tts, cover = self.root / "breeze-tts", self.root / "breeze-cover"
        archive = self.root / "ffmpeg.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                bundle.writestr("fixture/bin/" + name, b"MZ fixture")
        urls = []

        def download(url, *args, **kwargs):
            urls.append(url)
            if "gyan.dev" in url:
                raise RuntimeError("unreachable")
            return archive

        with (
            patch.object(installer, "command"),
            patch.object(
                installer.shutil,
                "disk_usage",
                return_value=types.SimpleNamespace(free=100 * 1024**3),
            ),
            patch.object(
                installer.shutil,
                "which",
                side_effect=lambda name, **kwargs: (
                    "fixture.exe" if "path" in kwargs else None
                ),
            ),
            patch.object(installer, "prepare_cover"),
            patch.object(installer, "download", download),
            patch.object(
                installer.subprocess,
                "run",
                return_value=types.SimpleNamespace(returncode=0),
            ),
        ):
            installer.install("cover", tts, cover)
        self.assertEqual(len(urls), 2)
        self.assertIn("BtbN", urls[1])
        self.assertEqual(
            (self.root / "tools/ffmpeg/bin/ffprobe.exe").read_bytes(), b"MZ fixture"
        )

    def test_demucs_replica_keeps_the_same_full_hash(self):
        seen = []

        def download(url, path, stage, step, size=None, sha=None):
            if "955717e8" in url:
                seen.append((url, size, sha))
                if "fbaipublicfiles.com" in url:
                    raise RuntimeError("unreachable")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}" if path.suffix == ".json" else b"fixture")
            return path

        with patch.object(installer, "download", download):
            installer.prepare_cover(self.root)
        self.assertEqual(len(seen), 2)
        self.assertIn("deepvoice/uvr", seen[1][0])
        self.assertEqual(seen[0][1:], seen[1][1:])
        self.assertEqual(len(seen[0][2]), 64)

    def test_manual_pip_source_does_not_silently_switch(self):
        with patch.object(
            installer, "command", side_effect=RuntimeError("offline")
        ) as run:
            with self.assertRaises(RuntimeError):
                installer.pip_install(
                    "python.exe",
                    ["torch==2.9.1+cu128"],
                    self.root,
                    cuda=True,
                    source="official",
                )
        self.assertEqual(run.call_count, 1)
        self.assertIn("https://download.pytorch.org/whl/cu128", run.call_args.args[0])

    def test_manual_model_source_is_retained_on_every_retry(self):
        with (
            patch.object(installer, "MODEL_SOURCE", "mirror"),
            patch.object(
                installer, "_download_once", side_effect=URLError("offline")
            ) as fetch,
            patch.object(installer.time, "sleep"),
        ):
            with self.assertRaises(RuntimeError):
                installer.download(
                    "https://huggingface.co/example/resolve/main/model",
                    self.root / "model.bin",
                    "test",
                    4,
                )
        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(
            all(
                call.args[0].startswith("https://hf-mirror.com/")
                for call in fetch.call_args_list
            )
        )

    def test_source_validation_and_queued_selection_are_immutable(self):
        for value in (
            [],
            {"python": "https://wrong.invalid"},
            {"cuda": True},
            {"unknown": "auto"},
        ):
            with self.assertRaises(ValueError):
                installer.normalize_sources(value)
        controller = types.SimpleNamespace(root=self.root)
        manager = installer.ServiceInstaller(controller, controller, {})
        selected = {"python": "aliyun", "cuda": "sjtu", "model": "mirror"}
        manager.begin("tts", selected)
        selected["model"] = "official"
        self.assertEqual(manager.states["tts"]["sources"]["model"], "mirror")

    def test_command_timeout_stops_children_before_parent(self):
        from unittest.mock import Mock

        import psutil

        child, parent = Mock(), Mock()
        parent.children.return_value = [child]
        process = Mock(pid=123)
        process.wait.side_effect = subprocess.TimeoutExpired("fixture", 1800)
        with (
            patch.object(installer.subprocess, "Popen", return_value=process),
            patch.object(psutil, "Process", return_value=parent),
            patch.object(psutil, "wait_procs"),
        ):
            with self.assertRaisesRegex(RuntimeError, "30分钟"):
                installer.command(["fixture"], self.root)
        child.kill.assert_called_once()
        parent.kill.assert_called_once()


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_marks_state_and_allows_retry(self):
        controller = types.SimpleNamespace(root=Path(__file__).parent)
        controller._lock = asyncio.Lock()
        waiting = asyncio.Event()

        async def status():
            waiting.set()
            await asyncio.Event().wait()

        controller.status = status
        other = types.SimpleNamespace(_lock=asyncio.Lock())
        manager = installer.ServiceInstaller(controller, other, {})
        manager.begin("tts")
        task = asyncio.create_task(manager.run("tts"))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(manager.busy)
        self.assertEqual(manager.states["tts"]["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
