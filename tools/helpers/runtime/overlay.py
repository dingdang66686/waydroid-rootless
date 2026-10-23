# Copyright 2026 The Waydroid contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Local rootfs overlay -> derived OCI image, checked at session start.

The public image stays clean.  The primary layout matches upstream Waydroid's
persistent overlay lowerdir in ``$WORK/overlay`` (or ``podman_overlay_path``)::

    overlay/
      system/...           files added/replaced under /system
      vendor/...           files added/replaced under /vendor
      ...                  other rootfs-relative paths

Extensions (ignored by an upstream LXC runtime, so an existing overlay tree
keeps working)::

      props/               property fragments merged into rootfs files
      remove.list          optional root-relative paths to delete
      rootfs/              legacy/self-contained tree from the first iteration

Full-file overrides such as ``overlay/system/build.prop`` follow upstream
overlay semantics; ``props/`` is a convenience merge on top.  ``mount_overlays
= False`` (the upstream switch) disables the whole feature.

On every container start the overlay fingerprint is compared with the last
build stamp.  If the overlay, the base image, or the derived tag changed, the
base image is exported without booting it, the overlay is applied, and the
result is imported as ``podman_overlay_image``.  Otherwise the cached derived
image is reused.  There is no watcher: changes take effect on the next start.

