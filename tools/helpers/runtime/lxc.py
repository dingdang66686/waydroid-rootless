# Copyright 2026 The Waydroid contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""
LXC runtime backend.

This is a thin adapter around the historical `tools/helpers/lxc.py` (kept as-is
so nothing else breaks) plus the host-side orchestration that used to live
inline in `tools/actions/container_manager.py` (driver preparation, the network
script, sensors, cgroup/NFC hacks, host permission fix-ups).  None of that is
LXC-specific in principle, but in practice it is what the rootful runtime needs,
so it belongs here rather than in the generic container manager.
"""
import glob
import logging
import os
from contextlib import suppress
from shutil import which

import tools.config
import tools.helpers.run
from tools import helpers
from tools.helpers import lxc as _lxc


NAME = "lxc"
NEEDS_ROOT = True
USES_SYSTEM_BUS = True


def is_initialized(args):
    return os.path.isfile(args.config) and os.path.isdir(tools.config.defaults["rootfs"])


def host_binder(args):
    # The rootful LXC container always uses the host binder nodes.
    return True


# --- one-time host preparation (init / upgrade) ---------------------------

def setup_host(args):
    helpers.drivers.loadBinderNodes(args)
    _lxc.setup_host_perms(args)
    _lxc.set_lxc_config(args)
    _lxc.make_base_props(args)


# --- binder / ashmem ------------------------------------------------------

def set_permissions(args, perm_list=None, mode="777"):
    def chmod(path, mode):
        if os.path.exists(path):
            command = ["chmod", mode, "-R", path]
            tools.helpers.run.user(args, command, check=False)

    # Nodes list
    if not perm_list:
        perm_list = [
            "/dev/ashmem",

            # sw_sync for HWC
            "/dev/sw_sync",
            "/sys/kernel/debug/sync/sw_sync",

            # Media
            "/dev/Vcodec",
            "/dev/MTK_SMI",
            "/dev/mdp_sync",
            "/dev/mtk_cmdq",

            # Graphics
            "/dev/graphics",
            "/dev/pvr_sync",
            "/dev/ion",
        ]

        # DRM render nodes
        perm_list.extend(glob.glob("/dev/dri/renderD*"))
        # Framebuffers
        perm_list.extend(glob.glob("/dev/fb*"))
        # Videos
        perm_list.extend(glob.glob("/dev/video*"))
        # DMA-BUF Heaps
        perm_list.extend(glob.glob("/dev/dma_heap/*"))

    for path in perm_list:
        chmod(path, mode)


prepared_drivers = False


def prepare_drivers(args):
    global prepared_drivers
    if prepared_drivers:
        return

    # Load binder and ashmem drivers
    cfg = tools.config.load(args)
    if cfg["waydroid"]["vendor_type"] == "MAINLINE":
        if helpers.drivers.probeBinderDriver(args) != 0:
            logging.error("Failed to load Binder driver")
        helpers.drivers.probeAshmemDriver(args)
    helpers.drivers.loadBinderNodes(args)
    set_permissions(args, [
        "/dev/" + args.BINDER_DRIVER,
        "/dev/" + args.VNDBINDER_DRIVER,
        "/dev/" + args.HWBINDER_DRIVER
    ], "666")
    prepared_drivers = True


# --- per-session host services --------------------------------------------

def setup_host_services(args):
    # Networking
    command = [tools.config.tools_src +
               "/data/scripts/waydroid-net.sh", "start"]
    tools.helpers.run.user(args, command)

    # Sensors
    if which("waydroid-sensord"):
        tools.helpers.run.user(
            args, ["waydroid-sensord", "/dev/" + args.HWBINDER_DRIVER], output="background")

    # Cgroup hacks
    if which("start"):
        command = ["start", "cgroup-lite"]
        tools.helpers.run.user(args, command, check=False)

    # Keep schedtune around in case nesting is supported
    if os.path.ismount("/sys/fs/cgroup/schedtune"):
        try:
            os.mkdir("/sys/fs/cgroup/schedtune/probe0")
            os.mkdir("/sys/fs/cgroup/schedtune/probe0/probe1")
        except OSError:
            command = ["umount", "-l", "/sys/fs/cgroup/schedtune"]
            tools.helpers.run.user(args, command, check=False)
        finally:
            if os.path.exists("/sys/fs/cgroup/schedtune/probe0/probe1"):
                os.rmdir("/sys/fs/cgroup/schedtune/probe0/probe1")
            if os.path.exists("/sys/fs/cgroup/schedtune/probe0"):
                os.rmdir("/sys/fs/cgroup/schedtune/probe0")

    # TODO: remove NFC hacks
    if which("stop"):
        command = ["stop", "nfcd"]
        tools.helpers.run.user(args, command, check=False)
    elif which("systemctl") and (tools.helpers.run.user(args, ["systemctl", "is-active", "-q", "nfcd"], check=False) == 0):
        command = ["systemctl", "stop", "nfcd"]
        tools.helpers.run.user(args, command, check=False)

    # Set permissions
    set_permissions(args)


def teardown_host_services(args):
    # Networking
    command = [tools.config.tools_src +
               "/data/scripts/waydroid-net.sh", "stop"]
    tools.helpers.run.user(args, command, check=False)

    # TODO: remove NFC hacks
    if which("start"):
        command = ["start", "nfcd"]
        tools.helpers.run.user(args, command, check=False)
    elif which("systemctl") and (tools.helpers.run.user(args, ["systemctl", "is-enabled", "-q", "nfcd"], check=False) == 0):
        command = ["systemctl", "start", "nfcd"]
        tools.helpers.run.user(args, command, check=False)

    # Sensors
    if which("waydroid-sensord"):
        command = ["pidof", "waydroid-sensord"]
        pid = tools.helpers.run.user(args, command, check=False, output_return=True).strip()
        if pid:
            command = ["kill", "-9", pid]
            tools.helpers.run.user(args, command, check=False)


# --- per-session files ----------------------------------------------------

def prepare_session(args, session):
    # Create session-specific LXC config file
    _lxc.generate_session_lxc_config(args, session)
    # Backwards compatibility
    with open(tools.config.defaults["lxc"] + "/waydroid/config") as f:
        if "config_session" not in f.read():
            helpers.mount.bind(args, session["waydroid_data"],
                               tools.config.defaults["data"])

    # Mount rootfs
    cfg = tools.config.load(args)
    helpers.images.mount_rootfs(args, cfg["waydroid"]["images_path"], session)


def teardown_session(args):
    # Umount rootfs
    helpers.images.umount_rootfs(args)

    # Backwards compatibility
    with suppress(Exception):
        helpers.mount.umount_all(args, tools.config.defaults["data"])


def set_protocol(args):
    helpers.protocol.set_aidl_version(args)


# --- lifecycle ------------------------------------------------------------

def status(args):
    return _lxc.status(args)


def start(args, session):
    _lxc.start(args)


def stop(args):
    _lxc.stop(args)


def freeze(args):
    _lxc.freeze(args)


def unfreeze(args):
    _lxc.unfreeze(args)


def shell(args):
    _lxc.shell(args)


def logcat(args):
    _lxc.logcat(args)
