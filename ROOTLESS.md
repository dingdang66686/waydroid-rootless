# Rootless podman backend for Waydroid

This repository integrates a rootless `podman`/`crun` backend into Waydroid.
It keeps Waydroid's CLI, D-Bus API, container/session interfaces, and Android
image format, but replaces the rootful LXC container with a rootless podman
container.

The runtime contract is deliberately small:

- one OCI image (`podman_image`), and
- one named podman volume mounted at `/data` (`podman_data_volume`),

plus the user's Wayland/Pulse/DRI sockets.  There is no external-rootfs mode,
no runtime overlay, and no per-run bind-mount of the Android `.rc` files.

## Requirements

- Linux kernel >= 5.13 for native rootless overlayfs (tested on 7.2).
- `podman` + `crun`, rootless; `pasta` for the container network.
- `/etc/subuid` and `/etc/subgid` with a **131072**-UID range, e.g.:

  ```text
  $USER:100000:131072
  ```

  The backend maps `0 → 1000`, `1 → 100000`, and `INT_MAX → 231071` inside the
  container.  A smaller range fails when Android creates its UIDs.
- A kernel with binderfs (`CONFIG_ANDROID_BINDERFS`), cgroup v2, and the usual
  Wayland/DRI/Pulse sockets.
- `sudo` for the install step (it installs the launcher/unit and disables the
  distro's rootful `waydroid-container.service`).

## Install

```sh
git clone https://github.com/dingdang66686/waydroid-rootless
cd waydroid-rootless

# The plain patched Android 13 image published by this project
# (if the GHCR package is private, run `podman login ghcr.io` first):
podman pull ghcr.io/dingdang66686/waydroid-rootless-image:13

./install-rootless.sh install
```

The installer:

- copies `tools/` and `waydroid.py` to `/opt/waydroid-rootless`;
- writes `/usr/local/bin/waydroid` (root keeps using the distro binary);
- creates `~/.local/state/waydroid/waydroid.cfg` with
  `container_backend = podman`,
  `podman_image = ghcr.io/dingdang66686/waydroid-rootless-image:13`,
  `podman_data_volume = waydroid-data`;
- installs the per-user `waydroid.service` (`waydroid session start`);
- disables the rootful `waydroid-container.service` if present.

Use a different image/tag with `WAYDROID_IMAGE=... ./install-rootless.sh install`
(for example your own image with an ARM translation overlay).

Rollback:

```sh
./install-rootless.sh uninstall
```

The distro package and `/var/lib/waydroid/data` are left intact.

## Image and ARM note

The published image is the **plain patched Waydroid Android 13 rootfs**
(`x86_64`).  It does not include `libndk_translation`/`houdini`:

- the ARM translation layer is an external overlay, and
- `podman_image` can point at an image you derive from the published one.

The image build is in
[waydroid-rootless-image](https://github.com/dingdang66686/waydroid-rootless-image)
(see `BUILD.md` there); its GitHub Actions workflow publishes
`ghcr.io/dingdang66686/waydroid-rootless-image:13`.

## Local overlay (startup rebuild)

The application supports a private local file-tree overlay, checked at every
container start (including `container restart`; there is no watcher):

```text
$WORK/overlay/
  system/...             files added/replaced under /system
  vendor/...             files added/replaced under /vendor
  ...                    any other rootfs-relative path
```

Only basic file-tree semantics are supported: files, directories and symlinks
are added or replaced.  Use full files (for example
`overlay/system/build.prop`).  `props/`, `remove.list`, `.wh.` whiteouts and the
old `rootfs/` layout are not supported; deletions are not possible with a COPY
layer.

A basic overlay is built as a **COPY-only layer** on the configured base:

```text
podman build FROM <podman_image> COPY <overlay> /
```

Base layers stay shared in containers/storage, so the derived image only adds
the overlay layer (the ARM layer is about 45 MB); the base is not exported and
there is no second flattened copy.

On start the backend hashes the overlay and compares it with
`$WORK/podman-overlay.stamp`, together with the base image ID and derived image
ID.  If nothing changed it reuses the derived image.  Otherwise it rebuilds the
layer.  `mount_overlays = False` disables the feature.  The configured
`podman_image` remains the clean base; only the running session uses the derived
tag.  A failed rebuild is a hard error instead of silently booting without the
compatibility layer.

Optional config keys:

```ini
podman_overlay_path = /home/you/.local/state/waydroid/overlay
podman_overlay_image = localhost/waydroid-overlay:13
```

The overlay and derived image are local/private: do not commit proprietary
prebuilts (for example the ARM translation layer) or push a derived image
containing them.

## Image-mode commands

- `waydroid init`: pull `podman_image_remote`, tag it as `podman_image`, prepare
  the named volume/host props, and start the user service.  A failed pull leaves
  a running container untouched.
- `waydroid upgrade`: pull `podman_image_remote`, tag, and restart the user
  service; `--offline` skips the pull and only restarts.
- `/data` is preserved across both.  Like upstream, the backend does not
  inspect, stamp, warn about, or wipe it.

## Status

The backend is tested end-to-end on the development host: the user unit starts,
`sys.boot_completed=1`, ADB works over the loopback publish, `app
install/list/remove`, `prop get/set`, `show-full-ui`, and
`container freeze/unfreeze/restart/stop` work.  The four hard points and the
remaining system gaps are documented in
[`docs/rfc/rootless-backend.md`](docs/rfc/rootless-backend.md):

- the 131072-UID submap;
- podman's unconditional `--config-net` (the pasta wrapper filters it);
- the `.rc`/property image overrides;
- cgroup, mknod, BPF, and iptables gaps.

Sensors are a stub (`waydroid-sensord`/sensorfw is not installed); this matches
the pre-switch setup on the test host.  The backend remains a prototype, not an
upstream-ready patch series.

## License

The Waydroid tree and these changes are GPL-3.0-only; see `LICENSE`.
