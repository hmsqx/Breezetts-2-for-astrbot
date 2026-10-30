"""Refresh the small backend source payload shipped with this local plugin."""

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

from breeze.services.installer import SEED_ARCHIVE_SHA, SEED_REVISION  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed_archive", type=Path, nargs="?")
    parser.add_argument("--cover-source", type=Path, default=PLUGIN / "runtime/cover")
    parser.add_argument("--tts-source", type=Path, default=PLUGIN / "runtime/tts")
    args = parser.parse_args()
    plugin = PLUGIN
    cover = args.cover_source.resolve()
    tts = args.tts_source.resolve()
    sources = [
        (path, "tts", path.relative_to(tts))
        for folder in ("breeze_infer", "models")
        for path in (tts / folder).rglob("*.py")
    ]
    sources += [
        (tts / name, "tts", Path(name)) for name in ("configs/fast.json", "LICENSE")
    ]
    sources += [
        (cover / name, "cover", Path(name))
        for name in (
            "server.py",
            "public_proxy.py",
            "transcribe_song.py",
            "run_demucs.py",
        )
    ]
    sources += [
        (path, "cover", path.relative_to(cover))
        for path in (cover / "yt_dlp_plugins").rglob("*.py")
    ]
    assert sources and all(path.is_file() for path, _, _ in sources)
    for source, kind, relative in sources:
        target = plugin / "runtime" / kind / relative
        assert target.resolve().is_relative_to((plugin / "runtime").resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
    if args.seed_archive is not None:
        upstream = args.seed_archive.resolve()
        assert hashlib.sha256(upstream.read_bytes()).hexdigest() == SEED_ARCHIVE_SHA
        target = plugin / "runtime/seed-vc.zip"
        with (
            zipfile.ZipFile(upstream) as src,
            zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dest,
        ):
            for item in src.infolist():
                if item.is_dir():
                    continue
                if (
                    Path(item.filename).suffix not in {".py", ".yml", ".yaml", ".json"}
                    and "license" not in item.filename.lower()
                ):
                    continue
                assert item.filename.startswith(f"seed-vc-{SEED_REVISION}/")
                dest.writestr(item.filename, src.read(item))
        (plugin / "runtime/seed-vc.json").write_text(
            json.dumps(
                {
                    "revision": SEED_REVISION,
                    "upstream_sha256": SEED_ARCHIVE_SHA,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            f"Bundled pinned Seed-VC code and licenses: {target.stat().st_size} bytes; no GitHub request at install time."
        )
    print(
        f"Bundled {len(sources)} source files; no models, audio, credentials or environments."
    )
