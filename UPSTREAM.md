# Upstream

This repository is a Waydroid tree with the rootless podman backend integrated.

- Upstream: <https://github.com/waydroid/waydroid>
- Base commit: `db7294c4c2bd43157bab94cf8a3015e22d9f1ad8`
- Branch: `main` (the rootless integration commit on top of the upstream base)
- Integration diff: [`rootless-backend.patch`](rootless-backend.patch)

The source changes are confined to the CLI/helpers (`tools/`), the config
defaults, and the session unit.  The image build lives in the separate
[waydroid-rootless-image](https://github.com/dingdang66686/waydroid-rootless-image)
repository; this repository never imports or extracts `system.img`/`vendor.img`.

See [ROOTLESS.md](ROOTLESS.md) for install/usage and
[`docs/rfc/rootless-backend.md`](docs/rfc/rootless-backend.md) for the design,
feasibility evidence, and system gaps.
