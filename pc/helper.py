"""Starts the control helper (WebDriverAgent) on the iPhone, over USB.

WebDriverAgent (WDA) is what actually performs taps and typing on the phone. It
has to be launched by a computer with Apple's developer tools mounted on the
phone. We do that with pymobiledevice3:

  1. mounter auto-mount     -> mount the developer disk image (needed by XCTest)
  2. apps list              -> find the installed WDA app (its ID can vary)
  3. developer dvt launch   -> start it with USE_IP=127.0.0.1 (listen only inside
                               the phone) and USE_PORT=8100

Once started, WDA keeps running on the phone, so control also works over Wi-Fi
afterwards (until the phone restarts).

pymobiledevice3 doesn't install on Python 3.15 yet (some dependencies aren't
built for it), so we run it with `uv` on its own Python 3.13, which uv downloads
automatically. Nothing else on the PC is changed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PMD_VERSION = "11.55.0"
HERE = Path(__file__).resolve().parent
LOG = HERE / "helper.log"


def _log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def _uv() -> str | None:
    exe = "uv.exe" if sys.platform == "win32" else "uv"
    local = Path(sys.executable).parent / exe          # inside the viewer's .venv
    if local.exists():
        return str(local)
    return shutil.which("uv")


class ControlHelper:
    """Runs the launch steps in a background thread and exposes a status line."""

    def __init__(self) -> None:
        self.status = ""
        self.busy = False
        self.last_attempt = 0.0
        self._lock = threading.Lock()

    def start(self, reason: str = "") -> bool:
        """Kick off a launch attempt unless one is already running."""
        with self._lock:
            if self.busy:
                return False
            self.busy = True
            self.last_attempt = time.time()
        threading.Thread(target=self._run, args=(reason,), daemon=True).start()
        return True

    # -------------------------------------------------------------------------

    def _pmd(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
        uv = _uv()
        cmd = [uv, "tool", "run", "--quiet", "--python", "3.13",
               "--from", f"pymobiledevice3=={PMD_VERSION}", "pymobiledevice3", *args]
        _log("$ " + " ".join(cmd[8:]))
        env = dict(os.environ, PYTHONIOENCODING="utf-8", NO_COLOR="1")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, creationflags=flags)
        _log(f"  exit {p.returncode}\n  out: {p.stdout[-2000:]}\n  err: {p.stderr[-3000:]}")
        return p

    @staticmethod
    def _short_error(p: subprocess.CompletedProcess) -> str:
        lines = [l.strip() for l in (p.stderr or p.stdout or "").splitlines() if l.strip()]
        for l in reversed(lines):
            if "Error" in l or "error" in l or "Exception" in l:
                return l[:160]
        return (lines[-1][:160] if lines else f"exit code {p.returncode}")

    def _run(self, reason: str) -> None:
        try:
            _log(f"--- launch attempt ({reason})")
            if _uv() is None:
                self.status = "Control: installing helper tools (one time)…"
                subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"],
                               capture_output=True, timeout=300)
                if _uv() is None:
                    self.status = "Control: couldn't install 'uv' (see pc/helper.log)"
                    return

            self.status = "Control: preparing developer tools on the iPhone (first time: a few minutes)…"
            p = self._pmd("mounter", "auto-mount", timeout=600)
            if p.returncode != 0:
                self.status = "Control: couldn't mount developer tools — " + self._short_error(p)
                return

            self.status = "Control: looking for the helper app on the iPhone…"
            p = self._pmd("apps", "list", "--type", "User", timeout=120)
            bundle = None
            if p.returncode == 0:
                try:
                    apps = json.loads(p.stdout[p.stdout.index("{"):])
                    for bid in apps:
                        if "webdriveragent" in bid.lower():
                            bundle = bid
                            # Prefer the runner (".xctrunner") if both are listed.
                            if bid.lower().endswith(".xctrunner"):
                                break
                except (ValueError, KeyError):
                    pass
            if bundle is None:
                self.status = ("Control: helper app not found on the iPhone — install "
                               "WebDriverAgent-unsigned.ipa with Sideloadly")
                return

            self.status = "Control: starting the helper…"
            p = self._pmd("developer", "dvt", "launch",
                          "--env", "USE_PORT=8100", "--env", "USE_IP=127.0.0.1",
                          bundle, timeout=180)
            if p.returncode != 0:
                err = self._short_error(p)
                if "trust" in err.lower() or "profile" in err.lower():
                    err += " (Settings → General → VPN & Device Management → trust your Apple ID)"
                self.status = "Control: couldn't start the helper — " + err
                return
            self.status = "Control: helper started, connecting…"
        except subprocess.TimeoutExpired:
            self.status = "Control: a setup step timed out (see pc/helper.log)"
        except Exception as e:  # never crash the viewer
            _log(f"exception: {e!r}")
            self.status = f"Control: setup failed ({e.__class__.__name__}, see pc/helper.log)"
        finally:
            self.busy = False
