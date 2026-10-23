# Copyright 2023 Maximilian Wende
# SPDX-License-Identifier: GPL-3.0-or-later

import tools.helpers.run
import subprocess
import logging
import re
from contextlib import suppress
from shutil import which


def _adb_target(args):
    """Address `adb connect` should use.

    Rootless podman forwards a host loopback port to the container's adbd
    (pasta would otherwise drop all TCP via podman's "-t none"), so the
    backend's `adb_address()` wins over the guest IP.
    """
    if args is not None:
        try:
            resolver = getattr(tools.helpers.runtime.get(args),
                               "adb_address", None)
            if resolver:
                target = resolver(args)
                if target:
                    return target
        except Exception as e:
            logging.debug("backend adb address lookup failed: %s", e)
    return get_device_ip_address(args)


def adb_connect(args):
    """
    Creates an android debugging connection from the host system to the
    Waydroid device, if ADB is found on the host system and the device
    has booted.
    """
    # Check if adb exists on the system.
    if not which("adb"):
        raise RuntimeError("Could not find adb")

    # Start and 'warm up' the adb server
    subprocess.run(["adb", "start-server"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)

    target = _adb_target(args)
    if not target:
        raise RuntimeError("Unknown container IP address. Is Waydroid running?")

    result = subprocess.run(["adb", "connect", target],
                            capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or "connected to" not in output:
        hint = ""
        if "authenticate" in output or "unauthorized" in output:
            hint = (" Approve the 'Allow USB debugging' dialog in Android "
                    "(tick 'Always allow from this computer').")
        raise RuntimeError("adb connect {} failed: {}.{}".format(
            target, output or "unknown error", hint))
    logging.info("Established ADB connection to Waydroid device at {}."
                 .format(target))

    devices = subprocess.run(["adb", "devices"], capture_output=True,
                             text=True).stdout
    for line in devices.splitlines():
        if line.startswith(target) and "unauthorized" in line:
            logging.warning(
                "ADB device is unauthorized: approve the 'Allow USB debugging' "
                "dialog in Android (tick 'Always allow from this computer')")
            break


def adb_disconnect(args):
    if not which("adb"):
        raise RuntimeError("Could not find adb")

    target = _adb_target(args)
    if not target:
        raise RuntimeError("Unknown container IP address. Was Waydroid ever running?")

    subprocess.run(["adb", "disconnect", target], check=False)


def get_device_ip_address(args=None):
    # A backend that knows the address owns the answer (pasta has no dnsmasq
    # lease).  Returning None here is final: do not fall back to the rootful
    # dnsmasq lease, which would show a stale LXC address.
    if args is not None:
        try:
            resolver = getattr(tools.helpers.runtime.get(args),
                               "device_ip_address", None)
            if resolver:
                return resolver(args)
        except Exception as e:
            logging.debug("backend IP lookup failed: %s", e)
            return None

    # Rootful LXC: the IP is in the dnsmasq lease file.
    with suppress(IOError):
        with open("/var/lib/misc/dnsmasq.waydroid0.leases") as f:
            match = re.search(r"(\d{1,3}\.){3}\d{1,3}\s", f.read())
            if match:
                return match.group().strip()
