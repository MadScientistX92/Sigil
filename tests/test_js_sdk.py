"""Runs the JS/TS SDK (sdk/js) against a live server: unit tests with a fake fetch, then an end-to-end script.

Skipped when node or tsc is not installed."""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from test_sigil import _img

SDK = Path(__file__).resolve().parent.parent / "sdk" / "js"
pytestmark = pytest.mark.skipif(not (shutil.which("node") and (shutil.which("tsc") or (SDK / "node_modules").exists())),
                                reason="node + typescript needed for the JS SDK tests")


@pytest.fixture(scope="module")
def built():
    tsc = [str(SDK / "node_modules" / ".bin" / "tsc")] if (SDK / "node_modules").exists() else ["tsc"]
    r = subprocess.run(tsc + ["-p", "tsconfig.json"], cwd=SDK, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return SDK


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    port = _free_port()
    env = os.environ | {"SIGIL_DATA": str(tmp_path), "SIGIL_API_KEYS": "dev:acme", "SIGIL_VERIFY_RATE_ANON": "5",
                        "SIGIL_PUBLIC_URL": f"http://127.0.0.1:{port}"}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "sigil.api:app", "--port", str(port), "--log-level",
                          "warning"], env=env, cwd=SDK.parent.parent)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        p.kill()
        pytest.fail("server did not start")
    yield url
    p.terminate()
    p.wait(5)


def test_js_sdk_unit(built):
    files = sorted(str(f) for f in (built / "test").glob("*.test.mjs"))
    r = subprocess.run(["node", "--test", *files], cwd=built, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_js_sdk_end_to_end(built, server, tmp_path):
    png = tmp_path / "gen.png"
    png.write_bytes(_img())
    r = subprocess.run(["node", "test/e2e.mjs", server, "dev", str(png)], cwd=built, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "e2e ok" in r.stdout
