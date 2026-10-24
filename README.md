<img align="left" src="data/AppIcon.png" width="64">

# Waydroid

> **Rootless podman backend.**  This is a fork of
> [waydroid/waydroid](https://github.com/waydroid/waydroid) carrying the
> rootless backend documented in [ROOTLESS.md](ROOTLESS.md).
>
> The bootable OCI image builder is
> [dingdang66686/waydroid-rootless-image](https://github.com/dingdang66686/waydroid-rootless-image),
> and the published image is
> `ghcr.io/dingdang66686/waydroid-rootless-image:13`:
>
> The Arch package repository is
> [dingdang66686/waydroid-rootless-arch](https://github.com/dingdang66686/waydroid-rootless-arch).
> Once the repository is configured:
>
> ```sh
> podman pull ghcr.io/dingdang66686/waydroid-rootless-image:13
> sudo pacman -S waydroid
> systemctl --user enable --now waydroid.service
> ```
>
> The package replaces the distro `waydroid` package (Arch only).
>
> Local overlays are supported; see `ROOTLESS.md`.

Waydroid uses a container-based approach to boot a full Android system on a
regular GNU/Linux system.

## Overview

Waydroid uses Linux namespaces (user, pid, uts, net, mount, ipc) to run a
full Android system in a container and provide Android applications on
any GNU/Linux-based platform.

The Android system inside the container has direct access to any needed hardware.

The Android runtime environment ships with a minimal customized Android system
image based on [LineageOS](https://lineageos.org/). The image is currently based
on Android 13.

## Install

See install instructions [here](https://docs.waydro.id/usage/install-on-desktops)

## Documentation

Our documentation can be found at [docs.waydro.id](https://docs.waydro.id)

## Reporting bugs

If you have found an issue with Waydroid, please [file a bug](https://github.com/Waydroid/waydroid/issues/new/choose).

## Get in Touch

If you want to get in contact with the developers please feel free to join the
*Waydroid* groups in [Matrix](https://matrix.to/#/#waydroid:matrix.org) or [Telegram](https://t.me/WayDroid).

Our website can be found at [waydro.id](https://waydro.id/)
