# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import os
import signal
import tools.config
from contextlib import suppress
from tools import helpers
from tools import services
from tools import actions
import dbus
import dbus.service
import dbus.exceptions
from gi.repository import GLib


def _bus(args):
    """Return the D-Bus the container daemons bind to.

    The rootful LXC runtime owns id.waydro.Container on the system bus; a
    rootless backend runs the same daemons on the session bus instead (see
    helpers.runtime / docs/rfc/rootless-backend.md).
    """
    if helpers.runtime.uses_system_bus(args):
        return dbus.SystemBus()
    return dbus.SessionBus()

class DbusContainerManager(dbus.service.Object):
    def __init__(self, looper, bus, object_path, args):
        self.args = args
        self.looper = looper
        dbus.service.Object.__init__(self, bus, object_path)

    @helpers.logging.log_exceptions
    @dbus.service.method("id.waydro.ContainerManager", in_signature='a{ss}', out_signature='', sender_keyword="sender", connection_keyword="conn")
    def Start(self, session, sender, conn):
        dbus_info = dbus.Interface(conn.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus/Bus", False), "org.freedesktop.DBus")
        uid = dbus_info.GetConnectionUnixUser(sender)
        if str(uid) not in ["0", session["user_id"]]:
            raise RuntimeError("Cannot start a session on behalf of another user")
        pid = dbus_info.GetConnectionUnixProcessID(sender)
        if str(uid) != "0" and str(pid) != session["pid"]:
            raise RuntimeError("Invalid session pid")
        do_start(self.args, session)

    @helpers.logging.log_exceptions
    @dbus.service.method("id.waydro.ContainerManager", in_signature='b', out_signature='')
    def Stop(self, quit_session):
        stop(self.args, quit_session)

    @helpers.logging.log_exceptions
    @dbus.service.method("id.waydro.ContainerManager", in_signature='', out_signature='')
    def Freeze(self):
        if not actions.initializer.is_initialized(self.args):
            raise RuntimeError("Waydroid is not initialized")
        freeze(self.args)

    @helpers.logging.log_exceptions
    @dbus.service.method("id.waydro.ContainerManager", in_signature='', out_signature='')
    def Unfreeze(self):
        if not actions.initializer.is_initialized(self.args):
            raise RuntimeError("Waydroid is not initialized")
        unfreeze(self.args)

    @helpers.logging.log_exceptions
    @dbus.service.method("id.waydro.ContainerManager", in_signature='', out_signature='a{ss}')
    def GetSession(self):
        if not actions.initializer.is_initialized(self.args):
            raise RuntimeError("Waydroid is not initialized")
        try:
            session = self.args.session
            session["state"] = helpers.runtime.get(self.args).status(self.args)
            return session
        except AttributeError:
            return {}

def start(args):
    mainloop = GLib.MainLoop()

    def sigint_handler(data):
        with suppress(Exception):
            stop(args)
        mainloop.quit()

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, sigint_handler, None)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, sigint_handler, None)

    bus = _bus(args)
    initializer = actions.initializer.DbusInitializer(mainloop, bus, '/Initializer', args)
    _container_manager = DbusContainerManager(mainloop, bus, '/ContainerManager', args)

    try:
        _name = dbus.service.BusName("id.waydro.Container", bus, do_not_queue=True)
    except dbus.exceptions.NameExistsException:
        logging.error("Container service is already running")
        return

    mainloop.run()

    if initializer.worker_thread is not None:
        initializer.worker_thread.kill()
        initializer.worker_thread.join()

def do_start(args, session):
    if not actions.initializer.is_initialized(args):
        raise RuntimeError("Waydroid is not initialized")

    if "session" in args:
        raise RuntimeError("Already tracking a session")

    backend = helpers.runtime.get(args)
    backend.prepare_drivers(args)

    logging.info("Starting up container for a new session")

    backend.setup_host_services(args)
    backend.prepare_session(args, session)
    backend.set_protocol(args)

    backend.start(args, session)
    if helpers.runtime.host_binder(args):
        services.hardware_manager.start(args)

    args.session = session

def stop(args, quit_session=True):
    if not actions.initializer.is_initialized(args):
        raise RuntimeError("Waydroid is not initialized")

    logging.info("Stopping container")

    try:
        backend = helpers.runtime.get(args)

        if helpers.runtime.host_binder(args):
            services.hardware_manager.stop(args)

        status = backend.status(args)
        if status != "STOPPED":
            backend.stop(args)
            while backend.status(args) != "STOPPED":
                pass

        backend.teardown_host_services(args)
        backend.teardown_session(args)

        if "session" in args:
            if quit_session:
                logging.info("Terminating session because the container was stopped")
                with suppress(OSError):
                    os.kill(int(args.session["pid"]), signal.SIGUSR1)
            del args.session
    except Exception as e:
        logging.debug("Error while stopping container: %s", e)

def stop_cli(args):
    """`waydroid container stop` from the CLI.

    Prefer the daemon (standalone or merged into the session): it stops the
    container *and* terminates the session it is tracking.  Fall back to a
    direct stop when no daemon is listening.
    """
    try:
        helpers.ipc.DBusContainerService(args).Stop(True)
        return
    except dbus.DBusException:
        pass
    stop(args)


def restart(args):
    backend = helpers.runtime.get(args)
    status = backend.status(args)
    if status == "FROZEN":
        backend.unfreeze(args)
        while backend.status(args) == "FROZEN":
            pass
        status = backend.status(args)
    if status == "RUNNING":
        backend.stop(args)
        backend.start(args, getattr(args, "session", None))
    else:
        logging.error("WayDroid container is {}".format(status))

def freeze(args):
    backend = helpers.runtime.get(args)
    status = backend.status(args)
    if status == "RUNNING":
        backend.freeze(args)
        while backend.status(args) == "RUNNING":
            pass
    else:
        logging.error("WayDroid container is {}".format(status))

def unfreeze(args):
    backend = helpers.runtime.get(args)
    status = backend.status(args)
    if status == "FROZEN":
        backend.unfreeze(args)
        while backend.status(args) == "FROZEN":
            pass
    else:
        logging.error("WayDroid container is {}".format(status))
