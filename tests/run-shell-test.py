#!/usr/bin/python3
"""Start an isolated headless GNOME session; never restart the user's Shell."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="eizo-gnome-") as directory:
    runtime = Path(directory) / "runtime"
    runtime.mkdir(mode=0o700)
    environment = dict(os.environ)
    environment.update(
        XDG_RUNTIME_DIR=str(runtime),
        LIBGL_ALWAYS_SOFTWARE="1",
        GIO_USE_VFS="local",
        GTK_USE_PORTAL="0",
        NO_AT_BRIDGE="1",
        EIZO_TEST_SCREENSHOT=str(project / "dist" / "ui-preview.png"),
        XDG_CONFIG_HOME=str(Path(directory) / "config"),
        XDG_DATA_HOME=str(Path(directory) / "data"),
        XDG_CACHE_HOME=str(Path(directory) / "cache"),
        XDG_CONFIG_DIRS="/nonexistent",
        XDG_CURRENT_DESKTOP="GNOME",
    )
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        environment.pop(key, None)
    try:
        process = subprocess.Popen([
            "dbus-run-session", "--", "gnome-shell-test-tool", "--headless",
            "--disable-animations", "--extension",
            str(project / "dist" / "eizo-brightness@local.shell-extension.zip"),
            str(project / "tests" / "shell-smoke.js"),
        ], env=environment, start_new_session=True)
        sys.exit(process.wait(timeout=45))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        sys.exit("The GNOME UI test did not finish within 45 seconds.")
