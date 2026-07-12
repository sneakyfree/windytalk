"""Drift-catchers for the fat-installer cocktail manifests.

The doctrine (docs/PACKAGING.md): the installer ships EVERYTHING the code could
possibly need. These tests make that structurally true rather than promised —
if anyone adds an external tool or a third-party import to the shipped code
without adding it to packaging/manifests/, the merge gate goes red.

Scanning is done on SOURCE (regex over the backends / import statements), so the
manifest tracks what the code actually invokes, not what someone remembered.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = ROOT / "packaging" / "manifests"

# Python packages/dirs that ship in the product payload (mirrors scripts/ci.sh
# ruff targets minus tests/).
SHIPPED_PY_DIRS = ("hands", "engine", "brains", "server", "auth", "telemetry",
                   "wakeword", "agents")
FIRST_PARTY = set(SHIPPED_PY_DIRS) | {"contracts", "apps"}

BACKEND_FOR_OS = {"linux": "linux.py", "macos": "macos.py", "windows": "windows.py"}
PROVIDED_BY = {"bundled", "os-builtin", "system-extra"}

# --- source scanners ---------------------------------------------------------

# shutil.which("tool") / _which("tool") — the house style for availability probes.
WHICH_RE = re.compile(r'(?:_which|shutil\.which)\(\s*"([A-Za-z0-9._+-]+)"')
# First element of a literal argv list: ["grim", ...] / ["open", "-a", ...].
# Lowercase-only on purpose: SendKeys tokens like "{ESC}" and flag strings never match.
CMDLIST_RE = re.compile(r'\[\s*"([a-z][a-z0-9._+-]*)"\s*,')
# Windows interpreter candidates tuple.
PSBIN_RE = re.compile(r"_PS_BINARIES\s*=\s*\(([^)]*)\)")

IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)


def _load(name: str) -> dict:
    return json.loads((MANIFEST_DIR / name).read_text())


def _scanned_tools(os_name: str) -> set[str]:
    src = (ROOT / "hands" / "backends" / BACKEND_FOR_OS[os_name]).read_text()
    tools = set(WHICH_RE.findall(src)) | set(CMDLIST_RE.findall(src))
    m = PSBIN_RE.search(src)
    if m:
        tools |= set(re.findall(r'"([^"]+)"', m.group(1)))
    return tools


def _third_party_imports() -> set[str]:
    found: set[str] = set()
    for d in SHIPPED_PY_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if "tests" in p.parts or p.name.startswith("test_"):
                continue
            found |= set(IMPORT_RE.findall(p.read_text()))
    return {m for m in found
            if m not in sys.stdlib_module_names and m not in FIRST_PARTY}


# --- the drift tests ---------------------------------------------------------

def test_every_invoked_tool_is_in_its_cocktail():
    """Every external binary a backend can invoke must appear in that OS's
    manifest. Add a tool to the code without adding it to the cocktail -> red."""
    for os_name in BACKEND_FOR_OS:
        manifest = _load(f"{os_name}.json")
        declared = set(manifest["external_tools"])
        ignore = set(manifest.get("scanner_ignore", []))
        missing = _scanned_tools(os_name) - declared - ignore
        assert not missing, (
            f"{os_name}: backend invokes tools missing from "
            f"packaging/manifests/{os_name}.json: {sorted(missing)}"
        )


def test_every_third_party_import_is_declared():
    """Every third-party python import in shipped code must be a declared
    package (common core/engine-local, or an OS python_extra)."""
    declared = {e["import_name"] for e in _load("common.json")["python_packages"]}
    for os_name in BACKEND_FOR_OS:
        declared |= {e["import_name"] for e in _load(f"{os_name}.json").get("python_extra", [])}
    missing = _third_party_imports() - declared
    assert not missing, (
        f"third-party imports missing from the cocktail manifests: {sorted(missing)}"
    )


def test_manifest_schema_sanity():
    """Each OS manifest declares artifacts, a floor, and a sane provided_by for
    every tool; nothing marked bundled may be a phantom (empty role)."""
    for os_name in BACKEND_FOR_OS:
        manifest = _load(f"{os_name}.json")
        assert manifest["artifacts"], f"{os_name}: no artifacts declared"
        assert manifest["floor"]["os"], f"{os_name}: no OS floor declared"
        for tool, spec in manifest["external_tools"].items():
            assert spec.get("provided_by") in PROVIDED_BY, (
                f"{os_name}:{tool}: provided_by must be one of {sorted(PROVIDED_BY)}"
            )
            assert spec.get("role"), f"{os_name}:{tool}: missing role"


def test_bundled_python_satisfies_pyproject_floor():
    """The frozen python we ship must satisfy pyproject's requires-python."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    floor = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', pyproject)
    assert floor, "pyproject.toml requires-python not found/parseable"
    bundled = _load("common.json")["runtimes"]["python"]["version"]
    major, minor = (int(x) for x in bundled.split(".")[:2])
    assert (major, minor) >= (int(floor.group(1)), int(floor.group(2))), (
        f"bundled python {bundled} is below pyproject floor "
        f"{floor.group(1)}.{floor.group(2)}"
    )
