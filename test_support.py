"""Import the real plugin modules with an isolated AstrBot runtime for tests."""

import importlib
import os
import sys
import tempfile
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent
CORE = os.environ.get("ASTRBOT_SOURCE")
_PACKAGE = "breeze_plugin_tests"
_temporary_root = None


def load_module(name):
    """Load each business module once, without reading real AstrBot user data."""
    global _temporary_root
    if _PACKAGE not in sys.modules:
        temporary_parent = PLUGIN.parent / "tmp"
        temporary_parent.mkdir(exist_ok=True)
        _temporary_root = tempfile.TemporaryDirectory(
            prefix="plugin-test-", dir=temporary_parent, ignore_cleanup_errors=True
        )
        runtime = Path(_temporary_root.name)
        (runtime / "data/config").mkdir(parents=True)
        os.environ["ASTRBOT_ROOT"] = str(runtime)
        if CORE:
            sys.path.insert(0, str(Path(CORE).resolve()))
        package = types.ModuleType(_PACKAGE)
        package.__path__ = [str(PLUGIN)]
        sys.modules[_PACKAGE] = package
    return importlib.import_module(f"{_PACKAGE}.{name}")


def load_plugin():
    """Return the cached real framework entry module in this test process."""
    return load_module("main")
