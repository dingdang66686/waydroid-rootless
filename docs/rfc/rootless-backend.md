# RFC: A rootless container backend for Waydroid

* **Status:** Draft, for discussion. This document is a feasibility + migration
  plan, not a patch series.
* **Scope:** turn "run Waydroid without root" from an out-of-tree hack into a
  supported, selectable backend in `waydroid/waydroid`.
* **Upstream baseline read for this document:** `waydroid/waydroid` @
  `db7294c` ("dbus: Drop -w from the container service's Exec"); installed copy
  compared against `1.6.3-1`.
* **Prototype / evidence:** a working out-of-tree implementation
  (`waydroid-rootless/`: one `run.sh` plus a ~450-line `devstage.c`) that boots
  the stock, already-installed Android 13 image to
  `sys.boot_completed=1` under rootless podman, with network, rendering, input
  and audio working. Everything marked *(verified)* below was measured on that
  prototype. §A.1 lists the exact host/toolchain.

---

## 0. TL;DR

1. **Feasible today, without root and without LXC.** A rootless podman
   container running the unmodified Waydroid Android image reaches
   `sys.boot_completed=1` in 10–14 s, gets a working IPv4/IPv6/DNS network
   through pasta + Android's own `netd`, draws at 2160×1302, and receives
   Wayland input *(verified)*.
2. **The hard parts are not containerization.** They are (a) a handful of
   kernel checks that are against the *initial* user namespace (eBPF, `mknod`,
   `SO_RCVBUFFORCE`, `setpriority(-20)`, `/proc/cmdline` chmod), (b) podman
   forcing `--config-net` on pasta, and (c) upstream's assumption that the
   container is a **root system service**.
3. **The UID/GID question is smaller than it looks.** netd needs the container's
   0..`INT_MAX` UID range to be mapped, but a usable map needs only a
   **~131072-wide subordinate ID block** (`/etc/subuid`), and `--uidmap` for
   rootless podman is written in **intermediate** IDs, not host IDs. No 2 GiB
   subuid block and no emulated single UID are required. *(verified; see §6.2)*
4. **Recommended shape:** a *backend interface* in upstream
   (`tools/helpers/runtime/{lxc,podman}.py` + a `container_backend` config key),
   LXC stays the default, `podman` is opt-in and user-level. This is a
   refactor-first migration: most of it is testable without a rootless host.
5. **Four small image/init-side changes** would remove all four file overrides
   the prototype currently needs (§7.3). They belong to the image / AOSP /
   Lineage, not to Waydroid core — that is the main "coordination cost" of this
   RFC.
6. **One upstream (podman) change is worth requesting**: `--network=pasta:…`
   should not hard-code `--config-net` (§7.2). Until then the prototype ships a
   `PATH` shim, which is not upstreamable.
7. **Architecture is orthogonal to this RFC, but was prototyped together with
   it**: the backend runs whatever the image contains, so x86_64 hosts need an
   ARM translation layer to run `arm64-v8a`/`armeabi` apps. §11 documents it as
   an *optional, out-of-tree layered image* (proprietary `libndk_translation`),
   plus two reusable findings: the enabling properties must be written into the
   image's own `build.prop` (`ro.*` is write-once — the same class as §7.3), and
   app translation does **not** require `binfmt_misc` registration.
8. **The abstraction is not just a sketch.** A companion change set implements
   §5 on the current tree and proves the interface with a real CLI: a non-root
   user runs `waydroid container start` (daemon on the **session** bus) then
   `session start`, and `status` / `shell` / `logcat` / `freeze` / `unfreeze` /
   `restart` / `stop` / `session stop` all work against a rootless podman
   container — same image, same host, no root. Appendix C records it, including the
   fd broker that restores host↔Android binder (§C.3), so `app` / `prop` /
   `show-full-ui` and the four gbinder integration services work as well.

---

## 1. Motivation

Waydroid's container runtime is rootful LXC, started by a system-wide systemd
service and driven over the **system** D-Bus (`id.waydro.Container`). That
design has consequences users and packagers keep running into:

* Installing Waydroid requires root, and its runtime (`lxc`) must be installed
  and configured system-wide.
* Networking is the host's job (`waydroid-net.sh`: a `waydroid0` bridge,
  dnsmasq, iptables/nftables NAT, `sysctl` writes) — i.e. more root-only host
  state.
* `helpers/lxc.py` chmods host `/dev/*` nodes to 777
  (`set_permissions()`), because the container runs with Android's fake-uid
  patch: many Android UIDs collapse onto host uid 0.
* Running Android as the *same user* as its host would make "an Android app is
  a Linux process you can see, ptrace, and account for" natural, and is a
  prerequisite for sandboxing stories (one container per app, per-user
  Waydroid, Flatpak-style packaging).
* Prior art shows demand: Valve's **Lepton** is a Waydroid fork whose whole
  point is an unprivileged Android layer.

The blocker has never been "can Linux do this" but "does Waydroid's code have a
seam where a different runtime can plug in".

---

## 2. Goals and non-goals

**Goals**

* Add a *selectable* container backend so that a user-level (rootless) runtime
  can run the stock Waydroid images.
* Keep the rootful LXC path byte-for-byte the default behaviour until the new
  backend is proven.
* Make the seam honest: no `if os.geteuid() != 0: use_different_everything()`
  sprinkling; a small interface with two implementations.
* Document, per difference, the *correct* fix and *which repository* it belongs
  to, so the work can be split into independently reviewable pieces.
* Get the required host setup down to something a distro user can actually do
  (e.g. "your user needs a subordinate ID block of ~131072").

**Non-goals (for this RFC)**

* Rewriting Waydroid's UI, session manager, or image build.
* Replacing rootful LXC. Rootful LXC stays; rootless is an *alternative*.
* Defining the final per-app sandboxing model.
* Supporting kernels without unprivileged user namespaces.
* Performance work beyond "boots in the same ballpark".
* A PoC patch series. The prototype exists to validate the claims; §8 phases it
  into upstream-sized PRs.

---

## 3. Terminology

| Term | Meaning here |
| --- | --- |
| **rootless** | The container is created by the unprivileged user, inside a user namespace. No `sudo` at run time. |
| **rootful / LXC path** | Today's upstream behaviour: `lxc-start` as root, driven by a root systemd service. |
| **container service** | The `id.waydro.Container` D-Bus service (`tools/actions/container_manager.py`), today root-only. |
| **session** | The per-user `id.waydro.Session` service (`tools/actions/session_manager.py`), unprivileged already. |
| **intermediate UID** | podman's name-space term for the ID space *its own* rootless namespace lives in; distinct from both container IDs and host IDs. See §6.2. |
| **fake-uid** | Waydroid's patch that makes many Android UIDs resolve to the same host UID (0). Lepton uses this; this RFC does **not**. |
| **backend** | An implementation of the runtime interface defined in §5. |

---

## 4. Current architecture (facts the backend must respect)

### 4.1 Process and D-Bus topology

* Every `waydroid` action is a `tools` subcommand; root gating is per action, in
  `tools/__init__.py:25-27` (`actionNeedRoot()`), and applies only to `init`
  (`:61`), `upgrade` (`:64`), `container` (`:75`), `shell` (`:112`) and `logcat`
  (`:115`). Everything else (session, app, prop, status, …) already runs as the
  user.
* `prep_args()` (`tools/__init__.py:17-23`) hard-codes the *system* paths
  `work=/var/lib/waydroid`, `config=/var/lib/waydroid/waydroid.cfg`,
  `log=/var/lib/waydroid/waydroid.log`; only the log path has a non-root fallback
  (`/tmp/tools.log`, `:41-42`). **There is no per-user config path at all.**
* Two D-Bus services:
  * `tools/actions/container_manager.py`: `DbusContainerManager` at
    `/ContainerManager` (`id.waydro.ContainerManager`, `:26`) and
    `DbusInitializer` at `/Initializer` (`id.waydro.Initializer`); the bus name
    `id.waydro.Container` is claimed with `do_not_queue` at `:122` (a second
    start exits with "Container service is already running", `:123-125`).
    Methods `Start(a{ss})` `:26`, `Stop(b)` `:38`, `Freeze()` `:44`,
    `Unfreeze()` `:50`, `GetSession()->a{ss}` `:57`. `Start` authorizes via
    `GetConnectionUnixUser`/`GetConnectionUnixProcessID`: uid 0 or the session's
    `user_id`, and for non-root the caller PID must equal `session["pid"]`
    (`:28-34`).
  * `tools/actions/session_manager.py:40` owns `id.waydro.Session` on the
    **session** bus.
* `tools/helpers/ipc.py` constructs its proxy on `SystemBus()` unconditionally.
  That single line is the practical boundary between "root service" and
  "per-user service".
* `systemd/waydroid-container.service` has no `User=`, `BusName=id.waydro.Container`;
  `dbus/id.waydro.Container.service` sets `User=root`.

**Consequence for a backend:** the *runtime* is only half the work. A rootless
backend must also answer "who owns the container service, on which bus, reading
which config file".

### 4.2 The rootful container path

`tools/helpers/lxc.py` is the entire rootful implementation. It

1. writes `/var/lib/waydroid/lxc/waydroid/{config,config_nodes,config_session,waydroid.seccomp}`
   from `data/configs/config_base` plus per-Android-version fragments
   (`config_1`, `config_3`, `config_4`);
2. bind-mounts host `/dev/binder*`, ashmem, GPU/DRI, etc. into the container,
   and maps `/dev/hwbinder` → `/dev/host_hwbinder` (a **hard failure** if the
   host node is missing — this is a MAINLINE-mode check);
3. mounts tmpfs on `/mnt_extra`, `/tmp`, `/var`, `/run`;
4. `lxc.mount.auto=cgroup:ro sys:ro proc`, i.e. the cgroupfs is deliberately
   read-only and has to be repaired in-container (see §4.2 / §6.6);
5. keeps 23 capabilities via `lxc.cap.keep` and drops nothing;
6. `chmod 0777`s host `/dev/*` nodes in `set_permissions()`;
7. starts the container with
   `lxc-start -P /var/lib/waydroid/lxc -F -n waydroid -- /init`
   (the real Android init, first stage, as PID 1);
