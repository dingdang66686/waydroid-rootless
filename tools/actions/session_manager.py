# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import os
import signal
import sys
import tools.config
import tools.helpers.ipc
from tools import services
import dbus
import dbus.service
import dbus.exceptions
from gi.repository import GLib
import copy

class DbusSessionManager(dbus.service.Object):
    def __init__(self, looper, bus, object_path, args):
        self.args = args
        self.looper = looper
        dbus.service.Object.__init__(self, bus, object_path)

    @dbus.service.method("id.waydro.SessionManager", in_signature='', out_signature='')
    def Stop(self):
        do_stop(self.args, self.looper)
        stop_container(self.args, quit_session=False)

def handle_disconnect(args, looper):
    do_stop(args, looper)
    stop_container(args, quit_session=False)

def service(args, looper):
    bus = dbus.SessionBus()
    bus.set_exit_on_disconnect(False)
    bus.add_signal_receiver(lambda: handle_disconnect(args, looper),
                            signal_name='Disconnected',
                            dbus_interface='org.freedesktop.DBus.Local')
    _session_manager = DbusSessionManager(looper, dbus.SessionBus(), '/SessionManager', args)
    looper.run()

def start(args, unlocked_cb=None, background=True):
    try:
        _name = dbus.service.BusName("id.waydro.Session", dbus.SessionBus(), do_not_queue=True)
    except dbus.exceptions.NameExistsException:
        logging.error("Session is already running")
        if unlocked_cb:
            unlocked_cb()
        return

    session = copy.copy(tools.config.session_defaults)

    # A backend may put Android's /data somewhere else (the rootless podman
    # backend uses $WORK/data instead of the rootful $XDG_DATA_HOME path).
    data_resolver = getattr(tools.helpers.runtime.get(args),
                            "waydroid_data_dir", None)
    if data_resolver:
        session["waydroid_data"] = data_resolver(args)

    # Android writes the PNGs into /data/icons; the desktop files (the
    # host-side artifact) reference the backend's host-visible /data mount.
    # Do not create the directory here: Android owns it.
    session["waydroid_icons"] = os.path.join(session["waydroid_data"], "icons")

    # TODO: also support WAYLAND_SOCKET?
    wayland_display = session["wayland_display"]
    if wayland_display == "None" or not wayland_display:
        logging.warning('WAYLAND_DISPLAY is not set, defaulting to "wayland-0"')
        wayland_display = session["wayland_display"] = "wayland-0"

    if os.path.isabs(wayland_display):
        wayland_socket_path = wayland_display
    else:
        xdg_runtime_dir = session["xdg_runtime_dir"]
        if xdg_runtime_dir == "None" or not xdg_runtime_dir:
            logging.error("XDG_RUNTIME_DIR is not set; please don't start a Waydroid session with 'sudo'!")
            sys.exit(1)
        wayland_socket_path = os.path.join(xdg_runtime_dir, wayland_display)
    if not os.path.exists(wayland_socket_path):
        logging.error(f"Wayland socket '{wayland_socket_path}' doesn't exist; are you running a Wayland compositor?")
        sys.exit(1)

    waydroid_data = session["waydroid_data"]
    if not os.path.isdir(waydroid_data):
        os.makedirs(waydroid_data)

    dpi = tools.helpers.props.host_get(args, "ro.sf.lcd_density")
    if dpi == "":
        dpi = os.getenv("GRID_UNIT_PX")
        if dpi is not None:
            dpi = str(int(dpi) * 20)
        else:
            dpi = "0"
    session["lcd_density"] = dpi

    session["background_start"] = "true" if background else "false"

    mainloop = GLib.MainLoop()

    def sigint_handler(data):
        do_stop(args, mainloop)
        stop_container(args, quit_session=False)

    def sigusr_handler(data):
        do_stop(args, mainloop)

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGHUP, sigint_handler, None)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, sigint_handler, None)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, sigint_handler, None)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGUSR1, sigusr_handler, None)

    # Rootless: this process is the single per-user service.  Own both
    # well-known names and create the container control objects on this same
    # bus/loop.  A standalone `waydroid container start` still works: if the
    # name is already taken we fall back to the D-Bus Start() below.
    local_container = False
    container_name = None
    container_initializer = None
    if not tools.helpers.runtime.uses_system_bus(args):
        try:
            container_name = dbus.service.BusName(
                "id.waydro.Container", dbus.SessionBus(), do_not_queue=True)
        except dbus.exceptions.NameExistsException:
            container_name = None
        if container_name is not None:
            from tools.actions import container_manager as _cm
            from tools.actions import initializer as _init
            local_container = True
            args._local_container = True
            bus = dbus.SessionBus()
            container_initializer = _init.DbusInitializer(
                mainloop, bus, '/Initializer', args)
            _container_objects = _cm.DbusContainerManager(
                mainloop, bus, '/ContainerManager', args)
            logging.info("Merged the container and session control planes "
                         "into this process")

    try:
        if local_container:
            from tools.actions import container_manager as _cm
            _cm.do_start(args, session)
        else:
            tools.helpers.ipc.DBusContainerService(args).Start(session)
    except dbus.DBusException as e:
        logging.debug(e)
        if e.get_dbus_name().startswith("org.freedesktop.DBus.Python"):
            logging.error(e.get_dbus_message().splitlines()[-1])
        else:
            logging.error("WayDroid container is not listening")
        sys.exit(0)
    except Exception as e:
        logging.error(e)
        sys.exit(1)

    if tools.helpers.runtime.host_binder(args):
        services.user_manager.start(args, session, unlocked_cb)
        services.clipboard_manager.start(args)
        services.notification_manager.start(args, session)
    else:
        # Rootless backend without host binder: the gbinder-based services have
        # no service manager to talk to (see docs/rfc/rootless-backend.md).
        logging.warning("Skipping host integration services: Android's binder "
                        "is not reachable from the host with this backend")
        if unlocked_cb:
            unlocked_cb()
    service(args, mainloop)

    if container_initializer is not None \
            and container_initializer.worker_thread is not None:
        container_initializer.worker_thread.kill()
        container_initializer.worker_thread.join()

def do_stop(args, looper):
    services.user_manager.stop(args)
    services.clipboard_manager.stop(args)
    services.notification_manager.stop(args)
    looper.quit()

def stop(args):
    try:
        tools.helpers.ipc.DBusSessionService(args).Stop()
    except dbus.DBusException:
        stop_container(args, quit_session=True)

def stop_container(args, quit_session):
    if getattr(args, "_local_container", False):
        from tools.actions import container_manager as _cm
        _cm.stop(args, quit_session)
        return
    try:
        tools.helpers.ipc.DBusContainerService(args).Stop(quit_session)
    except dbus.DBusException:
        pass
