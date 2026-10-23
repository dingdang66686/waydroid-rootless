# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import os
import subprocess
from tools import helpers
from tools.helpers.version import versiontuple
import tools.config

def get_config(args):
    cfg = tools.config.load(args)
    args.arch = cfg["waydroid"].get("arch") or tools.config.defaults["arch"]
    args.images_path = (cfg["waydroid"].get("images_path")
                        or tools.config.defaults["images_path"])
    args.vendor_type = (cfg["waydroid"].get("vendor_type")
                        or tools.config.defaults["vendor_type"])
    args.system_ota = cfg["waydroid"].get("system_ota") or "None"
    args.vendor_ota = cfg["waydroid"].get("vendor_ota") or "None"
    args.session = None

def migration(args):
    try:
        old_ver = tools.helpers.props.file_get(args, args.work + "/waydroid_base.prop", "waydroid.tools_version")
        if versiontuple(old_ver) <= versiontuple("1.3.4"):
            chmod_paths = ["cache_http", "host-permissions", "lxc", "images", "rootfs", "data", "waydroid_base.prop", "waydroid.prop", "waydroid.cfg"]
            tools.helpers.run.user(args, ["chmod", "-R", "g-w,o-w"] + [os.path.join(args.work, f) for f in chmod_paths], check=False)
            tools.helpers.run.user(args, ["chmod", "g-w,o-w", args.work], check=False)
            os.remove(os.path.join(args.work, "session.cfg"))
        if versiontuple(old_ver) <= versiontuple("1.6.0"):
            # Because we now default adb to secure, disable auto_adb to avoid prompting the user on every session startup
            cfg = tools.config.load(args)
            cfg["waydroid"]["auto_adb"] = "False"
            tools.config.save(args, cfg)
    except Exception as e:
        logging.debug("Error during migration: %s", e)

def _upgrade_image(args):
    """Image mode: pull the published image (or skip with --offline) and
    restart the per-user service.  A failed pull leaves the running image and
    container untouched."""
    cfg = tools.config.load(args)
    image = cfg["waydroid"].get("podman_image") or ""
    remote = cfg["waydroid"].get("podman_image_remote") or image
    if not image:
        logging.error("podman_image is not set")
        return

    if not args.offline:
        if not remote:
            logging.error("podman_image_remote is not set")
            return
        logging.info("Pulling %s", remote)
        try:
            result = subprocess.run(["podman", "pull", remote],
                                    capture_output=True, text=True,
                                    timeout=600)
        except Exception as e:
            logging.error("podman pull failed: %s", e)
            return
        if result.returncode != 0:
            logging.error("podman pull %s failed: %s", remote,
                          (result.stderr or result.stdout).strip())
            logging.error("The running image is unchanged. Set "
                          "podman_image_remote to the published image, or "
                          "rebuild locally with image/build.sh and run "
                          "`waydroid upgrade --offline`.")
            return
        if remote != image:
            tagged = subprocess.run(["podman", "tag", remote, image],
                                    capture_output=True, text=True)
            if tagged.returncode != 0:
                logging.error("podman tag %s %s failed: %s", remote, image,
                              (tagged.stderr or tagged.stdout).strip())
                return
            logging.info("Tagged %s as %s", remote, image)

    if helpers.service.restart_user_service(args):
        logging.info("Restarted waydroid.service")
        return

    logging.warning("Could not restart waydroid.service; stopping the running "
                    "container. Start a new session with `waydroid session "
                    "start` after the image is ready.")
    from tools.actions import container_manager
    container_manager.stop(args)


def upgrade(args):
    backend = helpers.runtime.get(args)
    if getattr(backend, "uses_image", None) and backend.uses_image(args):
        _upgrade_image(args)
        return
    get_config(args)
    status = backend.status(args)
    if status != "STOPPED":
        logging.info("Stopping container")
        try:
            container = tools.helpers.ipc.DBusContainerService(args)
            args.session = container.GetSession()
            container.Stop(False)
        except Exception as e:
            logging.debug(e)
            tools.actions.container_manager.stop(args)
    migration(args)
    helpers.drivers.loadBinderNodes(args)
    if not args.offline:
        if args.images_path not in tools.config.defaults["preinstalled_images_paths"]:
            helpers.images.get(args)
        else:
            logging.info("Upgrade refused because Waydroid was configured to load pre-installed image from {}.".format(args.images_path))
    helpers.drivers.probeAshmemDriver(args)
    backend.setup_host(args)
    if status != "STOPPED":
        logging.info("Starting container")
        try:
            container.Start(args.session)
        except Exception as e:
            logging.debug(e)
            logging.error("Failed to restart container. Please do so manually.")