8. installs `data/configs/waydroid.seccomp` (LXC's own text format) and an
   AppArmor profile named `lxc-waydroid`.

Runtime-level command line surface that a backend must also accept (or
explicitly reject) comes from `tools/helpers/arguments.py`: `--uid`, `--gid`,
`--context`, `--nolsm`, `--allcaps`, `--nocgroup` — currently passed straight to
`lxc-attach`.

### 4.3 Rootfs assembly

`tools/helpers/{mount,images}.py` loop-mount `system.img` and `vendor.img`
(typically `/usr/share/waydroid-extra/images/…`) and stack an overlayfs on top.
Loop devices and kernel overlayfs require root.

### 4.4 Networking

`data/scripts/waydroid-net.sh` (invoked from `helpers/net.py`) creates
`waydroid0` = `192.168.240.1/24`, runs dnsmasq for DHCP (leases in
`dnsmasq.waydroid0.leases`), installs iptables/nftables NAT, and writes
`sysctl`s. The container's `lxc.net.0.link` is the `vnic` it hands to LXC.
Every step is root-only.

Notably, **Android itself already knows how to be a network citizen**: `netd`
brings up the interface, runs DHCP, installs its own per-UID fib rules
(`FRA_UID_RANGE`) pointing at its own tables, and does its own connectivity
validation. The host-side bridge/DHCP/NAT is only needed because the container
has no working L2 path to the outside by default. With pasta it does.

### 4.5 Configuration surface

`tools/config/__init__.py` holds `config_keys`, `defaults` and
`session_defaults`; `data/configs/*` holds runtime fragments. There is **no**
key (or code) anywhere for a container runtime choice — a repo-wide search for
`rootless|podman|crun|backend|runtime` returns nothing relevant. Whatever the
seam is called, this is where the new key belongs (a string, default `"lxc"`).

### 4.6 What is structurally LXC-only

Not portable to another backend without an equivalent: the `config_*` /
`waydroid.seccomp` fragments and the AppArmor profile (LXC formats), the
`lxc.cap.keep` list, `lxc.mount.auto=…`, `lxc-freeze`, and the
`--uid/--gid/--context/--nolsm/--allcaps/--nocgroup` pass-throughs.

Everything else in §4.2 is a *policy*, not a mechanism: it is what the runtime
must accomplish (devices present, tmpfs sizes, capability set, rootfs writable),
and it can be expressed in another runtime's terms.

`debian/control`'s only LXC-specific dependency is the `lxc` package (`iptables`
appears only because of `waydroid-net.sh`).

---

## 5. Proposed backend abstraction

### 5.1 Where the seam is

There is exactly one natural seam: `tools/helpers/lxc.py` is already "all the
runtime knowledge", and `tools/helpers/ipc.py` is already "the only place that
assumes the service is system-wide". Layout, **as implemented on the tree**
(see Appendix C for the exact patch):

```
tools/helpers/runtime/__init__.py   # factory + interface docs (get/name/…)
tools/helpers/runtime/lxc.py        # thin adapter over today's tools/helpers/lxc.py
tools/helpers/runtime/podman.py     # new: run.sh ported to Python + devstage.c
tools/helpers/runtime/devstage.c    # PID-1 stub, shipped next to podman.py
```

The `lxc` backend deliberately *adapts* rather than moves `tools/helpers/lxc.py`.
Moving it is pure churn and would hide the real diff in review; the adapter is
about 120 lines and every function it forwards is a one-liner. Should Phase 3
land for real, the move is a follow-up with no behaviour change. This is the one
place where the landed shape differs from the draft below.

### 5.2 Interface (draft)

```python
class RuntimeBackend:
    name: str                       # "lxc" | "podman"

    def prepare(self, cfg) -> None: ...      # images/rootfs, docs, host checks
    def setup_network(self, cfg) -> None: ...  # may be a no-op (pasta case)
    def start(self, args, session) -> None: ...
    def stop(self, quit_session: bool) -> None: ...
    def freeze(self) -> None: ...
    def unfreeze(self) -> None: ...
    def get_session(self) -> dict: ...
    def attach(self, args) -> int: ...        # waydroid shell
    def status(self, cfg) -> dict: ...        # for `waydroid status`
    def teardown(self, cfg) -> None: ...
```

The landed interface is flatter than the draft because the call sites pass
`args` everywhere anyway and Python makes a module-level function group cheaper
to review than an ABC + class. It is documented in
`tools/helpers/runtime/__init__.py`; the members are:

```
NAME, NEEDS_ROOT, USES_SYSTEM_BUS        # static capability flags
is_initialized(args)                     # replaces initializer.is_initialized()
host_binder(args)                        # can the host reach Android's SM?
binder_device_paths(args)                # gbinder driver names ("fd/N"), or None
waydroid_data_dir(args)                  # host mountpoint of the /data volume
prepare_drivers(args)                    # /dev nodes, binder
setup_host(args)                         # host perms, base props, dirs  (init)
setup_host_services(args)                # supl/network/cgroup helpers
teardown_host_services(args)
prepare_session(args, session)           # per-session host state, waydroid.prop
teardown_session(args)
set_protocol(args)                       # binder/service-manager AIDL version
status(args) -> "RUNNING"|"FROZEN"|"STOPPED"
start(args, session) / stop(args) / freeze(args) / unfreeze(args)
shell(args) / logcat(args)
```

`NAME`/`NEEDS_ROOT`/`USES_SYSTEM_BUS`/`host_binder` are what the rest of the
tree keys off: `container_manager._bus()` binds the daemon on the session bus and
skips `services.hardware_manager` when `USES_SYSTEM_BUS`/`host_binder` are False,
`tools.actionNeedRoot()` stops requiring root, and `session_manager` skips the
gbinder integration services. The gbinder path is restored without privilege:
the podman backend implements `binder_device_paths()` with an in-container fd
broker (§C.3), so `host_binder()` is true there too.

Rules the interface should enforce (these are what make the migration reviewable):

* A backend owns *everything* runtime-specific, including its config-file
  format; `runtime/lxc.py` keeps `data/configs/*` for itself, so `podman` is not
  forced to carry LXC fragments.
* `arguments.py`'s LXC-ish flags (`--uid`, `--gid`, `--context`, `--nolsm`,
  `--allcaps`, `--nocgroup`) become *documented capability queries* on the
  backend rather than raw strings (e.g. `supports_flag("context")`), and the
  podman backend maps the ones it can (`--uid/--gid` → `--user`) and rejects
  the rest with a clear message instead of silently ignoring them. (Landed
  partially: `shell` maps `--uid/--gid` to `podman exec --user`; the LXC-only
  flags are simply absent from the podman argument parser today.)

### 5.3 Configuration

New key, following the existing style in `tools/config/__init__.py`:

```python
"container_backend": "lxc",   # "lxc" | "podman"
```

Plus backend-scoped sub-keys that must **not** leak into the LXC path. As
landed (`tools/config/__init__.py`):

```python
"container_backend": "lxc",
"podman_image": "",           # required: OCI image ref (no external rootfs)
"podman_data_volume": "waydroid-data",  # named podman volume for /data
"podman_binderfs": "container",  # "container" (devstage + fd broker) | "host" (legacy)
"podman_uid_map": "auto",     # "auto" | "keep-id" | explicit --userns= value
"podman_pasta_options": "",   # extra comma-separated pasta options
```

Default behaviour is unchanged: `lxc`. Two supporting changes were needed to make
a *non-root* work directory possible at all:

* `tools/__init__.py:prep_args()` now honours `WAYDROID_WORK` (an env var, not a
  config key, so it survives the chicken-and-egg of "where is the config?") and
  calls `tools/config.rebase(work)`, which re-derives every `/var/lib/waydroid`
  default (`rootfs`, `overlay`, `data`, `lxc`, …) under the new work dir. Upstream
  computed those paths **once at import time**, which is why a per-user layout
  was previously impossible without a patch.
* `ipc._bus(args)` picks `SystemBus` or `SessionBus` from
  `runtime.uses_system_bus(args)`, and every `DBusContainerService()` /
  `DBusSessionService()` call site now passes `args` through.

Backend-internal escape hatches (env only, for A/B testing, mirroring run.sh):
`WD_USENS`, `WD_PASTA_CONFIG_NET`, `WD_CGROUP_RO`, `WD_GATEWAY`, `WD_EXTRA`,
`WD_KMSG_LOG`.

### 5.4 Call sites that change

| File | Change | Landed |
| --- | --- | --- |
| `tools/helpers/runtime/{__init__,lxc,podman}.py`, `devstage.c` | new package | **yes** |
| `tools/actions/container_manager.py` | rewritten: daemon bus from `_bus(args)`, `start/stop/restart/freeze/unfreeze/GetSession` delegate to `runtime.get(args)`; `hardware_manager` guarded by `host_binder` | **yes** |
| `tools/actions/initializer.py` | `is_initialized` → `runtime`; root-only `setupBinderNodes` guarded by `needs_root`; `init` branches: rootless only calls `backend.setup_host` | **yes** |
| `tools/actions/upgrader.py` | `lxc.status`/`lxc.setup_*` → `runtime.get(args)` | **yes** |
| `tools/actions/session_manager.py` | gbinder services (`user_manager`, `clipboard_manager`, `notification_manager`) guarded by `host_binder`; fixed an upstream `NameError` in `stop_container` (Appendix C.4) | **yes** |
| `tools/services/hardware_manager.py` | `lxc.stop/start/status` → `runtime` | **yes** |
| `tools/helpers/ipc.py` | bus selection from the backend | **yes** |
| `tools/config/__init__.py` | 6 new keys + `rebase(work)` | **yes** |
| `tools/__init__.py` | backend-aware `actionNeedRoot(args, action)`, `WAYDROID_WORK`, non-root work dir, `shell`/`logcat` via `runtime` | **yes** |
| `systemd/`, `dbus/`, `debian/control` | split runtime deps; add a user-unit variant | no (Phase 2/5) |

The podman backend itself is the port of `waydroid-rootless/run.sh` to Python
(`_build_run_cmd`, `_userns_opts`, `_pasta_network`, `_socket_binds`,
`_stage_host_files`, `_device_opts`) plus a static PID-1 stub compiled from
`devstage.c` on first use.

---

## 6. Rootless backend design

### 6.1 Runtime selection

