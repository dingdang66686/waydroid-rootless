# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import os
import shutil
import time
import tools.config
import tools.helpers.props
import tools.helpers.ipc
from tools.interfaces import IPlatform
from tools.interfaces import IStatusBarService
import dbus

def install(args):
    try:
        tools.helpers.ipc.DBusSessionService(args)

        cm = tools.helpers.ipc.DBusContainerService(args)
        session = cm.GetSession()
        if session["state"] == "FROZEN":
            cm.Unfreeze()

        backend = tools.helpers.runtime.get(args)
        push = getattr(backend, "push_apk", None)
        if push:
            apk_path = push(args, args.PACKAGE)
        else:
            tmp_dir = (session.get("waydroid_data")
                       or tools.config.session_defaults["waydroid_data"]) + "/waydroid_tmp"
            if not os.path.exists(tmp_dir):
                os.makedirs(tmp_dir)
            shutil.copyfile(args.PACKAGE, tmp_dir + "/base.apk")
            apk_path = "/data/waydroid_tmp/base.apk"

        platformService = IPlatform.get_service(args)
        if platformService:
            platformService.installApp(apk_path)
        else:
            logging.error("Failed to access IPlatform service")

        if push:
            remove = getattr(backend, "remove_apk", None)
            if remove:
                remove(args, apk_path)
        else:
            os.remove(tmp_dir + "/base.apk")

        if session["state"] == "FROZEN":
            cm.Freeze()
    except (dbus.DBusException, KeyError):
        logging.error("WayDroid session is stopped")

def remove(args):
    try:
        tools.helpers.ipc.DBusSessionService(args)

        cm = tools.helpers.ipc.DBusContainerService(args)
        session = cm.GetSession()
        if session["state"] == "FROZEN":
            cm.Unfreeze()

        platformService = IPlatform.get_service(args)
        ret = None
        if platformService:
            ret = platformService.removeApp(args.PACKAGE)
        if ret != 0:
            # IPlatform.removeApp returns -1 on some images even though
            # PackageManager can uninstall the package.
            fallback = getattr(tools.helpers.runtime.get(args),
                               "remove_app", None)
            if fallback is not None and fallback(args, args.PACKAGE) == 0:
                ret = 0
                logging.info("Uninstalled {} via the runtime shell".format(
                    args.PACKAGE))
        if ret != 0:
            logging.error("Failed to uninstall package: {}".format(args.PACKAGE))

        if session["state"] == "FROZEN":
            cm.Freeze()
    except dbus.DBusException:
        logging.error("WayDroid session is stopped")

def maybeLaunchLater(args, launchNow):
    try:
        tools.helpers.ipc.DBusSessionService(args)
        try:
            tools.helpers.ipc.DBusContainerService(args).Unfreeze()
        except Exception:
            logging.error("Failed to unfreeze container. Trying to launch anyways...")
        launchNow()
    except dbus.DBusException:
        logging.error("Starting waydroid session")
        tools.actions.session_manager.start(args, launchNow, background=False)

def launch(args):
    def justLaunch():
        platformService = IPlatform.get_service(args)
        if platformService:
            platformService.setprop("waydroid.active_apps", args.PACKAGE)
            platformService.launchApp(args.PACKAGE)
            multiwin = platformService.getprop(
                "persist.waydroid.multi_windows", "false")
            if multiwin == "false":
                platformService.settingsPutString(
                    2, "policy_control", "immersive.status=*")
            else:
                platformService.settingsPutString(
                    2, "policy_control", "immersive.full=*")
        else:
            logging.error("Failed to access IPlatform service")
    maybeLaunchLater(args, justLaunch)

def list(args):
    try:
        tools.helpers.ipc.DBusSessionService(args)

        cm = tools.helpers.ipc.DBusContainerService(args)
        session = cm.GetSession()
        if session["state"] == "FROZEN":
            cm.Unfreeze()

        platformService = IPlatform.get_service(args)
        if platformService:
            appsList = platformService.getAppsInfo()
            for app in appsList:
                print("Name: " + app["name"])
                print("packageName: " + app["packageName"])
                print("categories:")
                for cat in app["categories"]:
                    print("\t" + cat)
        else:
            logging.error("Failed to access IPlatform service")

        if session["state"] == "FROZEN":
            cm.Freeze()
    except dbus.DBusException:
        logging.error("WayDroid session is stopped")

def showFullUI(args):
    def justShow():
        platformService = IPlatform.get_service(args)
        if platformService:
            platformService.setprop("waydroid.active_apps", "Waydroid")
            platformService.settingsPutString(2, "policy_control", "null*")
            # HACK: Refresh display contents
            statusBarService = IStatusBarService.get_service(args)
            if statusBarService:
                statusBarService.expand()
                time.sleep(0.5)
                statusBarService.collapse()
        else:
            logging.error("Failed to access IPlatform service")
    maybeLaunchLater(args, justShow)

def intent(args):
    def justLaunch():
        platformService = IPlatform.get_service(args)
        if platformService:
            ret = platformService.launchIntent(args.ACTION, args.URI)
            if ret == "":
                return
            pkg = ret if ret != "android" else "Waydroid"
            platformService.setprop("waydroid.active_apps", pkg)
            multiwin = platformService.getprop(
                "persist.waydroid.multi_windows", "false")
            if multiwin == "false":
                platformService.settingsPutString(
                    2, "policy_control", "immersive.status=*")
            else:
                platformService.settingsPutString(
                    2, "policy_control", "immersive.full=*")
        else:
            logging.error("Failed to access IPlatform service")
    maybeLaunchLater(args, justLaunch)
