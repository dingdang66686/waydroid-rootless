# Copyright 2026 The Waydroid contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Container runtime backends.

Historically `tools/helpers/lxc.py` was "all the runtime knowledge": it both
wrote the LXC configuration and shelled out to lxc-start/lxc-stop/lxc-info.
This package turns that into a small backend abstraction so that a second
runtime -- rootless podman -- can be plugged in without touching the rest of
the CLI.

Backends live in this package and expose the same surface:

    NAME                 "lxc" | "podman"
    NEEDS_ROOT           does the *CLI* action need host root?
    USES_SYSTEM_BUS      is the container service on the D-Bus system bus?

    is_initialized(args) -> bool
    setup_host(args)                   one-time host preparation (init/upgrade)
    prepare_drivers(args)              binder/ashmem (and friends)
    setup_host_services(args)          network, sensors, cgroup, ... for a session
    teardown_host_services(args)       inverse of the above
    prepare_session(args, session)     rootfs + per-session files
    teardown_session(args)             inverse of the above
    set_protocol(args)                 binder/AIDL protocol for this image
    status(args) -> "RUNNING"|"FROZEN"|"STOPPED"
    start(args, session) / stop(args) / freeze(args) / unfreeze(args)
    shell(args) / logcat(args)

Callers must never import a concrete backend directly; they ask
`tools.helpers.runtime.get(args)` which resolves the active backend from the
`container_backend` configuration key (bootstrapped via the WAYDROID_BACKEND
environment variable, see name() below).

See docs/rfc/rootless-backend.md for the full design.
"""
import logging
import os

import tools.config

BACKENDS = ("lxc", "podman")


def name(args):
    """Return the active backend name.

    The *bootstrap* answer comes from the WAYDROID_BACKEND environment variable,
    because "which backend?" has to be answered before the config file -- whose
    location can itself depend on the backend -- has been read.  Once a config
    file exists, the persisted `container_backend` key is authoritative and the
    environment variable is only used to seed it.
    """
    env = os.environ.get("WAYDROID_BACKEND")
    if env:
        return env
    try:
        cfg = tools.config.load(args)
        backend = cfg["waydroid"].get("container_backend")
        if backend:
            return backend
    except Exception as e:
        logging.debug("Could not read container_backend from config: %s", e)
    return "lxc"


def get(args):
    """Return the backend module for the active backend."""
    backend = name(args)
    if backend == "lxc":
        from tools.helpers.runtime import lxc as module
    elif backend == "podman":
        from tools.helpers.runtime import podman as module
    else:
        raise RuntimeError("Unknown container backend: {}".format(backend))
    return module


def needs_root(args):
    return get(args).NEEDS_ROOT


def uses_system_bus(args):
    return get(args).USES_SYSTEM_BUS


def is_initialized(args):
    return get(args).is_initialized(args)


def host_binder(args):
    """True when this backend leaves the host able to reach Android's service
    manager over /dev/binder.

    The rootful LXC container always shares the host binder nodes; the rootless
    podman backend only does so when podman_binderfs=\"host\".  When this is
    False the gbinder-based session services (user monitor, clipboard,
    notifications, hardware HAL) cannot work and are skipped.
    """
    return get(args).host_binder(args)
