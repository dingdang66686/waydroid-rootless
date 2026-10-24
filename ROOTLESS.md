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

Add the package repository to `/etc/pacman.conf` before `[core]`/`[extra]`:

```ini
[waydroid-rootless]
Server = https://dingdang66686.github.io/waydroid-rootless-arch/$arch
SigLevel = Required DatabaseRequired
```

Import and trust the repository key:

```sh
sudo pacman-key --add https://dingdang66686.github.io/waydroid-rootless-arch/x86_64/waydroid-rootless.asc
sudo pacman-key --lsign-key B5B9D60481516AFE4E2DC8931F298A3142A4810C
```

Then:

```sh
podman pull ghcr.io/dingdang66686/waydroid-rootless-image:13
sudo pacman -Sy
sudo pacman -S waydroid
systemctl --user enable --now waydroid.service
```

On Arch, this package replaces the distro `waydroid` package, installs the
rootless launcher and user unit, disables the rootful
`waydroid-container.service`, and keeps `/data` in the named volume.

## Image

The published image is the plain Waydroid Android 13 rootfs.  The builder is
[waydroid-rootless-image](https://github.com/dingdang66686/waydroid-rootless-image);
its workflow publishes `ghcr.io/dingdang66686/waydroid-rootless-image:13`.

## Local overlay

A local file-tree overlay is supported at `$WORK/overlay`:

```text
$WORK/overlay/
  system/...
  vendor/...
  ...                    # rootfs-relative paths
```

It is copied as a layer on top of `podman_image` at container start.  If nothing
changed the cached derived image is reused; otherwise the layer is rebuilt.
Optional config:

```ini
podman_overlay_path = /home/you/.local/state/waydroid/overlay
podman_overlay_image = localhost/waydroid-overlay:13
```

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
