# Copyright 2026 The Waydroid contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Small helpers for the per-user service model."""
import logging
import shutil
import subprocess


def restart_user_service(args):
    """Restart (or start) the per-user waydroid.service.

    Returns True when systemd accepted the restart.  A non-systemd/manual
    setup returns False and the caller should tell the user to start a
    session explicitly.
    """
    if not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "waydroid.service"],
            capture_output=True, text=True, timeout=60)
    except Exception as e:
        logging.debug("systemctl --user restart failed: %s", e)
        return False
    if result.returncode != 0:
        logging.debug("systemctl --user restart failed: %s",
                      (result.stderr or result.stdout).strip())
        return False
    return True
