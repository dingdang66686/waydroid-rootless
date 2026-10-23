# Copyright 2022 Alessandro Astone
# SPDX-License-Identifier: GPL-3.0-or-later

import dbus


def _bus(args):
    """Return the D-Bus the container runtime daemons listen on.

    The rootful LXC runtime owns the well-known name id.waydro.Container on the
    *system* bus (it is started by a system service, as root).  A rootless
    backend must not and cannot own a system-bus name, so those daemons live on
    the *session* bus instead.  Which one applies is a property of the active
    container backend (see tools/helpers/runtime).
    """
    if args is not None:
        from tools import helpers
        if helpers.runtime.uses_system_bus(args):
            return dbus.SystemBus()
    return dbus.SessionBus()


def DBusContainerService(args=None, object_path="/ContainerManager", intf="id.waydro.ContainerManager"):
    return dbus.Interface(_bus(args).get_object("id.waydro.Container", object_path), intf)

def DBusSessionService(args=None, object_path="/SessionManager", intf="id.waydro.SessionManager"):
    return dbus.Interface(_bus(args).get_object("id.waydro.Session", object_path), intf)
