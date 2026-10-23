# Copyright 2026 The Waydroid contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Local file-tree overlay -> derived OCI image, checked at session start.

The public image stays clean.  A user may keep a local overlay tree in
``$WORK/overlay`` (or ``podman_overlay_path``)::

    overlay/
      system/...           files added/replaced under /system
      vendor/...           files added/replaced under /vendor
      ...                  other rootfs-relative paths

Only basic file-tree semantics are supported: files, directories and symlinks
are added or replaced.  Deletions, whiteouts and property merging are not part
of this mechanism; edit the full file (for example ``overlay/system/build.prop``)
or change the base image instead.

A basic overlay is built with a COPY-only ``podman build`` layer on top of the
configured ``podman_image``.  containers/storage keeps the base layers shared;
the derived image only adds the overlay layer (about 45 MB for the ARM layer).
There is no base export and no flattened image.

On every container start the overlay fingerprint is compared with the last
build stamp.  If the overlay, the base image, or the derived tag changed, the
layer is rebuilt; otherwise the cached image is reused.  There is no watcher:
changes take effect on the next start.

This module is deliberately local/private: it does not push anything.
"""

import fcntl
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
from datetime import datetime, timezone

SCHEMA = 2
DEFAULT_IMAGE_PREFIX = "localhost/waydroid-overlay"


def _image_exists(image):
    result = subprocess.run(["podman", "image", "exists", image],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    return result.returncode == 0


def _image_id(image):
    result = subprocess.run(["podman", "image", "inspect", "-f", "{{.Id}}", image],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("podman image {} not found".format(image))
    return result.stdout.strip()


def _overlay_path(args, cfg):
    path = (cfg["waydroid"].get("podman_overlay_path")
            or os.path.join(args.work, "overlay"))
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(args.work, path)
    return os.path.normpath(path)


def _overlay_image(args, cfg, base):
    image = cfg["waydroid"].get("podman_overlay_image") or ""
    if image:
        return image
    last = base.rsplit("/", 1)[-1]
    if "@" not in last and ":" in last:
        tag = last.rsplit(":", 1)[1]
    else:
        tag = "latest"
    return "{}:{}".format(DEFAULT_IMAGE_PREFIX, tag)


def _validate_tree(path):
    """Reject the removed extension syntax instead of silently layering it."""
    for name, label in (("props", "props/ property merging"),
                        ("rootfs", "the legacy rootfs/ layout"),
                        ("remove.list", "remove.list deletion support")):
        if os.path.lexists(os.path.join(path, name)):
            raise RuntimeError(
                "{} is not supported; use a basic file tree with full files "
                "under system/ and vendor/".format(label))
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            if name == ".wh..wh..opq" or name.startswith(".wh."):
                raise RuntimeError(
                    "whiteout entries are not supported; only basic file-tree "
                    "overlays are available")


def _active(path):
    """True when the tree contains at least one file or symlink.

    Empty directories alone are ignored (upstream auto-creates overlay/vendor
    empty); a plain file tree with content is active.
    """
    if not os.path.isdir(path):
        return False
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames:
            if os.path.islink(os.path.join(dirpath, name)):
                return True
        if filenames:
            return True
    return False


def _hash_file(h, path):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)


def _hash_tree(h, root):
    def walk(directory, rel):
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            name = entry.name
            entry_rel = rel + "/" + name if rel else name
            st = entry.stat(follow_symlinks=False)
            h.update(entry_rel.encode())
            h.update(b"\0")
            h.update(str(stat.S_IMODE(st.st_mode)).encode())
            h.update(b"\0")
            if entry.is_symlink():
                h.update(b"L\0")
                h.update(os.readlink(entry.path).encode())
                h.update(b"\0")
            elif entry.is_dir(follow_symlinks=False):
                h.update(b"D\0")
                walk(entry.path, entry_rel)
            elif entry.is_file(follow_symlinks=False):
                h.update(b"F\0")
                h.update(str(st.st_size).encode())
                h.update(b"\0")
                _hash_file(h, entry.path)
            else:
                h.update(b"X\0")
    walk(root, "")


def _fingerprint(path):
    if not _active(path):
        return None
    h = hashlib.sha256()
    h.update(b"podman-overlay-layer-v2\0")
    _hash_tree(h, path)
    return h.hexdigest()


def _fix_permissions(root):
    subprocess.run(["find", root, "-xdev", "-type", "f", "!", "-perm", "-o=r",
                    "-exec", "chmod", "a+rX", "{}", "+"], check=True)
    subprocess.run(["find", root, "-xdev", "-type", "d", "!", "-perm", "-o=x",
                    "-exec", "chmod", "a+X", "{}", "+"], check=True)


def _overlay_labels(image):
    tag = image.rsplit(":", 1)[-1] if ":" in image else "latest"
    return [
        "org.opencontainers.image.source="
        "https://github.com/dingdang66686/waydroid-rootless",
        "org.opencontainers.image.title=waydroid-rootless local overlay",
        "org.opencontainers.image.description="
        "Local overlay of the rootless Waydroid rootfs",
        "org.opencontainers.image.version={}".format(tag),
        "org.waydroid.rootless.overlay=true",
    ]


def _stamp_path(args):
    return os.path.join(args.work, "podman-overlay.stamp")


def _read_stamp(args):
    try:
        with open(_stamp_path(args), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_stamp(args, stamp):
    path = _stamp_path(args)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _stamp_valid(stamp, fingerprint, overlay, base, base_id, image):
    if not stamp or stamp.get("schema") != SCHEMA:
        return False
    for key, expected in (
            ("fingerprint", fingerprint),
            ("overlay", overlay),
            ("base_image", base),
            ("base_image_id", base_id),
            ("image", image)):
        if stamp.get(key) != expected:
            return False
    image_id = stamp.get("image_id")
    if not image_id or image_id == base_id or not _image_exists(image):
        return False
    return _image_id(image) == image_id


def _cleanup_old_image(old_id, image_id, base_id):
    if old_id and old_id != image_id and old_id != base_id:
        # Drop the previous derived image when possible; a container still
        # using it makes podman refuse, which is fine.
        subprocess.run(["podman", "rmi", old_id],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


def _build_layer(args, overlay, base, image, fingerprint, base_id):
    tmp = os.path.join(args.work, "tmp",
                       "podman-overlay-{}".format(os.getpid()))
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    context = os.path.join(tmp, "context")
    containerfile = os.path.join(tmp, "Containerfile")
    old_id = _image_id(image) if _image_exists(image) else None
    try:
        # Stage a private context for the same a+rX fix-up without mutating the
        # user overlay.  Only the overlay tree is copied; the base is not.
        logging.info("[overlay] staging COPY layer")
        shutil.copytree(overlay, context, symlinks=True)
        _fix_permissions(context)
        with open(containerfile, "w", encoding="utf-8") as f:
            f.write("FROM {}\nCOPY . /\n".format(base))
        cmd = ["podman", "build", "--pull=never", "--layers",
               "--network=none", "-f", containerfile, "-t", image]
        for label in _overlay_labels(image):
            cmd += ["--label", label]
        cmd.append(context)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError("podman build failed: {}".format(
                result.stderr.strip() or result.stdout.strip()))
        image_id = _image_id(image)
        _cleanup_old_image(old_id, image_id, base_id)
        _write_stamp(args, {
            "schema": SCHEMA,
            "fingerprint": fingerprint,
            "overlay": overlay,
            "base_image": base,
            "base_image_id": base_id,
            "image": image,
            "image_id": image_id,
            "built": datetime.now(timezone.utc).isoformat(),
        })
        logging.info("[overlay] built layer %s (%s)", image, image_id)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure(args, cfg):
    """Return the derived image to boot, or None when no overlay is active.

    The check runs at every container start (including ``container restart``).
    A failed rebuild is a hard error: silently booting the base image would
    lose the user's compatibility layer.
    """
    if str(cfg["waydroid"].get("mount_overlays", "True")).lower() not in ("true", "1"):
        logging.info("[overlay] mount_overlays is disabled; using base image")
        return None

    path = _overlay_path(args, cfg)
    if os.path.isdir(path):
        _validate_tree(path)
    if not _active(path):
        return None

    base = cfg["waydroid"].get("podman_image") or ""
    if not base:
        raise RuntimeError("podman_image is required before building an overlay")
    image = _overlay_image(args, cfg, base)
    if image == base:
        raise RuntimeError(
            "podman_overlay_image must differ from podman_image ({})".format(image))

    lock_path = os.path.join(args.work, "podman-overlay.lock")
    os.makedirs(args.work, exist_ok=True)
    os.makedirs(os.path.join(args.work, "tmp"), exist_ok=True)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for _attempt in range(3):
            fingerprint = _fingerprint(path)
            if fingerprint is None:
                return None
            base_id = _image_id(base)
            stamp = _read_stamp(args)
            if _stamp_valid(stamp, fingerprint, path, base, base_id, image):
                logging.info("[overlay] unchanged; using %s", image)
                return image
            logging.info("[overlay] overlay changed or image missing; rebuilding %s",
                         image)
            _build_layer(args, path, base, image, fingerprint, base_id)
            if _fingerprint(path) == fingerprint and _image_id(base) == base_id:
                return image
            logging.warning("[overlay] overlay or base changed during build; retrying")
    raise RuntimeError("overlay changed repeatedly while building {}".format(image))
