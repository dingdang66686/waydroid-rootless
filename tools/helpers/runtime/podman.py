# Copyright 2026 The Waydroid contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Rootless podman runtime backend.

This is the Python port of waydroid-rootless/run.sh: it boots the already
installed Waydroid Android image inside an unprivileged podman container instead
of Waydroid's rootful LXC runtime.  Every flag/probe that run.sh accumulated
lives here as an explicit `podman run` argument list plus a handful of host
files that are bind-mounted over the Android tree.

Host prerequisites (all satisfied by an ordinary user except where noted):

  * >= 131072 uids/gids in /etc/subuid and /etc/subgid, so Android's netd can
    install its 0..INT_MAX per-UID routing rules.  Without them the backend
    falls back to --userns=keep-id and devstage repairs the routing itself.
  * podman/crun and pasta.
  * a C compiler, to build the tiny static devstage/wd-broker stubs on first
    use.  The binder fd broker (wd-broker.c) is what lets the *host* reach
    Android's service manager despite binderfs being container-local.

See docs/rfc/rootless-backend.md for the full rationale, and
waydroid-rootless/run.sh for the commented original.
"""
import glob
import logging
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from contextlib import suppress
from shutil import which

import tools.config
import tools.helpers.run
from tools import helpers
from tools.helpers.lxc import ANDROID_ENV
from tools.helpers.runtime import overlay as _overlay


NAME = "podman"
NEEDS_ROOT = False
USES_SYSTEM_BUS = False

# Android's own uid/gid space is 0..99999 and AID_USER_OFFSET puts the next user
# at 100000..199999, so 2^18 covers it twice over.  Mapping more is pointless
# (see run.sh "netd's rtnetlink UID ranges").
MAP_CAP = 131072
INT_MAX = 2147483647

# Capabilities Waydroid's LXC config grants and Android's init needs that are
# not already in rootless podman's default set.  See run.sh for the details.
CAPS = [
    "sys_admin", "sys_ptrace", "sys_nice", "sys_resource", "syslog",
    "net_admin", "net_raw", "dac_read_search", "ipc_lock", "block_suspend",
    "wake_alarm", "audit_control",
]


# --- config helpers -------------------------------------------------------

def _cfg(args):
    return tools.config.load(args)


def _name(cfg):
    return cfg["waydroid"].get("podman_name") or "waydroid"


def _image(args, cfg):
    # The session process may select a locally overlaid derivative for this
    # run; it is not written back to waydroid.cfg.
    return (getattr(args, "podman_overlay_image", None)
            or cfg["waydroid"].get("podman_image") or "")


def _data_volume(cfg):
    return cfg["waydroid"].get("podman_data_volume") or "waydroid-data"


def _volume_mountpoint(name):
    result = subprocess.run(
        ["podman", "volume", "inspect", "-f", "{{.Mountpoint}}", name],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _ensure_data_volume(args, cfg):
    name = _data_volume(cfg)
    exists = subprocess.run(["podman", "volume", "exists", name],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if exists.returncode == 0:
        return name
    result = subprocess.run(["podman", "volume", "create", name],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError("could not create podman volume {}".format(name))
    return name


def _data_dir(args, cfg):
    """Host mountpoint of the named /data volume."""
    name = _ensure_data_volume(args, cfg)
    mountpoint = _volume_mountpoint(name)
    if not mountpoint:
        raise RuntimeError("could not inspect podman volume {}".format(name))
    return mountpoint


def _fix_data_owner(args, cfg):
    """Map the named volume root to Android's AOSP /data ownership (0:1000)."""
    if str(cfg["waydroid"].get("podman_uid_map", "auto")) not in ("auto", ""):
        logging.info("[podman] custom userns mapping; leaving /data root owner")
        return
    mountpoint = _data_dir(args, cfg)
    result = subprocess.run(["podman", "unshare", "chown", "0:1000", mountpoint],
                            capture_output=True, text=True)
    if result.returncode != 0:
        logging.warning("[podman] could not set /data root ownership: %s",
                        result.stderr.strip() or result.stdout.strip())