This module is deliberately local/private: it does not push anything and the
overlay directory is not part of the public image build.
"""

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from datetime import datetime, timezone

SCHEMA = 1
DEFAULT_IMAGE_PREFIX = "localhost/waydroid-overlay"
METADATA_TOP = ("props", "remove.list")


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


def _tree(path):
    """Upstream uses $WORK/overlay itself as the lowerdir.  Keep accepting the
    earlier rootfs/ child as a self-contained tree, but prefer upstream."""
    legacy = os.path.join(path, "rootfs")
    if os.path.isdir(legacy):
        return legacy, True
    return path, False


def _has_payload(root, legacy):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        top = os.path.realpath(dirpath) == os.path.realpath(root)
        for name in list(dirnames):
            if os.path.islink(os.path.join(dirpath, name)):
                return True
        if not legacy and top:
            filenames = [name for name in filenames if name not in METADATA_TOP]
        if filenames:
            return True
    return False


def _active(path):
    tree, legacy = _tree(path)
    if _has_payload(tree, legacy):
        return True
    props = os.path.join(path, "props")
    if os.path.isdir(props):
        for _dirpath, _dirnames, filenames in os.walk(props, followlinks=False):
            if filenames:
                return True
    remove = os.path.join(path, "remove.list")
    if os.path.isfile(remove):
        with open(remove, encoding="utf-8", errors="ignore") as f:
            if any(line.strip() and not line.lstrip().startswith("#")
                   for line in f):
                return True
    return False


def _hash_file(h, path):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)


def _hash_tree(h, root, prefix, exclude_top=()):
    def walk(directory, rel, top):
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if top and name in exclude_top:
                continue
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
                walk(entry.path, entry_rel, False)
            elif entry.is_file(follow_symlinks=False):
                h.update(b"F\0")
                h.update(str(st.st_size).encode())
                h.update(b"\0")
                _hash_file(h, entry.path)
            else:
                h.update(b"X\0")
    walk(root, prefix, True)


def _fingerprint(path):
    if not _active(path):
        return None
    tree, legacy = _tree(path)
    props = os.path.join(path, "props")
    remove = os.path.join(path, "remove.list")
    h = hashlib.sha256()
    h.update(b"podman-overlay-v2\0")
    if _has_payload(tree, legacy):
        prefix = "rootfs" if legacy else "overlay"
        h.update(prefix.encode() + b"\0")
        _hash_tree(h, tree, prefix, () if legacy else METADATA_TOP)
    if os.path.isdir(props):
        h.update(b"props\0")
        _hash_tree(h, props, "props")
    if os.path.isfile(remove):
        h.update(b"remove.list\0")
        _hash_file(h, remove)
    return h.hexdigest()


def _remove_path(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def _apply_remove_list(root, overlay):
    path = os.path.join(overlay, "remove.list")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8", errors="ignore") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if (os.path.isabs(line) or line in (".", "")
                    or line == ".." or line.startswith("../")
                    or "/../" in "/" + line + "/"):
                raise RuntimeError("remove.list:{}: invalid path {!r}".format(
                    lineno, line))
            rel = os.path.normpath(line)
            target = os.path.join(root, rel)
            if os.path.islink(target) or os.path.exists(target):
                _remove_path(target)
                logging.info("[overlay] removed %s", rel)
            else:
                logging.warning("[overlay] remove.list:%s: %s is not present",
                                lineno, rel)


def _copy_symlink(src, dst):
    if os.path.islink(dst) or os.path.exists(dst):
        _remove_path(dst)
    os.symlink(os.readlink(src), dst)


def _copy_file(src, dst):
    if os.path.islink(dst) or os.path.exists(dst):
        _remove_path(dst)
    shutil.copyfile(src, dst)
    shutil.copymode(src, dst)


def _apply_tree(src, dst, root, exclude_top=(), top=True):
    if not os.path.isdir(src):
        return
    try:
        entries = sorted(os.scandir(src), key=lambda e: e.name)
    except OSError:
        return
    if top and exclude_top:
        entries = [e for e in entries if e.name not in exclude_top]
    opaque = any(e.name == ".wh..wh..opq" for e in entries)
    if opaque:
        if os.path.realpath(dst) == os.path.realpath(root):
            raise RuntimeError("opaque overlay of the rootfs root is not supported")
        for child in os.listdir(dst):
            _remove_path(os.path.join(dst, child))
        logging.info("[overlay] opaque %s", os.path.relpath(dst, root))

    for entry in entries:
        name = entry.name
        if name == ".wh..wh..opq":
            continue
        if name.startswith(".wh."):
            target = os.path.join(dst, name[4:])
            if os.path.islink(target) or os.path.exists(target):
                _remove_path(target)
            logging.info("[overlay] whiteout %s",
                         os.path.relpath(target, root))
            continue

        target = os.path.join(dst, name)
        if entry.is_symlink():
            _copy_symlink(entry.path, target)
        elif entry.is_dir(follow_symlinks=False):
            if os.path.islink(target) or (os.path.exists(target)
                                          and not os.path.isdir(target)):
                _remove_path(target)
            os.makedirs(target, exist_ok=True)
            _apply_tree(entry.path, target, root, top=False)
            shutil.copymode(entry.path, target)
        elif entry.is_file(follow_symlinks=False):
            _copy_file(entry.path, target)
        else:
            raise RuntimeError("unsupported file type in overlay: {}".format(
                entry.path))


def _apply_props(root, overlay):
    props = os.path.join(overlay, "props")
    if not os.path.isdir(props):
        return
    for dirpath, dirnames, filenames in os.walk(props, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            src = os.path.join(dirpath, name)
            if os.path.islink(src):
                raise RuntimeError("props entries must not be symlinks: {}".format(src))
            rel = os.path.relpath(src, props)
            target = os.path.join(root, rel)
            if os.path.islink(target) or (os.path.exists(target)
                                          and not os.path.isfile(target)):
                raise RuntimeError(
                    "property target is not a regular file: {}".format(target))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            existed = os.path.isfile(target)
            lines = []
            if existed:
                with open(target, encoding="utf-8", errors="ignore") as f:
                    lines = f.read().splitlines()
            changes = {}
            with open(src, encoding="utf-8", errors="ignore") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.rstrip("\r")
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    key, sep, value = line.partition("=")
                    key = key.strip()
                    if not sep or not key:
                        raise RuntimeError("{}:{}: expected key=value".format(
                            src, lineno))
                    lines = [old for old in lines
                             if not re.match(r"^\s*" + re.escape(key) + r"\s*=",
                                             old)]
                    lines.append("{}={}".format(key, value))
                    changes[key] = value
            if not changes and not existed:
                continue
            with open(target, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            if not existed:
                os.chmod(target, 0o644)
            logging.info("[overlay] merged %s into %s", src, rel)
            for key, value in changes.items():
                logging.info("[overlay]   %s=%s", key, value)


def _fix_permissions(root):
    subprocess.run(["find", root, "-xdev", "-type", "f", "!", "-perm", "-o=r",
                    "-exec", "chmod", "a+rX", "{}", "+"], check=True)
    subprocess.run(["find", root, "-xdev", "-type", "d", "!", "-perm", "-o=x",
                    "-exec", "chmod", "a+X", "{}", "+"], check=True)


def _extract_base(base, root, tmp):
    tar = os.path.join(tmp, "base.tar")
    container = "waydroid-overlay-build-{}-{}".format(
        os.getpid(), int(time.time() * 1000))
    subprocess.run(["podman", "rm", "-f", container],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        logging.info("[overlay] exporting base image %s", base)
        subprocess.run(["podman", "create", "--name", container, base,
                        "/system/bin/true"], check=True)
        subprocess.run(["podman", "export", "-o", tar, container], check=True)
    finally:
        subprocess.run(["podman", "rm", "-f", container],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.makedirs(root, exist_ok=True)
    subprocess.run(["tar", "--no-same-owner",
                    "--exclude=dev/*", "--exclude=./dev/*",
                    "-xf", tar, "-C", root], check=True)
    os.unlink(tar)

    # podman's export adds a container marker and possibly device nodes; the
    # Android rootfs has an empty /dev and the backend supplies it at runtime.
    marker = os.path.join(root, "run", ".containerenv")
    if os.path.lexists(marker):
        os.unlink(marker)
    dev = os.path.join(root, "dev")
    os.makedirs(dev, exist_ok=True)
    for name in os.listdir(dev):
        _remove_path(os.path.join(dev, name))


def _write_tar(root, tar):
    subprocess.run(["tar", "--sort=name", "--numeric-owner",
                    "--owner=0", "--group=0", "--mtime=@0",
                    "-C", root, "-cf", tar, "."], check=True)


def _import(tar, image):
    tag = image.rsplit(":", 1)[-1] if ":" in image else "latest"
    changes = [
        "--change", "LABEL org.opencontainers.image.source="
                    "https://github.com/dingdang66686/waydroid-rootless",
        "--change", "LABEL org.opencontainers.image.title="
                    "waydroid-rootless local overlay",
        "--change", "LABEL org.opencontainers.image.description="
                    "Local overlay of the rootless Waydroid rootfs",
        "--change", "LABEL org.opencontainers.image.version={}".format(tag),
        "--change", "LABEL org.waydroid.rootless.overlay=true",
    ]
    result = subprocess.run(["podman", "import"] + changes + [tar, image],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("podman import failed: {}".format(
            result.stderr.strip() or result.stdout.strip()))


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


def _build(args, overlay, base, image, fingerprint, base_id):
    tmp = os.path.join(args.work, "tmp",
                       "podman-overlay-{}".format(os.getpid()))
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    root = os.path.join(tmp, "rootfs")
    tar = os.path.join(tmp, "rootfs.tar")
    old_id = _image_id(image) if _image_exists(image) else None
    try:
        _extract_base(base, root, tmp)
        tree, legacy = _tree(overlay)
        _apply_remove_list(root, overlay)
        _apply_tree(tree, root, root, () if legacy else METADATA_TOP)
        _apply_props(root, overlay)
        _fix_permissions(root)
        logging.info("[overlay] writing %s", tar)
        _write_tar(root, tar)
        _import(tar, image)
        image_id = _image_id(image)
        if old_id and old_id != image_id and old_id != base_id:
            # Drop the previous derived image when possible; a container
            # still using it makes podman refuse, which is fine.
            subprocess.run(["podman", "rmi", old_id],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
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
        logging.info("[overlay] built %s (%s)", image, image_id)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure(args, cfg):
    """Return the derived image to boot, or None when no overlay is active.

    The check runs at every container start (including ``container restart``).
    An active overlay that cannot be rebuilt is a hard error: silently booting
    the base image would lose the user's compatibility layer.
    """
    if str(cfg["waydroid"].get("mount_overlays", "True")).lower() not in ("true", "1"):
        logging.info("[overlay] mount_overlays is disabled; using base image")
        return None

    path = _overlay_path(args, cfg)
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
            _build(args, path, base, image, fingerprint, base_id)
            if _fingerprint(path) == fingerprint and _image_id(base) == base_id:
                return image
            logging.warning("[overlay] overlay or base changed during build; retrying")
    raise RuntimeError("overlay changed repeatedly while building {}".format(image))
