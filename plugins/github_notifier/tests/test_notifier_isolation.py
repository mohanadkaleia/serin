"""Structural guarantees (ENG-162): out-of-process, stdlib-only, bootable.

The reference plugin proves the public API (docs/plugins.md) is self-sufficient
only if it consumes NOTHING but HTTP — these tests make that a gate, not a
convention:

* a static AST sweep over every module in the package: no import of ``msgd``,
  ``msgctl``, ``server``, ``cli``, or any HTTP framework/client library;
* a fresh-interpreter import: loading the whole package pulls no msg (or
  third-party) modules into ``sys.modules``;
* a subprocess boot: ``python -m github_notifier`` fails fast (exit 2, the
  variable named on stderr) without config, and with config starts, prints its
  bound address, and exits 0 on SIGTERM — the contract the M5 exit gate's
  supervisor relies on.
"""

from __future__ import annotations

import ast
import signal
import subprocess
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "github_notifier"

#: Top-level module names the plugin must never import: the msg packages
#: (in-process coupling would unprove D12) and the frameworks/clients whose
#: absence keeps this a zero-dependency stdlib package.
FORBIDDEN_IMPORTS = frozenset(
    {"msgd", "msgctl", "server", "cli", "tests", "fastapi", "sqlalchemy", "httpx", "pydantic"}
)


def _imported_top_level_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_package_source_imports_nothing_from_msg() -> None:
    sources = sorted(PACKAGE_DIR.rglob("*.py"))
    assert sources, f"no sources found under {PACKAGE_DIR}"
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        forbidden = FORBIDDEN_IMPORTS & _imported_top_level_names(tree)
        assert not forbidden, f"{source} imports forbidden module(s): {sorted(forbidden)}"


def test_importing_the_package_loads_no_msg_modules() -> None:
    """Fresh interpreter: import every module, then audit sys.modules."""
    probe = (
        "import sys\n"
        "import github_notifier, github_notifier.config, github_notifier.dedupe\n"
        "import github_notifier.formatting, github_notifier.notifier\n"
        "import github_notifier.server, github_notifier.signature\n"
        f"forbidden = {sorted(FORBIDDEN_IMPORTS)!r}\n"
        "loaded = {name.split('.')[0] for name in sys.modules}\n"
        "hit = sorted(set(forbidden) & loaded)\n"
        "assert not hit, f'forbidden modules loaded: {hit}'\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, timeout=60)


def test_main_fails_fast_without_config() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "github_notifier"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin"},  # deliberately no GITHUB_WEBHOOK_SECRET etc.
    )
    assert result.returncode == 2
    assert "GITHUB_WEBHOOK_SECRET" in result.stderr


def test_main_boots_and_stops_cleanly() -> None:
    """The M5-5 supervisor contract: start, announce the port, exit 0 on SIGTERM."""
    process = subprocess.Popen(
        [sys.executable, "-m", "github_notifier"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_WEBHOOK_SECRET": "boot-test-secret",
            "MSG_HOOK_URL": "http://127.0.0.1:9/v1/hooks/never-called",
            "GITHUB_NOTIFIER_PORT": "0",  # ephemeral — never collides in CI
        },
    )
    try:
        assert process.stdout is not None
        banner = process.stdout.readline()
        assert "listening on 127.0.0.1:" in banner
        process.send_signal(signal.SIGTERM)
        returncode = process.wait(timeout=15)
        assert returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)