def _storage_driver():
    result = subprocess.run(
        ["podman", "info", "--format", "{{.Store.GraphDriverName}}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _storage_options():
    result = subprocess.run(
        ["podman", "info", "--format", "{{json .Store.GraphOptions}}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _work_tmp(args):
    return args.work + "/tmp"


def _ensure_cfg_args(args, cfg):
    """Populate the args attributes make_base_props()/images.get() expect.

    On the CLI those are only set by 'waydroid init'/'upgrade'; a plain
    'container start' has to recover them from the config file.
    """
    for arg, key, default in (
            ("vendor_type", "vendor_type", "MAINLINE"),
            ("images_path", "images_path", tools.config.defaults["images_path"]),
            ("system_ota", "system_ota", "None"),
            ("vendor_ota", "vendor_ota", "None")):
        if not getattr(args, arg, None):
            setattr(args, arg, cfg["waydroid"].get(key) or default)


# --- uid/gid mapping ------------------------------------------------------

def _subid_block(kind):
    """Return (host_start, count, intermediate_start) for the best matching
    /etc/sub<kind>id block, or None.  Port of run.sh's subid_block()."""
    lines = []
    paths = ["/etc/sub{}id".format(kind)]
    paths += sorted(glob.glob("/etc/sub{}id.d/*".format(kind)))
    for path in paths:
        with suppress(OSError):
            with open(path) as f:
                lines.extend(f.readlines())

    user = pwd.getpwuid(os.getuid()).pw_name
    uid = str(os.getuid())
    count = 0
    host_start = 0
    inter_off = 0
    inter = 0
    for line in lines:
        parts = line.strip().split(":")
        if len(parts) < 3 or (parts[0] != user and parts[0] != uid):
            continue
        try:
            n = int(parts[2])
        except ValueError:
            continue
        if n > 0:
            if n > count:
                count = n
                host_start = int(parts[1])
                inter_off = inter + 1
            inter += n
    if not count:
        return None
    return host_start, count, inter_off


def _cap_count(n):
    return MAP_CAP if n > MAP_CAP else n


def _userns_opts(args, cfg):
    """Return (podman options, derived) for the user namespace."""
    # WD_USENS is the run.sh A/B escape hatch and wins over the config.
    mode = os.environ.get("WD_USENS") or cfg["waydroid"].get("podman_uid_map") or "auto"
    if mode == "keep-id":
        return ["--userns=keep-id:uid=0,gid=0"], False
    if mode != "auto":
        return ["--userns=" + mode], False

    ub = _subid_block("u")
    gb = _subid_block("g")
    if ub and gb and ub[1] > 1 and gb[1] > 1:
        un = _cap_count(ub[1])
        gn = _cap_count(gb[1])
        opts = [
            "--uidmap", "0:0:1",
            "--uidmap", "1:{}:{}".format(ub[2], un - 1),
            "--uidmap", "{}:{}:1".format(INT_MAX, ub[2] + un - 1),
            "--gidmap", "0:0:1",
            "--gidmap", "1:{}:{}".format(gb[2], gn - 1),
            "--gidmap", "{}:{}:1".format(INT_MAX, gb[2] + gn - 1),
        ]
        logging.info(
            "podman userns: container 0 -> host %s, 1..%s -> host %s..%s, "
            "%s -> host %s",
            os.getuid(), un - 1, ub[0], ub[0] + un - 2,
            INT_MAX, ub[0] + un - 1)
        if un < 131072:
            logging.warning(
                "/etc/subuid holds only %s uids, so Android uids above %s "
                "(isolated processes) cannot be mapped", ub[1], un - 1)
        return opts, True
    return ["--userns=keep-id:uid=0,gid=0"], False


def _userns_covers_int_max(kind):
    """Only used on the keep-id fallback: does podman's own map reach INT_MAX?"""
    with suppress(Exception):
        out = subprocess.run(["podman", "unshare", "cat",
                              "/proc/self/{}_map".format(kind)],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            fields = line.split()
            if len(fields) >= 3 and int(fields[0]) + int(fields[2]) > INT_MAX:
                return True
    return False


# --- host network probes --------------------------------------------------

def _host_dns():
    with suppress(Exception):
        out = subprocess.run(["resolvectl", "status"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "DNS Servers:" in line:
                for tok in line.split()[2:]:
                    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", tok):
                        return tok
    return "10.0.0.1"


def _default_iface():
    with suppress(Exception):
        out = subprocess.run(["ip", "route", "show", "default"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            fields = line.split()
            if len(fields) >= 5:
                return fields[4]
    return ""


def _default_gateway():
    with suppress(Exception):
        out = subprocess.run(["ip", "route", "show", "default"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[1] == "via":
                return fields[2]
    return ""


def _pasta_network(args, cfg, wrap_dir):
    """Build the --network= value and, when needed, a PATH entry for the pasta
    wrapper that drops podman's hard-coded --config-net."""
    host_dns = _host_dns()
    network = "pasta:-I,eth0,--dns={},--dhcp-dns".format(host_dns)
    path_prefix = ""

    config_net = (os.environ.get("WD_PASTA_CONFIG_NET")
                  or cfg["waydroid"].get("podman_pasta_config_net"))
    if not config_net:
        # Without --config-net, pasta derives the advertised prefix from the
        # host interface and picks the first address (often the ULA).  Pin the
        # global one so netd's own SLAAC lands on a routable address.
        iface = _default_iface() or "wlan0"
        addr = ""
        prefix_len = ""
        with suppress(Exception):
            out = subprocess.run(["ip", "-6", "route", "show", "dev", iface,
                                  "proto", "ra"],
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                fields = line.split()
                if fields and re.match(r"^[23]", fields[0]):
                    prefix_len = fields[0].split("/")[-1]
                    break
        with suppress(Exception):
            out = subprocess.run(["ip", "-6", "addr", "show", "dev", iface,
                                  "scope", "global"],
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                fields = line.split()
                if len(fields) >= 2 and re.match(r"^[23]", fields[1]):
                    addr = fields[1].split("/")[0]
                    break
        if addr and prefix_len:
            network += ",-a,{}/{}".format(addr, prefix_len)

        pasta_real = which("pasta")
        if pasta_real:
            wrap = os.path.join(wrap_dir, "pasta-wrap")
            os.makedirs(wrap, exist_ok=True)
            wrapper = os.path.join(wrap, "pasta")
            with open(wrapper, "w") as f:
                f.write(
                    "#!/usr/bin/env bash\n"
                    "# generated by tools/helpers/runtime/podman.py - drop the\n"
                    "# --config-net podman insists on adding so passt keeps\n"
                    "# router advertisements enabled.\n"
                    "args=()\n"
                    'for a in "$@"; do\n'
                    '  [[ "$a" == "--config-net" ]] || args+=("$a")\n'
                    "done\n"
                    'exec "{}" "${{args[@]}}"\n'.format(pasta_real))
            os.chmod(wrapper, 0o755)
            path_prefix = wrap

    extra = cfg["waydroid"].get("podman_pasta_options")
    if extra:
        network += "," + extra
    return network, path_prefix


def _host_gateway(args, cfg, derived):
    if derived or (_userns_covers_int_max("uid") and _userns_covers_int_max("gid")):
        gateway = ""
    else:
        gateway = _default_gateway()
        logging.warning(
            "the rootless user namespace stops below uid %s, so the kernel "
            "rejects netd's per-UID rules (EINVAL) and devstage's netfix() has "
            "to repair the routing instead; configure a subuid/subgid block of "
            "at least 131072 to let netd do it natively", INT_MAX)
    # An explicit (even empty) WD_GATEWAY always wins over the probe.
    if os.environ.get("WD_GATEWAY") is not None:
        gateway = os.environ["WD_GATEWAY"]
    return gateway


# --- devices --------------------------------------------------------------

def _device_opts(args, cfg):
    opts = []

    def add(src, dst=None):
        if os.path.exists(src):
            opts.extend(["--device", src + (":" + dst if dst else "")])

    add("/dev/dri/renderD128")
    add("/dev/dri/card0")
    add("/dev/dma_heap/system")
    add("/dev/fb0")
    add("/dev/uhid")
    add("/dev/fuse")
    add("/dev/net/tun", "/dev/tun")

    if cfg["waydroid"].get("podman_binderfs") == "host":
        with suppress(Exception):
            helpers.drivers.loadBinderNodes(args)
        for drv, dst in ((getattr(args, "BINDER_DRIVER", None), "/dev/binder"),
                         (getattr(args, "VNDBINDER_DRIVER", None), "/dev/vndbinder"),
                         (getattr(args, "HWBINDER_DRIVER", None), "/dev/hwbinder")):
            if drv:
                add("/dev/" + drv, dst)
    return opts


def _socket_binds(args, cfg, session=None):
    session = session or {}
    binds = []

    def param(key, fallback):
        value = session.get(key)
        if not value or value == "None":
            return fallback
        return value

    xdg_runtime = param("xdg_runtime_dir",
                        os.environ.get("XDG_RUNTIME_DIR")
                        or "/run/user/{}".format(os.getuid()))
    display = param("wayland_display", "wayland-0")
    wayland = os.path.join(xdg_runtime, display)
    with suppress(OSError):
        if stat.S_ISSOCK(os.stat(wayland).st_mode):
            os.chmod(wayland, 0o777)
            binds += ["-v", "{}:/run/xdg/wayland-0".format(wayland)]
    pulse = os.path.join(xdg_runtime, "pulse", "native")
    if os.path.exists(pulse):
        binds += ["-v", "{}:/run/xdg/pulse/native".format(pulse)]
    return binds


# --- host-side staging ----------------------------------------------------

def _devstage(args):
    """Ship build/compile the static PID-1 stub (see devstage.c)."""
    binary = args.work + "/devstage"
    source = os.path.join(os.path.dirname(os.path.realpath(__file__)), "devstage.c")
    if (not os.path.exists(binary)
            or os.path.getmtime(source) > os.path.getmtime(binary)):
        logging.info("[podman] cc -static -o %s %s", binary, source)
        tools.helpers.run.user(args, ["cc", "-O2", "-static", "-Wall", "-o",
                                      binary, source])
    return binary


# --- host <-> container binder bridge ------------------------------------
#
# The container creates its own binderfs (devstage.c).  Those nodes live in the
# container's mount namespace and cannot be reopened from the host by
# major:minor, so the host reaches Android's servicemanager through a tiny
# in-container helper (wd-broker.c) that opens the node and passes the fd over
# a unix socket in a host-owned bind mount.  See docs/rfc/rootless-backend.md.

_BINDER_PATHS = {}
_BINDER_FDS = {}


def _binder_dir(args):
    return os.path.join(args.work, "tmp", "binder")


def _broker_binary(args):
    directory = _binder_dir(args)
    os.makedirs(directory, exist_ok=True)
    binary = os.path.join(directory, "wd-broker")
    source = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                          "wd-broker.c")
    if (not os.path.exists(binary)
            or os.path.getmtime(source) > os.path.getmtime(binary)):
        logging.info("[podman] cc -static -o %s %s", binary, source)
        tools.helpers.run.user(args, ["cc", "-O2", "-static", "-Wall", "-o",
                                      binary, source])
    return binary


def _container_running(cfg):
    result = subprocess.run(
        ["podman", "container", "inspect", "-f", "{{.State.Status}}",
         _name(cfg)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True)
    return result.returncode == 0 and result.stdout.strip() == "running"


def _container_id(cfg):
    result = subprocess.run(
        ["podman", "container", "inspect", "-f", "{{.Id}}", _name(cfg)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return _name(cfg)


def _acquire_binder_fd(args, cfg, dev):
    """Ask the container to open /dev/binderfs/<dev> and receive the fd."""
    name = _name(cfg)
    _broker_binary(args)
    sock_name = "{}-{}-{}.sock".format(dev, os.getpid(), time.time_ns())
    sock = os.path.join(_binder_dir(args), sock_name)
    with suppress(FileNotFoundError):
        os.unlink(sock)

    proc = subprocess.Popen(
        ["podman", "exec", "-u", "0", name,
         "/run/wd-binder/wd-broker", "/dev/binderfs/" + dev,
         "/run/wd-binder/" + sock_name])
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if os.path.exists(sock):
                break
            if proc.poll() is not None:
                raise RuntimeError(
                    "binder broker for {} exited with {}".format(
                        dev, proc.returncode))
            time.sleep(0.05)
        else:
            raise RuntimeError(
                "binder broker for {} did not create {}".format(dev, sock))

        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(sock)
            _, fds, _, _ = socket.recv_fds(conn, 1, 1)
        finally:
            conn.close()
    finally:
        with suppress(Exception):
            proc.terminate()
        with suppress(Exception):
            proc.wait(timeout=5)
        with suppress(FileNotFoundError):
            os.unlink(sock)

    if not fds:
        raise RuntimeError("no binder fd received for {}".format(dev))
    return fds[0]


def binder_device_paths(args):
    """Return ("fd/N", ...) driver names for gbinder, or None.

    Each process that talks to Android acquires its own fds; they are cached
    for the lifetime of the process.  "/dev/" + "fd/N" is /dev/fd/N, which the
    interfaces already pass to gbinder.ServiceManager().
    """
    cfg = _cfg(args)
    if cfg["waydroid"].get("podman_binderfs") == "host":
        return None
    if not _container_running(cfg):
        return None

    key = _container_id(cfg)
    cached = _BINDER_PATHS.get(key)
    if cached and all(
            os.path.exists("/proc/self/fd/" + p.split("/")[-1])
            for p in cached):
        return cached

    # A recreated container has a new binder context; drop the old fds.
    for old in _BINDER_FDS.pop(key, []):
        with suppress(OSError):
            os.close(old)
    _BINDER_PATHS.pop(key, None)

    fds = []
    paths = []
    for dev in ("binder", "vndbinder", "hwbinder"):
        fd = _acquire_binder_fd(args, cfg, dev)
        fds.append(fd)
        paths.append("fd/{}".format(fd))

    _BINDER_FDS[key] = fds          # keep the descriptors open
    _BINDER_PATHS[key] = tuple(paths)
    return _BINDER_PATHS[key]



def _stage_host_files(args, cfg, tmp):
    """Prepare the host files/sockets every run needs.  Returns
    (binds, path_prefix) where path_prefix is prepended to PATH if needed."""
    binds = []

    # Android's first stage chmod("/proc/cmdline", 0440); bind a plain file.
    cmdline = os.path.join(tmp, "cmdline")
    if not os.path.exists(cmdline):
        os.makedirs(os.path.dirname(cmdline), exist_ok=True)
        with open("/proc/cmdline") as src, open(cmdline, "w") as dst:
            dst.write(src.read())
    binds += ["-v", "{}:/proc/cmdline".format(cmdline)]

    # ueventd asks for 16 MB SO_RCVBUF and retries with SO_RCVBUFFORCE, which
    # can never succeed rootless.  Request min(rmem_max, 16 MB) instead.
    ueventd = os.path.join(tmp, "ueventd.rc")
    try:
        with open("/proc/sys/net/core/rmem_max") as f:
            rmem = int(f.read().strip())
    except (OSError, ValueError):
        rmem = 16777216
    if rmem > 16777216:
        rmem = 16777216
    os.makedirs(os.path.dirname(ueventd), exist_ok=True)
    with open(ueventd, "w") as f:
        f.write("uevent_socket_rcvbuf_size {}\n".format(rmem))
    binds += ["-v", "{}:/vendor/ueventd.rc:ro".format(ueventd)]

    binds += ["-v", "{}:/devstage:ro".format(_devstage(args))]

    return binds


# --- the podman run command ----------------------------------------------

def _build_run_cmd(args, session):
    cfg = _cfg(args)
    tmp = _work_tmp(args)
    os.makedirs(tmp, exist_ok=True)

    name = _name(cfg)
    image = _image(args, cfg)
    if not image:
        raise RuntimeError("podman_image is required (OCI image)")

    userns_opts, derived = _userns_opts(args, cfg)
    network, path_prefix = _pasta_network(args, cfg, tmp)
    gateway = _host_gateway(args, cfg, derived)

    cmd = ["podman", "run", "-d", "--name", name]
    cmd += userns_opts
    cmd += ["--user", "0:0", "--hostname", "waydroid",
            "--network=" + network,
            "--env", "WD_GATEWAY=" + gateway,
            "--cgroupns=private",
            "--security-opt", "seccomp=unconfined",
            "--security-opt", "label=disable"]

    # Native podman publish: pasta forwards this host-loopback port to the
    # container's adbd.  No custom pasta "-t" rewriting is needed.
    adb_port = str(cfg["waydroid"].get("podman_adb_port") or "5555")
    if re.match(r"^\d+$", adb_port) and adb_port != "0":
        cmd += ["-p", "127.0.0.1:{}:5555".format(adb_port)]
    if not (os.environ.get("WD_CGROUP_RO")
            or cfg["waydroid"].get("podman_cgroup_ro")):
        cmd += ["--security-opt", "unmask=/sys/fs/cgroup"]
    for cap in CAPS:
        cmd += ["--cap-add", cap]
    cmd += _device_opts(args, cfg)

    data_volume = _ensure_data_volume(args, cfg)
    cmd += ["-v", "{}:/data".format(data_volume)]
    cmd += ["-v", "{}:/vendor/waydroid.prop:ro".format(args.work + "/waydroid.prop")]
    cmd += ["--tmpfs", "/run", "--tmpfs", "/var", "--tmpfs", "/tmp"]
    cmd += _socket_binds(args, cfg, session)

    # Host-owned directory for the binder fd broker's sockets and binary.
    binder_dir = _binder_dir(args)
    os.makedirs(binder_dir, exist_ok=True)
    cmd += ["-v", "{}:/run/wd-binder".format(binder_dir)]

    kmsg = os.environ.get("WD_KMSG_LOG")
    if kmsg:
        os.makedirs(os.path.dirname(kmsg), exist_ok=True)
        open(kmsg, "w").close()
        cmd += ["-v", "{}:/dev/kmsg".format(kmsg)]

    cmd += _stage_host_files(args, cfg, tmp)

    run_root = [image]

    extra = os.environ.get("WD_EXTRA")
    if extra:
        cmd += extra.split()
    cmd += run_root
    cmd += ["/devstage"]
    return cmd, path_prefix


# --- backend interface ----------------------------------------------------

def _image_exists(image):
    if not image:
        return False
    result = subprocess.run(["podman", "image", "exists", image],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    return result.returncode == 0


def uses_image(args):
    """The podman backend boots an OCI image directly; system/vendor images
    are build-system inputs, not runtime state."""
    return True


def is_initialized(args):
    if not os.path.isfile(args.config):
        return False
    return _image_exists(_image(args, _cfg(args)))


def waydroid_data_dir(args):
    """Host mountpoint of the named podman volume mounted at /data."""
    return _data_dir(args, _cfg(args))


def host_binder(args):
    # The fd broker (binder_device_paths) makes Android's servicemanager
    # reachable from the host even though binderfs is container-local.  The
    # legacy "host" mode, which handed the host's binderfs nodes to the
    # container, cannot work rootless (BINDER_SET_CONTEXT_MGR is EPERM), so it
    # is not advertised.
    return _cfg(args)["waydroid"].get("podman_binderfs") != "host"


def setup_host(args):
    cfg = _cfg(args)
    _ensure_cfg_args(args, cfg)
    image = _image(args, cfg)
    if not image:
        raise RuntimeError(
            "the rootless podman backend requires podman_image (an OCI "
            "image); there is no external-rootfs mode")
    if not _image_exists(image):
        raise RuntimeError(
            "podman image {} not found; build it with image/build.sh "
            "or pull it with podman".format(image))
    _ensure_data_volume(args, cfg)
    logging.info("[podman] image: %s (update = rebuild/pull this image; "
                 "the /data volume is preserved)", image)

    driver = _storage_driver()
    if driver != "overlay":
        logging.warning("[podman] storage driver is %r; overlay is preferred",
                        driver)
    if "fuse-overlayfs" in _storage_options():
        logging.info("[podman] storage uses fuse-overlayfs; native kernel "
                     "overlayfs is preferred")

    # Generic host GPU/HAL property probing -- not LXC configuration.  It still
    # lives in tools/helpers/lxc.py; see the note in the RFC about moving it to
    # a shared runtime/hostprops.py.
    from tools.helpers import lxc as _lxc
    _lxc.make_base_props(args)
    os.makedirs(_data_dir(args, cfg), exist_ok=True)
    os.makedirs(_work_tmp(args), exist_ok=True)


def prepare_drivers(args):
    # binderfs is created by devstage inside the container; the host reaches it
    # lazily through binder_device_paths(), once the container is running.
    return


def setup_host_services(args):
    # pasta does the networking inside the container; no sensord/cgroup/NFC
    # host services are involved.
    return


def teardown_host_services(args):
    return


def _force_sensors_stub(prop_path):
    """Sensors go through the host's /dev/hwbinder, which the fd broker does
    not cover (waydroid-sensord is a host binary).  Always stub the HAL."""
    with open(prop_path) as f:
        lines = f.read().splitlines()
    lines = [line for line in lines
             if not line.startswith("waydroid.stub_sensors_hal=")]
    lines.append("waydroid.stub_sensors_hal=1")
    with open(prop_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def prepare_session(args, session):
    cfg = _cfg(args)
    _ensure_cfg_args(args, cfg)
    _ensure_data_volume(args, cfg)
    _fix_data_owner(args, cfg)

    # Apply the local overlay (if any) once, before set_protocol() reads the
    # image build.prop and before the container starts.
    args.podman_overlay_image = _overlay.ensure(args, cfg)

    if not os.path.isfile(args.work + "/waydroid_base.prop"):
        from tools.helpers import lxc as _lxc
        _lxc.make_base_props(args)
    helpers.images.make_prop(args, session, args.work + "/waydroid.prop")
    _force_sensors_stub(args.work + "/waydroid.prop")

    os.makedirs(_work_tmp(args), exist_ok=True)
    _devstage(args)


def teardown_session(args):
    # podman throws its writable overlay away with the container; nothing to do.
    return


def _image_build_prop(args, cfg):
    """Read /system/build.prop out of an image without booting it."""
    image = _image(args, cfg)
    if not image:
        return None
    result = subprocess.run(["podman", "create", image],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    cid = result.stdout.strip()
    dst = os.path.join(_work_tmp(args), "build.prop")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        cp = subprocess.run(["podman", "cp", cid + ":/system/build.prop", dst],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        return dst if cp.returncode == 0 and os.path.isfile(dst) else None
    finally:
        subprocess.run(["podman", "rm", "-f", cid],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def set_protocol(args):
    cfg = _cfg(args)
    build_prop = _image_build_prop(args, cfg)
    if not build_prop:
        logging.error("[podman] could not read /system/build.prop from %s; "
                      "keeping the configured binder protocol",
                      _image(args, cfg))
        return
    helpers.protocol.set_aidl_version(args, build_prop)


def _podman_state(args, name):
    with suppress(Exception):
        out = subprocess.run(["podman", "inspect", "-f", "{{.State.Status}}", name],
                             capture_output=True, text=True)
        if out.returncode == 0:
            return out.stdout.strip()
    return None


def status(args):
    state = _podman_state(args, _name(_cfg(args)))
    if state == "running":
        return "RUNNING"
    if state == "paused":
        return "FROZEN"
    return "STOPPED"


def start(args, session):
    cfg = _cfg(args)
    # container restart bypasses prepare_session(), so check the overlay here
    # too.  The check is cheap when the stamp matches.
    args.podman_overlay_image = _overlay.ensure(args, cfg)
    name = _name(cfg)
    cmd, path_prefix = _build_run_cmd(args, session)

    tools.helpers.run.user(args, ["podman", "rm", "-f", name], check=False)

    env = {}
    if path_prefix:
        env["PATH"] = path_prefix + ":" + os.environ.get("PATH", "")
    tools.helpers.run.user(args, cmd, env=env)

    timeout = 10
    while status(args) != "RUNNING" and timeout > 0:
        logging.info("waiting %s seconds for container to start...", timeout)
        timeout -= 1
        time.sleep(1)
    if status(args) != "RUNNING":
        raise OSError("container failed to start")
    with suppress(OSError):
        os.chmod(args.log, 0o666)


def stop(args):
    name = _name(_cfg(args))
    tools.helpers.run.user(args, ["podman", "stop", "-t", "5", name], check=False)
    tools.helpers.run.user(args, ["podman", "rm", "-f", name], check=False)


def freeze(args):
    tools.helpers.run.user(args, ["podman", "pause", _name(_cfg(args))], check=False)


def unfreeze(args):
    tools.helpers.run.user(args, ["podman", "unpause", _name(_cfg(args))], check=False)


def _exec_cmd(args, command, interactive=False):
    cmd = ["podman", "exec"]
    if interactive:
        cmd.append("-i")
        if sys.stdin.isatty():
            cmd.append("-t")
    for key, value in ANDROID_ENV.items():
        cmd += ["--env", "{}={}".format(key, value)]
    if getattr(args, "uid", None) is not None:
        gid = args.gid if args.gid is not None else args.uid
        cmd += ["--user", "{}:{}".format(args.uid, gid)]
    cmd.append(_name(_cfg(args)))
    cmd += command
    return cmd


def exec_output(args, command, user=None):
    """Run a command in the container; return (rc, combined output)."""
    cmd = ["podman", "exec"]
    if user is not None:
        cmd += ["--user", str(user)]
    cmd.append(_name(_cfg(args)))
    cmd += command
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    return result.returncode, result.stdout


def adb_address(args):
    """Host loopback address pasta forwards to the container's adbd."""
    port = str(_cfg(args)["waydroid"].get("podman_adb_port") or "5555")
    if not re.match(r"^\d+$", port) or port == "0":
        return None
    return "127.0.0.1:" + port


def device_ip_address(args):
    """The container's IPv4 address.

    pasta has no dnsmasq lease file, so read the container's own routing table
    (podman exec runs in its network namespace).
    """
    if status(args) != "RUNNING":
        return None
    rc, out = exec_output(args, ["/system/bin/cat", "/proc/net/fib_trie"])
    if rc != 0:
        return None
    candidates = []
    lines = out.splitlines()
    for i, line in enumerate(lines):
        match = re.search(r"\|\-\-\s+(\d+\.\d+\.\d+\.\d+)\s*$", line)
        if match and "/32 host LOCAL" in "\n".join(lines[i + 1:i + 3]):
            candidates.append(match.group(1))
    for addr in candidates:
        if not addr.startswith("127.") and not addr.startswith("169.254."):
            return addr
    return candidates[0] if candidates else None


def remove_app(args, package):
    """Fallback for IPlatform.removeApp, which returns -1 on this image."""
    rc, _ = exec_output(args, ["/system/bin/pm", "uninstall", package])
    return rc


def push_apk(args, host_path):
    """Copy an APK into the container's writable layer with podman cp.

    The destination is the container root (owned by container root, which maps
    to the user), so this works without touching /data permissions; the file is
    discarded with the container.
    """
    dst = "/tmp/waydroid-install.apk"
    result = subprocess.run(
        ["podman", "cp", host_path, "{}:{}".format(_name(_cfg(args)), dst)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        raise RuntimeError("podman cp failed: {}".format(result.stdout.strip()))
    return dst


def remove_apk(args, container_path):
    exec_output(args, ["/system/bin/rm", "-f", container_path])


def shell(args):
    state = status(args)
    if state == "FROZEN":
        unfreeze(args)
    elif state != "RUNNING":
        logging.error("WayDroid container is {}".format(state))
        return

    command = list(args.COMMAND) if args.COMMAND else ["/system/bin/sh"]
    cmd = _exec_cmd(args, command, interactive=True)

    import signal

    def mock_sigint(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, mock_sigint)
    signal.signal(signal.SIGHUP, mock_sigint)

    perms = stat.S_IMODE(os.stat(sys.stdout.fileno()).st_mode)
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
    finally:
        os.chmod(sys.stdout.fileno(), perms)

    if state == "FROZEN":
        freeze(args)


def logcat(args):
    args.COMMAND = ["/system/bin/logcat"]
    if args.ARGS:
        args.COMMAND.extend(args.ARGS)
    args.uid = None
    args.gid = None
    shell(args)