| Candidate | Verdict |
| --- | --- |
| **Rootless LXC** | LXC supports unprivileged containers, but an unprivileged LXC container still needs a configured `lxc.idmap` in `/etc/lxc/…` plus `newuidmap`. It keeps the LXC-only config formats (§4.6), so it does not reduce the LXC coupling at all. It also does not solve the network story. |
| **Raw `crun`/`runc` + OCI bundle** | Fewer moving parts and no podman layer, but Waydroid would then own spec generation, storage, image extraction, overlay lifecycle and network namespace plumbing. That is a lot of new code to review for a project whose current runtime dependency is one package. |
| **podman** *(chosen for the prototype and recommended)* | Turns the whole problem into documented flags: `--rootfs <dir>:O` (overlay in podman's own storage, nothing mounted by the user), `--uidmap/--gidmap`, `--network=pasta:…`, `--device`, `--tmpfs`, `--security-opt unmask=…`, `--cap-add`. It is packaged essentially everywhere, and its rootless model is exactly the "unprivileged user namespace" this RFC wants. |
| **Rootful, with a setuid helper** | Defeats the purpose. |

**Recommendation:** the interface (§5) must not assume podman; a `crun` backend
should remain possible. But the reference implementation should be podman.

### 6.2 UID/GID mapping (the "netd" constraint)

**Why a map is needed at all.** `netd` installs per-UID fib rules from Android
(`FRA_UID_RANGE`), and for the default network ConnectivityService asks for
`0..INT_MAX`. The kernel maps *both* endpoints through
`make_kuid(current_user_ns(), …)`; an unmapped endpoint becomes `INVALID_UID`
and the rule is rejected with `EINVAL`
(`net/core/fib_rules.c`, `fib_nl2rule()`). With no rules, nothing points at
netd's table 1002, no route lands, and every packet dies on the fallback
`32000: from all unreachable`. So the container's UID space must reach
`0x7fffffff`.

Mapping *only* the endpoints does not help: the stored range is a pair of
*global* `kuid_t`s matched against the socket's global uid
(`fib_rule_match()`), so clamping `INT_MAX` onto a low host UID yields a rule
like `uidrange 0-5`, which is accepted and matches nothing *(verified)*.

**The map.** Express the container's UID space in terms of the invoking user and
their subordinate block:

```
container 0           -> host <me>              (keep-id semantics)
container 1 .. 1+N-1  -> host <subuid_start> .. (contiguous)
container 0x7fffffff  -> host <subuid_start+N-1>   ("pin the universe endpoint")
```

The netd rule then becomes the global range `[me, subuid_start+N-1]`, which
contains every Android daemon, app and isolated UID, while the namespace stays
only as wide as the subordinate block. The prototype used `N = min(block,
262144)`; the integration caps the map at **131072** (`MAP_CAP`), matching the
"we need 131072" ask. A wider `/etc/subuid` entry is accepted but the map does
not grow with it (`2^17` still covers Android's `0..99999` twice over;
`AID_APP_START 10000`, `AID_ISOLATED_END 99999`). The test host's
`/etc/subuid` and `/etc/subgid` are now `stray:100000:131072`, and the resulting
map is `0:1000:1` / `1:100000:131071` / `INT_MAX:231071:1`.

**Sizes.** With `/etc/subuid = <user>:100000:131072` the map is

```
--uidmap 0:0:1  --uidmap 1:1:131071  --uidmap 2147483647:131072:1
```

i.e. container `0..131071` → host `1000, 100000..231070`, and
`INT_MAX` → `231071`; the netd rule becomes `uidrange 1000-231071` *(verified as
an accepted map shape; see §9 for the one host-side step not yet exercised)*.

**The intermediate-ID trap (this is the one non-obvious part).** For rootless
podman, the second column of `--uidmap`/`--gidmap` is **not a host ID**:

```
podman-run(1), --uidmap (docs/source/markdown/options/uidmap.container.md)
  rootful user:  container_uid:host_uid[:amount]
  rootless user: container_uid:intermediate_uid[:amount]
     intermediate 0 = the uid for the podman user
     intermediate 1 = the 1st subordinate uid
```

Podman builds the container's user namespace as a **child** of the rootless
namespace its pause process holds open, and the kernel reads a `uid_map`
relative to the *parent* namespace. Writing host IDs therefore asks for the
wrong things: an entry `0:<my uid>:1` puts container root at host
`<subuid_start + my uid - 1>` (100999 on this host), so container root no longer
owns the image (owned by the invoking user), every lower-layer file resolves to
`uid 65534`, overlay copy-up fails, and init exits (0) without writing a single
line to kmsg *(verified; this was the prototype's long-standing "container
exits immediately" failure)*. The correct spellings are `0:0:1` and
`1:<intermediate_start>:<N-1>`, where `intermediate_start` is `1` plus the sizes
of the blocks that precede this one in `/etc/sub{uid,gid}` (podman hands
intermediate IDs out over all blocks, in file order).

**Boundary.** Every intermediate ID must already be valid in podman's own
rootless namespace. Verified against the edge: `--gidmap 1:1:2147483647` is
accepted (last intermediate ID == namespace top), `--gidmap 1:1:2147483648`
fails with `crun: write to 'gid_map': Operation not permitted: OCI permission
denied`. This also bounds the map automatically: `N` cannot exceed the
subordinate block. Note that the namespace extent is fixed when podman starts
its pause process, so *widening* `/etc/subuid` needs the namespace recreated
(`podman system migrate`, or letting the pause process exit). The backend should
detect the mismatch and print that diagnostic rather than emitting a raw crun
error.

**Why not fake-uid (Lepton's approach)?** Lepton collapses Android UIDs onto one
host UID. That is simpler (no subordinate block, no intermediate-ID subtleties)
but it (a) makes every Android process the same host uid, so isolation, quotas
and `ps` accounting are meaningless; (b) requires the same image patch that
forces upstream's rootful path to `chmod 0777` host device nodes; (c) still
needs a user namespace for the netd rule, so it is not actually less kernel
work. Real multi-UID mapping is the same amount of *backend* code and strictly
better at run time. **Recommend the true multi-UID map; keep fake-uid out of the
RFC as a compatibility fallback only if the one-case size check below proves
unworkable.**

### 6.3 Filesystem layout

* **Rootfs: an OCI image, nothing else.** The runtime boots one
  `podman_image` reference; there is no extracted-rootfs mode and no runtime
  overlay. Podman's storage driver provides the container layer. The test host
  originally had `overlay/.has-mount-program` in its storage (left by an earlier
  fuse-overlayfs use), and c/storage **permanently ignores native overlay**
  while that marker exists. Renaming the old storage to `storage.fuse-backup`
  and letting podman create a fresh one flips btrfs to
  `Native Overlay Diff: true`; the container `/` mount becomes
  `overlay` (with `userxattr`), not `fuse-overlayfs`. Native rootless overlay
  needs kernel >= 5.13. This is host storage state, not a backend choice. Extracting `system.img`/`vendor.img`, the `a+rX` fix-up and the
  `.rc` overrides all happen in the build system
  (`waydroid-rootless-image/build-rootfs.sh`, see `image/BUILD.md`), not at
  run time.
* **`/data` is a named podman volume** (`podman_data_volume`, default
  `waydroid-data`) mounted at `/data`. It survives `podman rm`; wipe it with
  `podman volume rm <name>` after removing the container. Inside the container
  it appears as `uid 1000`, not 0; Android tolerates this *(verified)*. Like
  upstream, the backend never wipes `/data` on an Android upgrade.
* **`/dev/socket`** is a persistent host directory that is swept clean at start
  (otherwise a previous run's sockets cause `Address already in use`); a tmpfs
  would also work.
* **`/tmp`, `/var`, `/run`, `/mnt`** are tmpfs, as in the LXC config.

### 6.4 Devices, binder, and init's first stage

* **No `mknod`.** A rootless process cannot create char/block device nodes, and
  `CAP_MKNOD` is checked against the initial user namespace. So the backend must
  not rely on Android's first stage creating `/dev/*`.
* **binderfs instead of host binder nodes.** Android supports binderfs; the
  container mounts it itself and creates `binder`, `hwbinder`, `vndbinder` (the
  prototype does this in a ~60-line static stub, `devstage.c`). This also fixes
  the `/dev/host_hwbinder` hard failure: upstream binds the *host's*
  `/dev/hwbinder` there; with binderfs the container points
  `/dev/host_hwbinder` at *its own* hwbinder node, which is what a self-contained
  Android instance wants. **This is the single most valuable image-side change
  (AOSP already has the code path; it is a configuration/build question).**
* **Host ↔ Android binder without privilege.** The host cannot reopen the
  container's binderfs nodes: `/proc/<pid>/root` is blocked by the rootless
  fuse-overlayfs mount, and a raw `mknod` of the same major:minor yields
  `ENXIO`. A tiny static helper inside the container (`wd-broker.c`) opens the
  node and passes the fd to the host over `SCM_RIGHTS`; the host then uses
  `/dev/fd/N`. No root, no host binderfs, no capability broker (§C.3).
* **Start at `second_stage`.** The prototype enters init at
  `init second_stage`, after the stub has done the (very few) first-stage jobs
  that matter: mount binderfs, provide `/dev/input`, make `/proc/sys` writable,
  and (optionally) repair cgroup mounts. A tiny, auditable stub is *necessary*
  for any rootless backend; it should live in upstream as a first-class
  component rather than as a shell wrapper (`/system/bin/sh` is not usable — the
  linker lives on an APEX that is not mounted yet, which is exactly why the
  stub is static).
* **Pass-through devices:** DRI render node (and optionally card0), dma-heap,
  fb0, uhid, fuse, tun. All optional; bind only what exists.

### 6.5 Networking

The prototype uses **pasta** (`--network=pasta:…`) and lets Android do the rest:

* pasta's DHCPv4 server gives netd its address; pasta is told to advertise the
  host's current resolver via DHCP (`--dns=… --dhcp-dns`), otherwise netd's
  resolver list is empty and every lookup fails *(verified)*.
* netd then installs its own per-UID rules and routes *(verified)*: from the
  host, `uidrange 1000-362143 lookup 1002`, and table 1002 holds
  `default via 10.0.0.1`. IPv4, IPv6 and DNS all reach 0% loss and
  `dumpsys connectivity` reports `VALIDATED` *(verified)*.
* **IPv6 needs work against podman, not against pasta.** podman always prepends
  `--config-net` to the pasta command line, and passt then sets `no_ra = 1`
  (`conf.c: if (c->pasta_conf_ns) c->no_ra = 1;`), so router advertisements are
  dropped. With `--config-net` pasta also copies the host's addresses into the
  namespace (which hides the problem until netd tears them down and can never
  get them back — SLAAC needs an RA). The prototype ships a `PATH` shim that
  filters `--config-net` and pins the advertised prefix with `-a <global>/<len>`
  *(verified: the container gets a global IPv6 address)*. **This is the one
  change to request in podman itself (§7.2).**

* **ADB:** the backend publishes the port with podman's native
  `-p 127.0.0.1:<podman_adb_port>:5555` (default 5555). Podman turns that into
  pasta's `-t 127.0.0.1/<port>-<port>:5555-5555` (host loopback only), so the
  generated pasta shim is only needed for `--config-net`, not for forwarding.
  `waydroid adb connect` uses `127.0.0.1:<port>` instead of the guest IP.
  Secure adb is on (`ro.adb.secure=1`): the first connection shows Android's
  "Allow USB debugging?" dialog; tick "Always allow from this computer" (that
  dialog is what makes `system_server` persist the key — writing
  `/data/misc/adb/adb_keys` by hand is not sufficient until then).

The important architectural point: **the rootless backend deletes
`waydroid-net.sh` from the picture.** There is no bridge, no dnsmasq, no NAT
rules, no `sysctl` writes, no root. Android configures itself.

### 6.6 Namespace and system interfaces

| Need | Rootful does | Rootless reality *(verified unless noted)* |
| --- | --- | --- |
| writable `/sys/fs/cgroup` | `lxc.mount.auto=cgroup:ro` + in-container repair | podman mounts it `ro` by default; `--security-opt unmask=/sys/fs/cgroup` makes the spec say `rw` (`pkg/specgen/generate/oci_linux.go:getCgroupPermissions`). Works; keep the in-container bind+remount as an idempotent fallback. |
| `/proc/cmdline` | AOSP first stage does `chmod("/proc/cmdline", 0440)` and aborts on failure; as root the chmod works | procfs inodes belong to the initial namespace's root, so the chmod can never succeed. Bind a plain file over `/proc/cmdline` (default content: the host cmdline, matching what Waydroid's container sees). |
| `/proc/sys` writes | `lxc.mount.auto=sys:ro` + repair | remount `/proc/sys` rw in the stub. |
| `/dev/kmsg` logging | real device via udev | rootless cannot `mknod`; bind a regular file over `/dev/kmsg` — `open(O_WRONLY)` succeeds and init's `KernelLogger` output lands there. Prototype-only debugging aid; upstream could use `logd`/`dmesg` alternatives. |
| eBPF | `/system/bin/bpfloader` loads programs | impossible rootless (`bpf()` is checked against the initial namespace, and `bpffs` is not userns-mountable). `bpfloader.rc` has `reboot_on_failure reboot,bpfloader-failed`, so it must be neutralised. Lepton ships a userspace `bpf()` shim ("ubpf"); see §7.3. |
| `SO_RCVBUFFORCE` | ueventd sets a 16 MB receive buffer | `SO_RCVBUFFORCE` needs `CAP_NET_ADMIN` in the initial namespace, and AOSP's `uevent_open_socket()` treats the failure as fatal. Requesting `min(rmem_max, 16 MB)` via `ueventd.rc` avoids the FORCE branch entirely. Image-side fix: the vendor image should ship that `.rc` (the Waydroid vendor image currently has none). |
| `setpriority(-20)` | zygote gets `priority -20` | `setpriority` is a `capable()` check against the initial namespace, and init treats a failed process attribute as `LOG(FATAL)`. Image-side fix. |
| seccomp | LXC text profile + AppArmor `lxc-waydroid` | prototype uses `--security-opt seccomp=unconfined --security-opt label=disable`; the user namespace *is* the boundary. Acceptable for an opt-in backend, but an OCI seccomp profile is the right end state. |
| `iptables -m quota2` | host has the module | a rootless container cannot load netfilter modules; if the image's netd needs `xt_quota2`, the host must pre-load it (root step) or the image must degrade gracefully. See §9. |
| `Freeze`/`Unfreeze` | `lxc-freeze` | expected to map to `podman pause`/`unpause` (cgroup freezer); **not yet verified** (§9). |

### 6.7 Display, input, audio

* **Display:** Android's hwcomposer is a Wayland client, so the compositor
  socket must be visible in the container. The socket is created
  `0777 & ~umask` = `0755`; `connect()` needs *write* on the inode, and with
  real UIDs the container's "system" uid is a subuid that owns nothing, so
  hwcomposer fails with `EACCES`. Upstream gets away with it only because
  rootful + fake-uid makes uid 1000 really uid 0. The prototype `chmod 0777`s
  the socket (contained: `/run/user/<uid>` is `0700`). **The upstream-correct
  fix is better: pass a connected socket via `WAYLAND_SOCKET=<fd>`**, which
  libwayland accepts in place of connecting, and which needs no permission
  change at all. Same idea for PulseAudio where possible.
* **Input:** uses Waydroid's existing path — hwcomposer receives Wayland input
  and feeds Android by `mkfifo()`ing `/dev/input/wl_{keyboard,pointer,tablet,touch}_events`.
  All four devices come up `Enabled: true` and the user confirmed
  keyboard/mouse/touch work *(verified)*. One pitfall: AOSP init's keychord
  machinery opens every `/dev/input` entry with a *blocking* `open()` as soon as
  a single `keycodes` line exists anywhere in the loaded `.rc` files; the tablet
  FIFO never gets a writer on a plain desktop, so init blocks forever, the boot
  action queue stalls, and the watchdog kills `system_server`. The prototype
  binds in a copy of LineageOS's `init.lineage-system_ext.rc` with the
  `keycodes` line removed. See §7.3 for the real fix.
* **Audio:** unchanged from upstream's model (`waydroid.pulse_runtime_path`),
  with `/run/xdg/pulse/native` bound in; `audioserver` and the vendor audio HAL
  run *(verified)*. The end-to-end "sound comes out" check is the user's ear
  test, not part of this RFC.

---

## 7. Gap analysis: required upstream changes

The prototype's differences from stock upstream are finite. Each is listed with
the *correct* fix and the repository that owns it. Nothing here requires a
Waydroid-core hack in the happy path.

### 7.1 Hard point ① — subordinate IDs, and why the ask is now small

**Naive requirement:** netd's rule reaches `INT_MAX`, so the container's UID
map must cover it; the obvious "map the whole range" reading needs
`<user>:100000:2147483647` in `/etc/subuid` **and** `podman system migrate`
(the pause-process namespace is created once and survives `/etc/subuid` edits).

**Actual requirement (this RFC):** with an explicit `--uidmap`, only a
`~131072`-wide block is needed (`0..99999` plus headroom), because the map pins
the container's `INT_MAX` directly to the block's top instead of mapping
everything in between. Two consequences:

* The host-side setting is a normal, small admin step; `waydroid` can *check*
  it and print a one-line diagnosis instead of failing with a crun error.
* The `podman system migrate` footnote disappears for anyone who never had a
  too-small block; it remains a *diagnostic* for users who widened the file with
  a live pause process.

**Where the fix belongs:** the backend (map generation + host check) in
`waydroid/waydroid`; documentation/packaging guidance for `/etc/subuid`.
Distributions whose default is `65536` (common) need a documented
"widen to 131072" step. This is the hard point upstream reviewers will care
about, and the answer should be presented as *"we need 131072, and here is the
exact reason, and here is the diagnostic"*, not *"please require 2 GiB"*.

### 7.2 Hard point ② — podman hard-codes `--config-net`, pasta has no opt-out

* **Symptom:** IPv6 is broken (link-local only) even though pasta supports
  RAs; IPv4 works, which makes it look like an Android problem.
* **Root cause:** podman prepends `--config-net` to the pasta command line;
  passt then force-disables RAs (`conf.c: if (c->pasta_conf_ns) c->no_ra = 1;`).
  `--network=pasta:…` options are only *appended*, and there is no knob to drop
  the flag.
* **Upstream-correct fix:** a small **podman** change — e.g. only pass
  `--config-net` when the user asked for it (pasta's own default is *off*), or
  provide an explicit opt-out in `--network=pasta:…`. Pasta/passt cannot undo
  it locally.
* **Prototype workaround:** a `PATH` shim that filters the flag (plus `-a` to
  pin the advertised prefix). Not upstreamable, and shouldn't be.
* **Fallback if the podman change is not accepted:** document IPv4-only for the
  rootless backend, or have the backend run pasta itself and hand podman an
  existing netns. Both are worse; the podman PR is small and self-contained.

### 7.3 Hard point ③ — the four file overrides are image/init-side

These are the prototype's *only* intrusive changes to the Android side. All four
are consequences of kernel checks against the initial user namespace, and all
four have a fix that belongs outside Waydroid's container code.

| Override | Why it is needed | Correct fix, and where |
| --- | --- | --- |
| `bpfloader.rc` replaced by `setprop bpf.progs_loaded 1` | rootless cannot load eBPF; the `.rc` has `reboot_on_failure`, so the container reboots otherwise | **Image / Lepton-style shim:** ship a userspace `bpf()` compatibility layer (Lepton's "ubpf") or a rootless image variant that does not require eBPF. Owning repo: the Android image build (Waydroid images / Lineage), not Waydroid core. |
| `init.zygote64_32.rc` with `priority -20` removed | `setpriority` is a `capable()` check against the initial namespace; init treats the failure as fatal | **AOSP/Lineage:** init should not `LOG(FATAL)` when a process attribute is not permitted, or the image should not request a nice value it cannot get. Owning repo: AOSP `system/core/init` / the image's `.rc`. |
| `init.lineage-system_ext.rc` with `keycodes …` removed | init's keychord handler opens every `/dev/input` node with a blocking `open()`; the tablet FIFO never gets a writer, so init hangs and the watchdog kills `system_server` | **AOSP/Lineage (preferred):** `keychords.cpp` should open non-blocking / skip non-evdev nodes (`EVIOCGVERSION` is already checked *after* the blocking open). Alternatively the image should not register keychords, or Waydroid's input transport should not rely on blocking FIFOs. Owning repo: AOSP or Waydroid's image. |
| `/vendor/ueventd.rc` (new file: `uevent_socket_rcvbuf_size <min(rmem_max,16M)>`) | `SO_RCVBUFFORCE` needs `CAP_NET_ADMIN` in the initial namespace and AOSP treats the failure as fatal; the vendor image ships no `ueventd.rc` | **Image:** ship the vendor `ueventd.rc` (real device vendor images have one). Owning repo: the Waydroid vendor image. |

Two related, non-`.rc` items are worth noting because they are the same class of
problem and are *not* hacks:

* `waydroid.prop`: upstream already has this mechanism (a property overlay bound
  over `/vendor/waydroid.prop`). The prototype's file is a normal, in-tree use
  of it. The only thing to add for rootless is documenting which properties are
  required (e.g. `waydroid.host.user/uid/gid`, `waydroid.*_path`).
* `tmp/cmdline` → `/proc/cmdline`: not a `.rc` override but the same root cause
  (§6.6). **AOSP** should tolerate a failed `chmod("/proc/cmdline")`, or the
  backend should keep providing the synthetic file (it is a 3-line policy and
  can live in the backend).

### 7.4 Hard point ④ — the remaining privileged system interfaces

Beyond §7.3: writable cgroupfs, `/proc/sys`, `mknod`, `iptables`/`xt_quota2`,
`bpf()`. Their status, with the prototype's resolution:

* **cgroupfs:** solved with a documented podman flag, plus an idempotent
  in-container fallback (§6.6). No upstream change required.
* **`/proc/sys`, `/proc/cmdline`, `/dev/kmsg`:** backend/stub policy (§6.6).
* **`mknod`:** eliminated by using binderfs and entering init at
  `second_stage`; the residual need is the stub itself, which should be a
  first-class upstream component.
* **`bpf()`:** image-side (§7.3).
* **`xt_quota2`:** host must pre-load the module, or netd must degrade — needs
  a decision (§9). It does not affect boot or basic connectivity in the
  prototype's configuration, but it is the kind of thing that will surface for
  someone, so it should be acknowledged in the RFC rather than discovered later.
* **seccomp/AppArmor:** an OCI seccomp profile is the end state; the user
  namespace is the boundary in the meantime.

### 7.5 The real upstream work: a per-user service model

This is the part that has nothing to do with container runtimes and everything
to do with Waydroid's architecture:

1. **Bus.** `tools/helpers/ipc.py` proxies `id.waydro.Container` on the *system*
   bus. A rootless container service must live on the *session* bus (or a
   per-user bus), so this must become configuration, and every caller must
   handle "the container service is my own user's".
2. **Ownership.** `systemd/waydroid-container.service` (root) needs a
   `systemd --user` sibling, and `dbus/id.waydro.Container.service` needs a
   user-bus variant. `actionNeedRoot()` (`tools/__init__.py:25-27`) must consult
   the backend: with `container_backend=podman`, `waydroid container` /
   `waydroid shell` must *not* require root.
3. **Paths.** `prep_args()` hard-codes `/var/lib/waydroid/{waydroid.cfg,
   waydroid.log}`. Rootless needs `$XDG_CONFIG_HOME` / `$XDG_STATE_HOME`
   equivalents (`~/.config/waydroid`, `~/.local/state/waydroid`) — and, because
   container state is now per-user, a per-user image directory too.
4. **Authorization.** `Start`'s check ("uid 0 or the session's user") collapses
   to "the session's user" once the service is per-user; `polkit` policy can
   then be dropped for that backend rather than reimplemented.
5. **`Freeze`/`Unfreeze`, `status`, `shell`:** each becomes a backend method;
   `shell`'s `--uid/--gid/--context/--nolsm/--allcaps/--nocgroup` surface must be
   mapped or refused (§5.2).

None of this is conceptually hard; it is the bulk of the line count and the bulk
of the review discussion, because it touches D-Bus, systemd and packaging at
once. It is also the part most amenable to being landed *before* the podman
backend exists (see Phase 1/2 in §8).

**Status: items 1, 3, 5 are implemented and verified**, and the rootless
process model is now **merged**: `waydroid session start` owns both
`id.waydro.Container` and `id.waydro.Session` in one process (the container
control objects are created on the same session bus and main loop), so
`status`/`shell`/`logcat`/`freeze`/`unfreeze`/`stop` work without root and
without a second daemon. A standalone `waydroid container start` is still
supported: if it already owns the name, `session start` falls back to the
D-Bus `Start` call. `waydroid container stop` now goes through the daemon, so
it also terminates the merged session (with a direct-stop fallback when no
daemon is listening). Item 2 is still *not* landed: there is no
`systemd --user` unit or `dbus/*.service` user-bus activation yet, so the
process is started by hand. Item 4 (dropping the `Start` authorization for a
per-user service) is naturally satisfied because the daemon and the session run
as the same uid, but the check itself was left intact so the rootful path is
byte-identical.

Host↔Android binder is no longer part of the gap: the fd broker (§C.3) makes
`host_binder(args)` true for the podman backend, so `hardware_manager`,
`user_manager`, `clipboard_manager` and `notification_manager` run in the
rootless path as well.

---

## 8. Phased rollout

Designed so each phase is independently reviewable and independently useful, and
so nothing user-visible changes until Phase 4.

> **Landing status (this RFC's companion patch).** Phases 1–3 exist on the tree
> today as one working change set (Appendix C): the backend seam, the
> configuration keys, the session-bus service model, and a complete rootless
> podman backend driven end-to-end by the CLI as a non-root user. Phase 4
> (image-side fixes) is *partially* realised by the `tweaked` layer (§11), and
> Phase 5 (per-user units, rootless default) is not started.

**Phase 0 — prototype (done, out of tree).** `waydroid-rootless/`: boot, network,
render, input, audio. Purpose: prove the claims in §6 and produce the evidence
this RFC cites. Not proposed for merge.

**Phase 1 — pure refactor: the backend seam.** Move `tools/helpers/lxc.py` to
`tools/helpers/runtime/lxc.py`; introduce the ABC; make
`container_manager.py` / `initializer.py` / `upgrader.py` call it; leave exactly
one implementation and zero behaviour change. `data/configs/*` are parked behind
the LXC backend. *Risk: low. Value: it makes every later phase small.*
**Landed** with the adapter variant described in §5.1 (`runtime/lxc.py` forwards
to the existing `helpers/lxc.py` instead of moving it, so the diff is reviewable).

**Phase 2 — configuration + service model groundwork.** Add
`container_backend` (default `"lxc"`), make `ipc.py`'s bus configurable, add the
per-user config/state path plumbing, make `actionNeedRoot()` backend-aware, add
the `systemd --user` unit. Still only the LXC backend exists; a user with
`container_backend=lxc` sees no difference. *Risk: medium (D-Bus/systemd), but
testable without rootless support.* **Landed except the `systemd --user` unit**
(config keys, `ipc._bus`, `WAYDROID_WORK` + `config.rebase()`, backend-aware
`actionNeedRoot`).

**Phase 3 — the podman backend (experimental).** Rootfs-directory mode, derived
UID/GID map + host diagnostics, pasta networking, the device/tmpfs/binderfs
mapping, the stub that enters `second_stage`, capability/security flags, the
documented podman workaround for `--config-net` (or land §7.2 first).
Gated: opt-in, "experimental", may require manual `/etc/subuid` widening.
*Risk: medium; blast radius is confined to the opt-in path.* **Landed**: the
whole `waydroid-rootless/run.sh` flow now lives in `runtime/podman.py`, and the
non-root CLI cycle is verified end-to-end (Appendix A.4).

**Phase 4 — image-side fixes, so the backend needs no file overrides.** Land
§7.3 items (binderfs, ubpf/no-eBPF variant, keychords, `ueventd.rc`,
zygote priority) in the image build. After this, the podman backend carries no
"replace an `.rc`" logic at all, which is the acceptance criterion for calling
it non-experimental. *Risk: coordination across repos, not technical.*

**Phase 5 — make rootless the default for non-root users.** Switch
`actionNeedRoot()` and the service model so that a plain user can `waydroid
init`/`session`/`show` without root, with LXC still available for rootful
installs. *Risk: high — this is where distro/packaging feedback lands.*

**Phase ? — podman networking.** §7.2 as its own small upstream contribution, in
parallel with Phase 3 (the workaround makes Phase 3 possible without it).

---

## 9. Risks and open questions

**Risks**

1. **Subordinate-ID availability.** Many distros default to 65536. The backend
   must detect this, explain it, and degrade predictably (`0..99999` minus the
   missing tail means isolated-process UIDs cannot be mapped). Mitigation: the
   map is derived, so a narrow block still boots a useful container.
2. **Kernel/distro user-namespace policy.** `kernel.unprivileged_userns_clone=0`
   (older Ubuntu) or AppArmor's `apparmor_restrict_unprivileged_userns=1`
   (Ubuntu 24.04+) will block rootless podman outright. This is a host
   prerequisite to document, not something Waydroid can fix.
3. **podman version coupling.** The design leans on documented flags
   (`--rootfs <dir>:O`, `unmask=/sys/fs/cgroup`, `--network=pasta:…`), but the
   *behaviour* of a couple of them has changed across releases. Mitigation: the
   interface isolates this to one file, and `waydroid status` should surface the
   runtime version.
4. **Relaxed seccomp/AppArmor.** Defensible (user namespace is the boundary) but
   must be explicit, opt-in, and documented; the end state is an OCI seccomp
   profile.
5. **Two service models.** Rootful LXC and per-user podman will coexist for a
   long time; documentation and `status` output must make the active backend
   obvious, or bug reports become unactionable.
6. **Image identity.** The image-side fixes (§7.3) change the meaning of "the
   Waydroid image" — the rootless backend may need a distinct image flavour.
   Deciding whether that is a variant or the new baseline is a project-policy
   question, not a technical one.

**Open questions (explicitly unresolved here)**

1. **`Freeze`/`Unfreeze`:** does rootless `podman pause` do the right thing for
   Android's expectations? **Verified on the integration tree**: `freeze` →
   `podman pause` (`inspect` reports `paused`, `status` reports `FROZEN`) and
   `unfreeze` → `running`; after unpause Android is healthy
   (`sys.boot_completed=1`, `prop get` and `app list` still work). Whether any
   *session* path needs it is still open.
2. **`iptables -m quota2`:** is the module required by the image's netd in the
   default configuration, and if so, is a host `modprobe` acceptable, or should
   the image gain a "no accounting" mode?
3. **idmapped rootfs:** `--rootfs <dir>:idmap` would remove the `a+rX` fix-up
   and restore real ownership. It failed with `operation not permitted` on the
   prototype's kernel; worth re-testing on newer kernels and considering as the
   preferred long-term mode.
4. **`/data` ownership:** persistent host directories owned by the user show up
   as container `uid 1000`, not 0. Does anything beyond the prototype's testing
   depend on `/data` being root-owned?
5. **Multi-user concurrency:** two users, one image directory. Read-only lower +
   per-user upper should be fine, but `waydroid init`/`upgrade` need a lock or
   a per-user copy.
6. **Image flavour:** one image for both backends, or a rootless variant
   (binderfs/ubpf/no-keychords)?
7. **Scope of the interface:** should `crun`-without-podman be a *supported*
   third backend (Lepton-shaped), or explicitly out of scope so the interface
   stays as small as §5.2?
8. **Android major-version upgrades.** With the image+volume model, same-SDK
   rebuilds are pull-and-recreate. Whether `/data` survives a change of SDK
   level is **unverified**: upstream never wipes it, and AOSP has in-boot
   migration paths, but Waydroid does not run an OTA postinstall. Matching
   upstream, the backend does not inspect, warn about, or wipe `/data`; if it
   breaks, the user handles it. Needs a real two-version test. §C.7.

---

## 10. Prior art

* **Lepton (Valve).** A Waydroid fork whose purpose is an unprivileged Android
  layer: `--rootfs <dir>:O`, in-container binderfs, pasta, a userspace `bpf()`
  shim, and (per app) one container. It demonstrates the *runtime* half of this
  RFC and is the closest existing answer to §7.3's eBPF item. It differs in
  using a **single/fake UID**, which this RFC deliberately does not: see §6.2.
* **Waydroid (rootful LXC).** The baseline; §4.
* **Unprivileged containers generally** (podman rootless, `systemd-nspawn`
  `--private-users`, LXC `lxc.idmap`): all confirm that user namespaces + a
  subordinate ID block are the mechanism, and all share the same "map needs to
  be expressed correctly" trap described in §6.2.
* **`distrobox`/`toolbox`-style user containers.** Prior art for the "the user
  owns the container, the app is just a process" model that motivates §1.

---

## 11. Optional extension: an ARM translation layer, delivered as a layered image

Nothing in §1–§10 is architecture-specific — the backend runs whatever the image
contains, and the stock Waydroid images are x86_64. This section covers the one
feature the prototype added next to the backend, because it is where the
"rootless podman" and "image management" designs meet, and because its fixes
belong to the image rather than to the backend.

### 11.1 The problem

An Android app that ships only `arm64-v8a` / `armeabi-v7a` native code cannot be
installed or executed at all on an x86_64 Android image. Android's answer is the
**native bridge**, a shared library named by `ro.dalvik.vm.native.bridge` that is
loaded into zygote and emulates the guest ABI *in-process* (`libhoudini` from
Intel for older releases, Google's `ndk_translation` — `libndk_translation.so` —
for Android 11+). The Waydroid image does not ship either; users add one with
`waydroid_script`, or Lepton's ARM shims.

Under rootless podman the container-side mechanics are unchanged — the bridge is
just files and `ro.*` properties inside the rootfs — but two things about it are
worth recording for the RFC: *where* the properties can be written (§11.5), and
whether the `binfmt_misc` handlers the package installs are needed (§11.6).

### 11.2 Scope: proprietary, out of tree, user-built

`ndk_translation` is a **proprietary Google binary**. It must not be committed to
Waydroid or to this prototype, and it is not proposed for the upstream image.
The RFC therefore treats it as an *optional layered image* that a user builds
locally on top of the bootable base image; the build inputs are pinned so the
result is reproducible, and the artefacts stay out of any tree that is meant to
be distributed.

| | |
| --- | --- |
| prebuilt | `supremegamers/vendor_google_proprietary_ndk_translation-prebuilt` @ `68734c52556d3d7a6db34c603dd9276915c29f2f` ("chromeos_zork R136", Android 13 drop) |
| packaging reference | `waydroid_script` `stuff/ndk.py` — the file list and the 10 `ro.*` properties are taken from it verbatim |
| integrity | 177 files / 43 MB, `image/prebuilts.sha256` (checked by `build.sh`); `image/fetch-prebuilts.sh` re-fetches them by commit |
| delivery | `waydroid-rootless-image/{Containerfile,build.sh,README.md,fetch-prebuilts.sh}` |

> The supported rootfs builder in this workspace is `image/build-rootfs.sh`
> (input: raw/sparse `system.img` + `vendor.img`; output: patched `rootfs/` +
> `manifest.json` (+ optional `rootfs.tar`), see `image/BUILD.md`).  The
> `build.sh`/Containerfile flow above is the optional ARM-layering reference;
> libndk is a user overlay, not part of the rootfs build.

### 11.3 Why the Containerfile contains only `COPY`

The constraint that shapes the whole design: **an un-booted Android rootfs cannot
execute anything.** `/system/bin/sh` is a bionic binary whose interpreter
`/system/bin/linker64` resolves into `/apex/com.android.runtime/bin/`, and the
APEX payloads are hosted at `/system/apex/<name>/` and only flattened onto
`/apex` by init at boot. So in an image *before* first boot every `/system/bin`
program fails:

```
$ podman run --rm localhost/waydroid-base:13 /system/bin/sh -c 'echo hi'
exec container process (missing dynamic library?) `/system/bin/sh`: No such file or directory
```

`RUN` is therefore unusable (the "no network / no extra base image" argument is
secondary). The Containerfile is three `COPY`-only stages, and `build.sh` derives
the changed files on the host, where the rootfs *is* readable as plain files:

```
base     = ARG BASE (default localhost/waydroid-base:13)
tweaked  = base    + COPY build/tweaks/   /     # the three §7.3 overrides
arm      = tweaked + COPY prebuilts/ /system/ + COPY build/armprops/ /
```

Generalised for upstream: *any* backend that wants to pre-modify an image must
either do it outside the image's own runtime (as here) or the APEX flattening has
to be understood by the image tooling. It is the same reason the prototype binds
`waydroid.prop` at run time instead of baking it.

### 11.4 Image chain, and the `WD_ROOTFS_IMAGE` semantics

| image | contents |
| --- | --- |
| `localhost/waydroid-base:13` | `tar --numeric-owner --owner=0 --group=0 -C ../rootfs . \| podman import -` — the extracted rootfs, owned by root inside, exactly as the derived map presents it |
| `localhost/waydroid-tweaked:13` | base + the overrides of §7.3 (`bpfloader.rc` → `setprop bpf.progs_loaded 1`, zygote `priority -20` removed, keychords removed) |
| `localhost/waydroid-arm64:13` | tweaked + `prebuilts/` copied into `/system` + the ten `ro.*` properties |

The prototype's `run.sh` grew one input, `WD_ROOTFS_IMAGE=<name[:tag]>`
(the integrated backend calls this `podman_image`, and accepts **only** an image
reference -- there is no external-rootfs mode), which
replaces `--rootfs <dir>:O` with the image reference. In image mode it skips
exactly the three things the image already contains — the rootfs existence check,
the `a+rX` fix-up of §6.3, and the three `.rc` bind mounts of §7.3 — while
everything host-dependent (`/data`, `/dev/socket`, `ueventd.rc`, `waydroid.prop`,
the synthetic `/proc/cmdline`, the `devstage` stub) is still prepared and mounted
as before, so an image and a directory boot the same system. Upstream this is
just "the backend accepts a rootfs directory *or* an image reference".

*(verified)*: `localhost/waydroid-{base,tweaked,arm64}:13` build in ~18 s in the
same rootless storage; all three boot to `sys.boot_completed=1`; directory mode
(no `WD_ROOTFS_IMAGE`) still boots in 18 s, i.e. the refactor is behaviour
preserving; and the `tweaked` image is the control group for the ARM layer
(`ro.product.cpu.abilist=x86_64,x86`).

### 11.5 The properties have to be image-side, because `ro.*` is write-once

This is §7.3 (hard point ③) in a stronger form. The ARM layer is enabled purely
by properties, and `ro.*` properties are **write-once**: the first definition
wins. `waydroid.prop` is a run-time overlay bound over `/vendor/waydroid.prop`,
so it *cannot* fix a value the image's `build.prop` already set — and the stock
image already sets `ro.dalvik.vm.native.bridge=0` (`/system/build.prop:131`) on
purpose. Hence the arm image rewrites the three partition `build.prop`s in place
(`system`, `vendor`, `vendor/odm/etc`) with `sed`, and appends the remaining keys
to `system/build.prop`; values are exactly waydroid_script's ten
(`abilist{,32,64}`, `native.bridge`, `enable.native.bridge.exec{,64}`,
`ro.ndk_translation.version=0.2.3`, `dalvik.vm.isa.arm{,64}`).

*(verified)*: `getprop` reports all ten, and the plain family is derived by init
from the `ro.system.*` copies — `ro.product.cpu.abilist` =
`x86_64,x86,arm64-v8a,armeabi-v7a,armeabi`.

### 11.6 `binfmt_misc`: what it is for, what it is not, and its scope

The package also ships `etc/init/ndk_translation.rc`, which mounts a fresh
`binfmt_misc` and registers four handlers (`arm_exe`, `arm_dyn`, `arm64_exe`,
`arm64_dyn`) pointing at `ndk_translation_program_runner_binfmt_misc*`. Three
separate questions were measured, because "is this rc part of the layer at all?"
depends on the answers.

1. **It is not needed to run ARM *apps*.** With all four handlers unregistered
   (`echo -1 > /proc/sys/fs/binfmt_misc/<entry>` from inside the container, then
   `cmd activity force-stop` + relaunch), an `arm64-v8a`-only app still logs
   `ndk_translation: Initialized NDK translation (aarch64), version 0.2.3` and
   runs `SDL_main` from its `lib/arm64/libmain.so`, and an `armeabi-v7a`-only app
   logs the same for `(armv7l)`. App code is translated by the *in-process*
   native bridge loaded into zygote; no `exec()` of guest code happens. The rc is
   therefore optional for the prototype's purpose.
2. **It *is* needed to `exec()` a standalone ARM ELF** (a shell command, a test
   binary). With the handlers present, `podman exec … /system/bin/arm/linker`
   prints the ARM linker's usage; with them removed, the same command fails with
   `exec /system/bin/arm/linker: Exec format error`. This is the only capability
   the rc actually adds.
3. **On this kernel the registration stays inside the container.** The container
   mounts its own `binfmt_misc` instance, so the handlers are visible in
   `/proc/sys/fs/binfmt_misc` of the container and *not* in the host's; verified
   by letting the image's own rc perform the mount + registration at boot and
   then comparing the two directories (container: 4 entries; host: `register`,
   `status` only). Caveat for older kernels: per-mount binfmt_misc instances are
   a relatively recent kernel feature; before it, registrations go into the
   single global list and would be visible to — and potentially shadow — the
   host's own ARM handlers (e.g. `qemu-user` binfmt). The prototype's escape
   hatch: `WD_EXTRA="-v /dev/null:/system/etc/init/ndk_translation.rc:ro"`
   neutralises the rc (empty rc file), leaving the properties and the in-process
   bridge untouched *(verified: no handlers registered, ARM app still runs)*.

Recommendation carried into §7.3/Appendix B: keep the rc in the *user-built*
image (it costs nothing and enables `exec`ing ARM binaries), treat it as optional,
and do not make it part of any upstream default, since it is the only part of
this layer that touches kernel-global facilities.

### 11.7 Result (verified)

| Check | Result |
| --- | --- |
| Images | `localhost/waydroid-{base,tweaked,arm64}:13` (2.37/2.37/2.41 GB), built in ~18 s |
| Boot, image mode | `WD_ROOTFS_IMAGE=localhost/waydroid-arm64:13` → `boot_completed=1` in ~15 s |
| Properties | 10/10 `ro.*` set; `ro.product.cpu.abilist=x86_64,x86,arm64-v8a,armeabi-v7a,armeabi` |
| Bridge in framework | `zygote64` (pid 77) and 32-bit `zygote` (pid 78) each map 4 `libndk_translation*` segments |
| binfmt handlers | all four registered by the image's rc; container-only visibility |
| abi resolution | `primaryCpuAbi=arm64-v8a` for an `arm64-v8a`-only APK; `armeabi` / `armeabi-v7a` for 32-bit-only APKs; `pm install` → `Success` (no "no matching ABI") |
| 64-bit ARM execution | `dev.serwin.AnarchRE` (`arm64-v8a` only, SDL3 game): pid alive, 37 `libndk_translation*` mappings, `Initialized NDK translation (aarch64), version 0.2.3`, `V SDL: Running main function SDL_main from …/lib/arm64/libmain.so`, `Displayed …: +372ms`, UI renders (`screencap`) |
| 32-bit ARM execution | `com.wrmndfzzy.atomize` / `org.sipdroid.sipua` (`armeabi-v7a` only): 12 `/system/lib/libndk_translation*` mappings, `Initialized NDK translation (armv7l), version 0.2.3` |
| Direct ARM `exec()` | `arm/linker` prints its usage with the handlers registered; `Exec format error` without them |

Not verified: GLES/Vulkan *through* the bridge (the bridge ships translated GL/VK
libraries; only the SDL/UI path was exercised), and `armeabi` apps whose
`targetSdkVersion < 23` — `org.helllabs.android.xmp` resolves to `armeabi` and
installs, but the platform's `ReviewPermissionsActivity` blocks it before the app
process starts, which is a platform behaviour unrelated to the bridge.

---

## Appendix A — prototype status (what is actually proven)

### A.1 Environment

* Host: CachyOS, kernel `7.2.11-1-cachyos`, unprivileged user `stray`
  (uid/gid 1000), GNOME on Wayland.
* `podman 6.1.4` (rootless), `crun 1.30.1-dirty`, `pasta 2026_10_02.cba3570`.
* `/etc/subuid` = `/etc/subgid` = `stray:100000:2147483647`.
* Image: the stock Android 13 LineageOS images from
  `/usr/share/waydroid-extra/images/{system.img,vendor.img}`, extracted to a
  user-owned directory.
* Prototype: `waydroid-rootless/run.sh` + `src/devstage.c` (a ~450-line static
  stub).

### A.2 Verified results

| Check | Result |
| --- | --- |
| Boot | `sys.boot_completed=1` in **10–14 s** |
| Container UID/GID map | `0 0 1` / `1 1 131071` / `2147483647 131072 1` |
| netd's own rules (host view) | `uidrange 1000-362143 lookup 1002`, plus `11000: … uidrange 1000-1000 …` |
| Default route | table 1002: `default via 10.0.0.1 dev eth0` |
| IPv4 / IPv6 / DNS | `ping 10.0.0.1`, `ping 1.1.1.1`, `ping6 ipv6.google.com`, `ping google.com` — 0% loss |
| Connectivity validation | `dumpsys connectivity`: `VALIDATED` |
| Rendering | `screencap` → PNG 2160×1302 |
| Input | all four `wayland_{keyboard,pointer,tablet,touch}` devices `Enabled: true`; `/dev/input/wl_*_events` FIFOs present; user confirmed keyboard/mouse/touch |
| Capabilities | `CapEff=0000001cc1ac75ff`, identical to the rootful baseline |
| Audio | `init.svc.audioserver` and `init.svc.vendor.audio-hal` running; `/run/xdg/pulse/native` present (end-to-end audio is a user ear test) |
| Map boundary | `--gidmap 1:1:2147483647` accepted; `--gidmap 1:1:2147483648` → `Operation not permitted` |
| Narrow map shape (block = 131072) | `--uidmap 0:0:1 --uidmap 1:1:131071 --uidmap 2147483647:131072:1` accepted; container UID 0 = host 1000, `INT_MAX` = host 231071 |
| Directory mode vs image mode | both reach `sys.boot_completed=1` (dir 18 s, `tweaked` image 15 s); `ro.product.cpu.abilist` is the control difference (`x86_64,x86` vs the ARM list) |
| Layered images | `localhost/waydroid-{base,tweaked,arm64}:13` (2.37/2.37/2.41 GB) build in ~18 s from `../rootfs` + `prebuilts/`, no registry and no root |
| ARM layer (§11, proprietary, optional) | 10/10 properties; `arm64-v8a`-only and `armeabi-v7a`-only apps install and execute (37 / 12 `libndk_translation*` mappings, `(aarch64)` / `(armv7l)` translator init, `SDL_main` from the app's ARM `libmain.so`) |
| `binfmt_misc` (§11.6) | image's rc registers 4 handlers, visible only inside the container; ARM apps run with them removed; direct ARM `exec()` fails without them |

### A.3 Not yet verified

* Boot with `/etc/subuid` actually narrowed to 131072 (the map **shape** is
  verified; the "podman's namespace is only as wide as the file" half is
  inferred from the boundary experiment).
* End-to-end audio playback and Vulkan/device-level rendering quality.
* Anything multi-user, and anything about `waydroid init`/`upgrade` in a
  rootless layout.
* GLES/Vulkan *through* the ARM bridge (the bridge ships translated GL/VK
  libraries; only the SDL/UI path was exercised), and `armeabi` apps with
  `targetSdkVersion < 23` (blocked by `ReviewPermissionsActivity`). See §11.7.

(`Freeze`/`Unfreeze` used to be listed here; they are verified on the
integration tree — Appendix A.4.)

### A.4 Non-root CLI, end to end, on the upstream tree

The prototype above drives `podman` directly. This is the *integration* result:
the same host, the same image, but the user interface is the stock Waydroid CLI
(`waydroid.py`) running as `stray` (uid 1000), with
`container_backend=podman` and `podman_image=localhost/waydroid-arm64:13`
(`podman_binderfs=container`). Nothing is run as root; the rootful package's
`waydroid-container.service` is untouched and still owns `id.waydro.Container`
on the *system* bus, while the integration daemon owns the same name on the
*session* bus (`_bus(args)`, §5.3).

| Command | Result |
| --- | --- |
| `container start` | non-root daemon binds `id.waydro.Container` on the **session** bus as `stray` |
| `session start` | `Start()` over the session bus → `podman run` → `sys.boot_completed=1` (fresh `/data`, ~10 s) |
| `status` | `Session: RUNNING` / `Container: RUNNING` / `MAINLINE` / `192.168.240.112` / `stray(1000)` / `wayland-0` |
| `shell getprop sys.boot_completed` | `1` |
| `shell id` | `uid=0(root) gid=0(root)` |
| `shell getprop ro.product.cpu.abilist` | `x86_64,x86,arm64-v8a,armeabi-v7a,armeabi` (ARM layer active through the CLI path) |
| `logcat` | streams `logcat` from the container |
| `container freeze` | `podman pause`; `podman inspect` → `paused` |
| `container unfreeze` | `podman unpause`; → `running` |
| `container restart` | `rm -f` + run; container `Up N seconds`, map re-printed by the daemon |
| `container stop` | container removed; `status` → `Container: STOPPED` |
| `session stop` | clean teardown: session process exits, container removed, `status` → `Session: STOPPED`, session bus cleared |

Host↔Android binder is **not** degraded: the in-container fd broker (§C.3)
gives the CLI a real `/dev/fd/N` for Android's servicemanager. Verified on the
same non-root run:

| Command | Result |
| --- | --- |
| `prop get ro.build.version.sdk` | `33` |
| `prop get ro.product.cpu.abilist` | `x86_64,x86,arm64-v8a,armeabi-v7a,armeabi` |
| `prop set persist.waydroid.multi_windows false` | rc 0 |
| `app list` | 11 packages, with names and categories |
| `app install <apk>` | rc 0; the new package appears in `app list` |
| `app launch com.android.documentsui` | rc 0 |
| `show-full-ui` | rc 0 |
| `user_manager` | desktop entries (`~/.local/share/applications/waydroid.*.desktop`) and icons under `<work>/data/icons` |
| `session stop` → `session start` | fd cache re-acquired (keyed by container **ID**); `prop get` / `app list` still work |

Follow-ups closed in the same tree: `status` now gets the container address
from `/proc/net/fib_trie` (pasta has no dnsmasq lease), so `adb connect` works;
`app remove` falls back to `pm uninstall`; `app install` prepares
`/data/waydroid_tmp` itself; `set_protocol` reads `/system/build.prop` out of
the **image** (`podman create` + `podman cp`, no boot) and rewrites the
binder/service-manager protocols (no `/data` policy, as upstream).
`podman_binderfs=host` is now legacy: container-local binderfs + the fd broker
is the supported path.

Caveat found here and worth a code comment: the backend stops Android with
`podman stop -t 5` + `rm -f`, which does **not** give Android a clean shutdown;
a later boot can stall waiting for `vendor.waydroid.display@1.0` (SurfaceFlinger
up, `system_server` blocked, `hwservicemanager` logging `ctl.interface_start` →
`error 0x20`). A fresh `/data` boots again (`podman unshare rm -rf <work>/data`),
i.e. it is `/data` state, not the container. §C.5.

---

## Appendix B — prototype difference → upstream fix

| # | Prototype behaviour | Rootless reason | Upstream shape | Owning repo |
| --- | --- | --- | --- | --- |
| 1 | explicit `--uidmap/--gidmap` (intermediate IDs) | netd's `0..INT_MAX` fib rule | `podman` backend generates it; host check + diagnostic | waydroid |
| 2 | `/etc/subuid` wide enough for the map | subordinate ID availability | document "~131072 is enough" | waydroid (+ distros) |
| 3 | `PATH` shim filtering pasta's `--config-net` | podman forces it; passt then kills RAs | podman: make `--config-net` opt-in | podman (upstream) |
| 4 | `--rootfs <dir>:O`, extracted image dir | no root loop/overlay | `images.py`/updater learn a directory mode | waydroid |
| 5 | `a+rX` fix-up of a dozen files | extracted files all appear `uid 0` | idmapped rootfs (open question §9) | waydroid |
| 6 | binderfs + `/dev/host_hwbinder` → own node | no root `mknod`, no host binder to bind | Android-side binderfs support | image / AOSP |
| 7 | init entered at `second_stage` by a static stub | no first-stage device creation | first-class backend stub | waydroid |
| 8 | `bpfloader.rc` replacement | no rootless eBPF | ubpf-style shim / no-eBPF variant | image (Lepton-style) |
| 9 | `zygote` `priority -20` removed | `capable()` against initial ns | init tolerates it, or image omits it | AOSP / image |
| 10 | keychords line removed | init blocks on FIFO input | non-blocking/skip-FIFO open in keychords | AOSP / image |
| 11 | `/vendor/ueventd.rc` added | `SO_RCVBUFFORCE` unavailable | ship the file in the vendor image | image |
| 12 | synthetic `/proc/cmdline` file | chmod on procfs fails | stub keeps providing it, or AOSP tolerates | waydroid / AOSP |
| 13 | cgroup `unmask=/sys/fs/cgroup` + stub fallback | podman mounts cgroupfs ro | documented flag | waydroid |
| 14 | tmpfs `/tmp`, `/var`, `/run`, `/mnt` | as LXC config | podman flags | waydroid |
| 15 | `chmod 0777` of the Wayland socket | real UID ≠ 0 cannot connect | `WAYLAND_SOCKET=<fd>` handoff | waydroid |
| 16 | persistent `/data`, `/dev/socket` dirs | no root bind targets | backend-owned state dirs | waydroid |
| 17 | `seccomp=unconfined`, `label=disable` | LXC profile formats are LXC-only | OCI seccomp profile later | waydroid |
| 18 | user-level container service, session bus, per-user config | rootless runs as the user | `ipc.py` bus config, `systemd --user`, XDG paths | waydroid |
| 19 | no `waydroid-net.sh` at all | pasta + netd | backend is responsible for networking | waydroid |
| 20 | rootfs exported to a local image (`podman import`, `--owner=0 --group=0`) and booted with `podman_image` | rootless storage is the only writable image store; no registry | backend accepts an **image reference only**; building it is the build system's job | waydroid |
| 21 | layered image whose Containerfile is `COPY`-only, with the changed files derived on the host by `build.sh` | an un-booted Android rootfs cannot `RUN` anything (APEX not flattened) | image flavour that already ships the §7.3 fixes (Phase 4) | waydroid / image |
| 22 | the §7.3 `.rc` overrides baked into the `tweaked` image | they are image content, and image mode skips the bind mounts | same as §7.3; the image is the delivery vehicle | image / AOSP |
| 23 | ARM `ro.*` properties `sed`-ed into the image's three `build.prop`s | `ro.*` is write-once, so `waydroid.prop` cannot override the image's `=0` | image-side, user-built (proprietary blob; not an upstream default) | image / user |
| 24 | `ndk_translation.rc` kept, registering 4 `binfmt_misc` handlers | only adds `exec()` of standalone ARM ELFs; apps do not need it (in-process bridge) | optional; document the opt-out and the pre-namespacing kernel caveat | user / image |

---

## Appendix C — the integration as landed

This appendix describes the companion change set that makes §5–§8 concrete on
the current tree (base commit `db7294c`). It is a *working* change set, not a
polished PR; §C.6 lists what a reviewer should expect to be cleaned up first.

### C.1 The runtime package

```
tools/helpers/runtime/__init__.py   # factory + interface documentation
tools/helpers/runtime/lxc.py        # adapter over tools/helpers/lxc.py
tools/helpers/runtime/podman.py     # run.sh ported to Python
tools/helpers/runtime/devstage.c    # PID-1 stub, compiled on first use
```

`__init__.py` exposes a flat, module-level API plus four static capability
flags, and discourages holding on to the backend object (`runtime.get(args)`):

```
NAME              "lxc" | "podman"
NEEDS_ROOT        bool    -> tools.actionNeedRoot() consults this
USES_SYSTEM_BUS   bool    -> ipc._bus(args) picks SystemBus/SessionBus
is_initialized(args)
host_binder(args) bool    -> can the host reach Android's service manager?
prepare_drivers / setup_host / setup_host_services / teardown_host_services
prepare_session / teardown_session / set_protocol
status / start / stop / freeze / unfreeze / shell / logcat
```

Image mode's `is_initialized()` checks `podman image exists <podman_image>`
instead of treating any non-empty string as initialized, so `init` / `session
start` fail with a clear message when the image was never built or pulled.
Backends may also expose optional helpers (`binder_device_paths`,
`waydroid_data_dir`, `device_ip_address`, `remove_app`, `push_apk`/`remove_apk`);
call sites use `getattr()` and leave the rootful path unchanged.

The delivered rootless contract is deliberately small: `podman_image` (an OCI
reference) + `podman_data_volume` (a named volume mounted at `/data`) + the
Wayland/Pulse/DRI sockets. There is no external rootfs and no runtime overlay;
the image is the build system's output. Host-side file injection uses native
`podman cp` (the APK goes to `/tmp`), and desktop icons point at the volume's
`/data/icons` mountpoint (Android writes them there).

`runtime/lxc.py` is deliberately a thin adapter (~120 lines of one-liner
forwards) over the *existing* `tools/helpers/lxc.py`, so the interesting diff is
the podman backend rather than a `git mv`. `runtime/podman.py` is the Python
port of `waydroid-rootless/run.sh` (`_build_run_cmd`, `_userns_opts`,
`_pasta_network`, `_socket_binds`, `_stage_host_files`, `_device_opts`), plus
`devstage.c` built into `$WORK/devstage` on first use. A dry run of
`_build_run_cmd` matches run.sh: `--uidmap 0:0:1 / 1:1:131071 /
2147483647:131072:1`, `pasta -a,<v6>/64`, the 12 `--cap-add`s, every bind, and
the wayland/pulse binds.

### C.2 Configuration and the work-dir bootstrap

Six keys (all defaulted such that `container_backend=lxc` behaves as before) are
added; see §5.3. The bootstrap problem — *the config file lives under the work
dir, so how does a non-root user pick one?* — is solved with an env var that is
read before the config is loaded:

```
WAYDROID_BACKEND=podman     -> overrides container_backend
WAYDROID_WORK=/path         -> prep_args() calls config.rebase("/path")
```

`config.rebase(work)` re-derives `rootfs`, `overlay`, `data`, `lxc`, `waydroid.cfg`,
`waydroid.log`, … from the new work dir. This is required because upstream
computes those defaults **once at import time**; without the rebase, changing
`work` after import leaves every derived path pointing at `/var/lib/waydroid`.

### C.3 Host ↔ Android binder: the fd broker

With `podman_binderfs=container` (the default), `devstage` creates a private
binderfs inside the container. Android is fully functional, but the host cannot
*open* those nodes directly. Two routes fail for structural reasons:

* `/proc/<container-pid>/root/dev/binderfs/binder` — the container root is a
  rootless **fuse-overlayfs** mount with `default_permissions`, and the caller is
  not mapped into the container's user namespace, so traversal is `EACCES` (even
  as host root, whose uid is unmapped in the child namespace).
* `mknod` a host node with the same major:minor (`242:1`) — binderfs does not
  register a globally reopenable cdev for that devt, so `open()` is `ENXIO`.

The working path is **fd passing**, and it needs no privilege:

1. A ~90-line static helper (`tools/helpers/runtime/wd-broker.c`) is exposed
   through a host-owned bind mount (`<work>/tmp/binder` → `/run/wd-binder`) and
   run inside the container with `podman exec -u 0`.
2. It `open()`s `/dev/binderfs/<binder|vndbinder|hwbinder>`, listens on a unix
   socket in that directory, and sends the open fd with `SCM_RIGHTS`. It serves
   one client and exits.
3. The host connects, receives the fd, and uses `/dev/fd/N`; the kernel keeps the
   same `binder_proc` / binder context alive.
4. `helpers.drivers.loadBinderNodes()` asks the active backend for driver names
   (`binder_device_paths()`, returning `"fd/N"`), so every existing
   `gbinder.ServiceManager("/dev/" + args.BINDER_DRIVER, …)` call site works
   unchanged. `host_binder(args)` is true for the podman backend.

The cache is per process and invalidated by container **ID** (not name), so a
recreated container gets fresh fds; sockets and the broker process are cleaned
up after each acquisition.

Verified from the host on the non-root tree: `is_present()` true, the full
service list, `waydroidplatform` obtained, then `prop get/set`, `app
list/install/launch`, `show-full-ui`, and all four gbinder integration services
(`user_manager` produced the desktop entries and icons). `app remove` returns
-1 from the platform service on this image; the CLI now falls back to
`podman exec … pm uninstall`, so the command succeeds (the transaction itself
reaches Android normally).

`podman_binderfs=host` (hand the host binderfs nodes to the container) remains
unsupported: `BINDER_SET_CONTEXT_MGR` is `EPERM` for a container whose root is
mapped into a child user namespace, which is why rootful LXC can do it and
rootless cannot. The fd broker makes that mode unnecessary.

Two configuration facts still matter if the `host` route is ever retried:
`binder`/`vndbinder`/`hwbinder` are not `config_keys`, so they must be written
under `[waydroid]` (a `cat >>` lands them in `[properties]` and
`loadBinderNodes` raises `KeyError`); and on this host the world-accessible nodes
are the generic `/dev/binderfs/*` ones, not the 0600 `anbox-*` nodes.

### C.4 An upstream bug this path surfaced

`session_manager.stop_container()` (HEAD, unmodified) dereferences an undefined
global `args`:

```python
def stop_container(quit_session):          # no args parameter
    tools.helpers.ipc.DBusContainerService(args).Stop(quit_session)
```

`DbusSessionManager.Stop`, `handle_disconnect`, `sigint_handler` and the
bare-`except` in `stop()` all call it. Over D-Bus the `NameError` is swallowed
into a failed method return, so the session still tears down and the bug hides.
The integration change threads `args` through (`stop_container(args,
quit_session)`), which is what makes `waydroid session stop` exit cleanly with
no error on stderr. Worth landing on its own, independent of this RFC.

### C.5 Stopping is not a clean shutdown

`backend.stop()` is `podman stop -t 5` followed by `podman rm -f` — it does not
ask Android to shut down. Repeated hard stops were observed to leave `/data` in
a state where the next boot stalls in the display HAL:

```
surfaceflinger        futex_wait
system_server         futex_wait   (waiting for SurfaceFlinger)
hwservicemanager      ctl.interface_start "vendor.waydroid.display@1.0::…" -> error 0x20
servicemanager        Tried to start aidl service SurfaceFlinger as a lazy service, but was unable to
```

It is not the container: wiping `/data` and rebooting restores a ~10 s cold
boot. Two follow-ups: give Android a graceful shutdown (`svc power shutdown` /
`reboot -p` through `backend.shell`) before `podman stop`, and/or raise the
timeout. Until then, `podman unshare rm -rf <work>/data` is the recovery, and it
has to be `unshare` because the files are owned by mapped UIDs the user cannot
unlink directly.

### C.6 Short-cuts to clean up before a real PR

* `runtime/podman.py` still imports `make_base_props` / `setup_host_perms` from
  `helpers.lxc` because they are not LXC-specific. They belong in a shared
  `runtime/hostprops.py` (or in `runtime/__init__.py`).
* `runtime/lxc.py` should eventually replace `helpers/lxc.py` rather than
  forward to it (§5.1).
* The podman argument parser does not yet model the LXC-only flags
  (`--context`, `--nolsm`, `--allcaps`, `--nocgroup`); `shell` maps `--uid/--gid`
  to `podman exec --user` and the rest are simply absent. §5.2 asks for explicit
  capability queries.
* There is no `systemd --user` unit yet, so the daemon is currently started by
  hand (`waydroid container start &`). §7.5 / Phase 2.

### C.7 What an Android version bump still needs

`waydroid upgrade` is not "swap `system.img`". It downloads and checksums a
**matched** system+vendor pair, updates the OTA timestamps, discards the old
writable overlay (`remove_overlay()`), regenerates `waydroid_base.prop`
(host GPU/HAL/EGL/Vulkan/camera props, `waydroid.tools_version`, OTA info, and
the `[properties]` overrides), re-probes ashmem, and restarts. At session start
`images.mount_rootfs()` rebuilds `waydroid.prop`, and
`helpers.protocol.set_aidl_version()` re-reads `rootfs/system/build.prop` and
rewrites `binder_protocol` / `service_manager_protocol` (the SDK → aidl map).
`migration()` is about the *tools* version, not the Android version.

In the image model the first steps collapse into "pull/build the new image" (the
writable layer is ephemeral, so there is no overlay to discard), and the
protocol step is now handled from the image.

`/data` is a separate question, and it is **not verified here**. Upstream does
**not** wipe it: `upgrade` only removes the writable overlay (`overlay_rw` /
`overlay_work`), and `init --force` leaves `/data` alone as well. AOSP does have
in-boot userdata migration paths (`installd` / `PackageManagerService`,
`SettingsProvider`, `apexd`, `vold`), so a major-version jump *may* work with
the preserved data — "incompatible" is a hypothesis, not a finding. It is not
guaranteed either: those paths are built around a device OTA
(`update_engine` / postinstall), which Waydroid's image swap does not run, and
`/data/vendor` is not rebuilt. Whether a given version pair survives needs a
real two-version test. Like upstream, the backend deliberately does not inspect,
stamp, warn about, or wipe `/data`; if a version change breaks it, the user
clears it (`podman unshare rm -rf <work>/data`) or reinstalls.

In image mode `waydroid upgrade` pulls `podman_image_remote` (a placeholder
until images are published), tags it as `podman_image`, and restarts the
per-user service; a failed pull leaves the running container untouched.
`waydroid upgrade --offline` skips the pull and only restarts the service
(use it after importing a locally built rootfs: `image/build-rootfs.sh ...
--tar` then `podman import ...`).  `waydroid init` in image mode does the
same pull/tag, runs `setup_host()` (named volume + base props) and starts the
user service; a failed pull leaves any running container untouched.  The
`/data` volume is preserved.
