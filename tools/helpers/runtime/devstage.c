/*
 * devstage.c - rootless stand-in for Android's init first stage.
 *
 * The container runs this as PID 1 and it then exec()s "/init second_stage".
 * We start init at its second stage because the first stage is impossible in a
 * rootless container: it mknod()s character devices and chmod()s /proc/cmdline,
 * and the kernel checks CAP_MKNOD / CAP_FOWNER against the *initial* user
 * namespace, which a rootless container can never satisfy.
 *
 * Everything else the first stage would have provided is supplied by podman
 * (tmpfs on /dev, /proc, /sys, devpts, /dev/null, /dev/urandom, ...) or by
 * run.sh bind mounts, with five exceptions that we do here:
 *
 *   - the binder devices, because Android's init does not know about binderfs
 *     (Waydroid instead bind-mounts the host's /dev/binder*, which needs host
 *     root).  binderfs exists precisely so that unprivileged user namespaces
 *     can get their own binder devices: the filesystem carries FS_USERNS_MOUNT,
 *     and nodes are created by the BINDER_CTL_ADD ioctl on
 *     <mount>/binder-control instead of by mknod (which needs CAP_MKNOD in the
 *     initial userns).
 *
 *   - the tmpfs on /mnt, see setup_mnt_tmpfs() below.
 *
 *   - a writable /proc/sys, see setup_proc_sys() below.
 *
 *   - a writable /sys/fs/cgroup, see setup_cgroup_rw() below.
 *
 *   - /dev/input, see setup_dev_input() below.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/wait.h>
#include <unistd.h>

/* <linux/android/binderfs.h> */
struct binderfs_device {
    char name[256];
    uint32_t major;
    uint32_t minor;
};
#define BINDER_CTL_ADD _IOWR('b', 1, struct binderfs_device)

static const char *const BINDER_NODES[] = {"binder", "hwbinder", "vndbinder"};
#define N_NODES (sizeof(BINDER_NODES) / sizeof(*BINDER_NODES))

static void setup_binderfs(void) {
    if (access("/dev/binder", F_OK) == 0) { /* already provided */
        return;
    }

    if (mkdir("/dev/binderfs", 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "devstage: mkdir /dev/binderfs: %s\n", strerror(errno));
    }
    if (mount("binder", "/dev/binderfs", "binder", 0, NULL) != 0) {
        fprintf(stderr, "devstage: mount binderfs failed: %s\n", strerror(errno));
        return;
    }

    int ctl = open("/dev/binderfs/binder-control", O_RDONLY | O_CLOEXEC);
    if (ctl < 0) {
        fprintf(stderr, "devstage: open binder-control: %s\n", strerror(errno));
    } else {
        for (size_t i = 0; i < N_NODES; i++) {
            struct binderfs_device dev;
            memset(&dev, 0, sizeof(dev));
            snprintf(dev.name, sizeof(dev.name), "%s", BINDER_NODES[i]);
            if (ioctl(ctl, BINDER_CTL_ADD, &dev) != 0 && errno != EEXIST) {
                fprintf(stderr, "devstage: BINDER_CTL_ADD %s: %s\n", dev.name,
                        strerror(errno));
            }
        }
        close(ctl);
    }

    for (size_t i = 0; i < N_NODES; i++) {
        char target[64], link[64];
        snprintf(target, sizeof(target), "/dev/binderfs/%s", BINDER_NODES[i]);
        snprintf(link, sizeof(link), "/dev/%s", BINDER_NODES[i]);
        /* binderfs creates the nodes 0600 root:root; AOSP's ueventd.rc makes
         * them world read/write, and it must, because servicemanager,
         * hwservicemanager and vndservicemanager all run as uid 1000 and would
         * otherwise fail to open the driver (and then nothing ever becomes the
         * binder context manager). */
        if (chmod(target, 0666) != 0) {
            fprintf(stderr, "devstage: chmod %s: %s\n", target, strerror(errno));
        }
        unlink(link);
        if (symlink(target, link) != 0) {
            fprintf(stderr, "devstage: symlink %s: %s\n", link, strerror(errno));
        }
    }

    /*
     * Waydroid bind-mounts the *host's* /dev/hwbinder onto /dev/host_hwbinder
     * (tools/helpers/lxc.py) and refuses to start if that fails, because its
     * patched libhidlbase has a second, "host" service manager:
     * defaultServiceManager1_2(bool), ProcessState::self(bool) and
     * getRawServiceInternal(..., bool) all take an isHost flag, and the vendor
     * Waydroid HALs (vendor.waydroid.display/window/clipboard) consult it.  A
     * rootless container has no host hwbinder to bind -- binderfs is exactly
     * the mechanism that lets us have our own -- and for Waydroid the two
     * paths are only ever two names for one binder instance, so point
     * host_hwbinder at our own hwbinder node.
     */
    unlink("/dev/host_hwbinder");
    if (symlink("/dev/binderfs/hwbinder", "/dev/host_hwbinder") != 0) {
        fprintf(stderr, "devstage: symlink /dev/host_hwbinder: %s\n",
                strerror(errno));
    }
}

/*
 * Android's first stage mounts a tmpfs on /mnt (SetupMountNamespaces() in
 * system/core/init/mount_namespace.cpp):
 *
 *     mount("tmpfs", "/mnt", "tmpfs",
 *           MS_NOEXEC | MS_NOSUID | MS_NODEV, "mode=0755,uid=0,gid=1000")
 *
 * We need it for a reason beyond fidelity.  /mnt is where zygote prepares the
 * per-user storage directories for the processes it forks, and it does that
 * with fs_prepare_dir() = mkdir() + chown() + chmod():
 *
 *     PrepareDir("/mnt/user/0", 0710, AID_SHELL, AID_EVERYBODY)
 *
 * Our container root is a fuse-overlayfs.  FUSE filesystems cannot be
 * idmapped-mount(), and a chown() to any uid other than the caller's own is
 * checked with capable_wrt_inode_uidgid() against the superblock's user
 * namespace, which for a FUSE mount is the *initial* one.  So the chown()
 * fails with EPERM and the fork of system_server dies:
 *
 *     JNI FatalError called: (system_server) ... Zygote.cpp:769:
 *       fs_prepare_dir failed on /mnt/user/0: Operation not permitted
 *
 * A tmpfs is mounted from inside this user namespace, so its superblock
 * belongs to us (and it is memory backed, so there is nothing below to
 * delegate the chown() to) and every uid mapped into the container works.
 */
static void setup_mnt_tmpfs(void) {
    if (mount("tmpfs", "/mnt", "tmpfs", MS_NOEXEC | MS_NOSUID | MS_NODEV,
              "mode=0755,uid=0,gid=1000") != 0) {
        fprintf(stderr, "devstage: mount tmpfs /mnt: %s\n", strerror(errno));
        return;
    }
    if (chmod("/mnt", 0755) != 0) {
        fprintf(stderr, "devstage: chmod /mnt: %s\n", strerror(errno));
    }
}

/*
 * /proc/sys is mounted read-only by podman/crun.  Android's netd writes IPv6
 * sysctls there while bringing an interface up -- IpClient calls disableIpv6(),
 * setAcceptRa() and setIpv6PrivacyExtensions() before it ever reaches the DHCP
 * state machine -- and when those writes fail with EROFS, IpClient aborts
 * provisioning ("onProvisioningFailure(): 5") in a tight retry loop, so eth0
 * never gets a lease.
 *
 * The proc superblock was mounted by podman inside our own user namespace, so
 * we hold CAP_SYS_ADMIN over it and can lift the read-only bit ourselves.  Only
 * the network sysctls become usable in practice: the global ones under
 * /proc/sys/kernel/... are checked against the initial user namespace and still
 * return EPERM.
 */
static void setup_proc_sys(void) {
    if (mount("proc", "/proc/sys", "proc",
              MS_REMOUNT | MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) != 0) {
        fprintf(stderr, "devstage: remount /proc/sys rw: %s\n", strerror(errno));
    }
}

/*
 * Android's init needs a writable cgroupfs: it creates /sys/fs/cgroup/uid_N and
 * pid_N for every service it starts.
 *
 * run.sh asks podman for that explicitly with "--security-opt
 * unmask=/sys/fs/cgroup", which is podman's own switch for it:
 *
 *     pkg/specgen/generate/oci_linux.go: getCgroupPermissions(unmask) returns
 *     "rw" when unmask contains "/sys/fs/cgroup" (or is "ALL"), "ro" otherwise,
 *     and SpecGenToOCI() puts that in the OCI spec's cgroup mount.
 *
 * With it the mount is rw from the start and this function returns immediately
 * (statvfs check below); it exists as an idempotent fallback for podman builds
 * or hosts whose spec still says "ro".
 *
 * Without the flag the OCI spec mounts /sys/fs/cgroup with "ro" in its option
 * list -- podman does that for rootless containers whatever --cgroupns and
 * --cgroups say, see the config.json of such a container:
 *
 *     {"destination": "/sys/fs/cgroup", "type": "cgroup", "source": "cgroup",
 *      "options": ["rprivate", "nosuid", "noexec", "nodev", "relatime", "ro"]}
 *
 * crun does not mount cgroup2 itself then: cgroup2 carries FS_USERNS_MOUNT and
 * would work in our user namespace, but a cgroup2 mount already exists at that
 * path (podman's), and stacking a second one fails with EBUSY (or EPERM
 * without CAP_SYS_ADMIN).  do_mount_cgroup_v2() (libcrun/linux.c) falls back to
 * bind mounting the *container's own cgroup directory* over the target while
 * keeping the spec's flags, "ro" included, which is what we see:
 *
 *     cgroup2 /sys/fs/cgroup cgroup2 ro,nosuid,nodev,noexec,nsdelegate,...
 *
 * init's cgroup setup then fails for every service it starts, twice per
 * service, 88 times of each in a single boot:
 *
 *     libprocessgroup: Failed to make and chown /sys/fs/cgroup/uid_1000:
 *                      Read-only file system
 *     init: createProcessGroup(1000, 118) failed for service 'surfaceflinger':
 *                      Read-only file system
 *
 * and those two messages are not just noise: Service::Start() (init/
 * service.cpp) treats a failing createProcessGroup() as a failed start and
 * reports it to its caller as one.  With the mount writable, init creates and
 * owns its uid_/pid_ directories as on any other machine and both messages
 * disappear.
 *
 * The read-only bit is a per-mount flag, not a property of the filesystem: the
 * cgroup2 superblock is writable, and the directory crun bound over the mount
 * point is the container's own cgroup, which our uid 0 owns (podman creates the
 * scope with the uid that runs podman).  Binding that mount on top of itself and
 * remounting it without MS_RDONLY gives the writable view back -- the same
 * operation crun's fallback performs, minus the flag.  Only a bind remount can
 * lift it: remounting the inherited mount directly is refused with EPERM,
 * because the mount was created in the parent user namespace, and every write
 * to it needs CAP_SYS_ADMIN, which run.sh adds for init's benefit anyway.
 *
 * Two things stay broken, because neither can be repaired from inside a
 * container on a cgroup-v2-only host: the cgroup v1 hierarchies Android expects
 * at /dev/{memcg,cpuctl,cpuset,stune,blkio} (the controllers are all bound to
 * the unified hierarchy, so mount() fails with EPERM/ENODEV), and the
 * activation of the freezer controller through
 * /sys/fs/cgroup/cgroup.subtree_control, which stays EBUSY as long as init
 * itself runs in the namespace root cgroup.  Neither is needed to boot: the
 * only cgroup files init really uses are the uid_/pid_ directories under
 * /sys/fs/cgroup, and those are the same ones this function can repair.
 */
static void setup_cgroup_rw(void) {
    struct statvfs vfs;

    /* With run.sh's "--security-opt unmask=/sys/fs/cgroup" the mount is already
     * writable and there is nothing to do. */
    if (statvfs("/sys/fs/cgroup", &vfs) == 0 && !(vfs.f_flag & ST_RDONLY)) {
        return;
    }

    if (mount("/sys/fs/cgroup", "/sys/fs/cgroup", NULL, MS_BIND, NULL) != 0) {
        fprintf(stderr, "devstage: bind /sys/fs/cgroup: %s\n", strerror(errno));
        return;
    }
    /* MS_REMOUNT | MS_BIND *without* MS_RDONLY.  The other flags have to be
     * restated: a bind remount drops every flag it is not told to keep. */
    if (mount(NULL, "/sys/fs/cgroup", NULL,
              MS_REMOUNT | MS_BIND | MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) != 0) {
        fprintf(stderr, "devstage: remount /sys/fs/cgroup rw: %s\n",
                strerror(errno));
    }
}

/*
 * Waydroid's hwcomposer is an ordinary Wayland client of the host compositor:
 * it owns the Android window, so the compositor sends it the keyboard, pointer
 * and touch events for whoever is focused on that window.  It hands those to
 * Android by writing struct input_event's into four FIFOs:
 *
 *     /dev/input/wl_{keyboard,pointer,tablet,touch}_events
 *
 * hwcomposer (mkfifo() is one of its imported libc symbols) creates the pipes
 * and opens the one for each device the seat advertises; the reader is
 * Waydroid's patched InputReader (libinputreader.so, linked into system_server),
 * which opens them like ordinary evdev nodes and polls them.  No kernel input
 * device is involved, which is exactly what we want here: the events are scoped
 * to our window instead of mirroring the whole host.
 *
 * Both ends run as uid 1000 inside the container (hwcomposer is "user host",
 * system_server is "system"), so all that is missing is the directory itself.
 * Android would normally get it from ueventd -- ueventd.rc has
 *
 *     subsystem input
 *     dirname /dev/input
 *
 * -- but ueventd only acts on kernel uevents, and the kernel broadcasts those
 * to the initial network namespace only, while podman puts us in a netns of our
 * own for pasta.  Without it hwcomposer's mkfifo() fails with ENOENT, the open
 * right after it follows suit, and it logs
 *
 *     hwcomposer: Failed to open pipe to InputFlinger: No such file or directory
 *
 * for every event, so nothing ever reaches Android's input stack.
 *
 * The directory existing is not enough on its own: init must not be watching
 * it.  The LineageOS system_ext rc declares a keychord, and one keychord is all
 * init/keychords.cpp needs to arm an inotify watch on /dev/input and to open
 * every entry in it with a *blocking* open(O_RDONLY) -- which on the tablet
 * FIFO (no writer on a desktop seat) hangs init's main loop for good.  run.sh
 * therefore binds in a copy of that rc without its keycodes line; see the
 * "init's keychord watcher and /dev/input" comment there.
 *
 * 0777 (the mkdir mode is masked, hence the explicit chmod) because neither end
 * runs as root: hwcomposer has to create the FIFOs here and both ends have to
 * open them.
 */
static void setup_dev_input(void) {
    if (mkdir("/dev/input", 0777) != 0 && errno != EEXIST) {
        fprintf(stderr, "devstage: mkdir /dev/input: %s\n", strerror(errno));
        return;
    }
    if (chmod("/dev/input", 0777) != 0) {
        fprintf(stderr, "devstage: chmod /dev/input: %s\n", strerror(errno));
    }
}

/*
 * FALLBACK ONLY -- see "netd's rtnetlink UID ranges" in run.sh for the real
 * fix.  netd programs Android's per-UID routing policy over rtnetlink, and the
 * kernel rejects the rules it needs:
 *
 *     E netd: Error adding IPv4 rule: Invalid argument
 *     W ConnectivityService: Exception adding interface: ... Invalid argument (code 22)
 *     E ConnectivityService: Exception in networkAddRouteParcel ...: No such device (code 19)
 *
 * The rejected messages carry FRA_UID_RANGE attributes whose endpoints come
 * from ConnectivityService -- 0..INT_MAX for the default network -- and the
 * kernel validates both endpoints with make_kuid(current_user_ns(), ...)
 * before accepting the rule (fib_rules.c: unmapped endpoint -> INVALID_UID ->
 * -EINVAL).  A rootless container whose user namespace is only 64k UIDs wide
 * therefore cannot install *any* of the per-UID rules, and with no rule
 * pointing at them the tables that should hold the routes stay empty.
 *
 * (netd's tc/eBPF/iptables setup fails the same way.)  eth0 still gets its
 * DHCP lease, so ConnectivityService reports CONNECTED with INTERNET and a
 * synthetic default route, and yet the kernel has no route at all: netd's rule
 * set ends in
 *
 *     32000:  from all unreachable
 *
 * Everything fails with ENETUNREACH -- including DNS, where netd's resolver
 * tags its query socket with a mark that matches none of the rules but the
 * unreachable one, which is why name lookups die with ENONET even though the
 * interface is up.
 *
 * With a user namespace wide enough to cover uid 2147483647, netd's rules are
 * accepted, it installs the default route into table 1002 by itself and this
 * function is never called (run.sh leaves WD_GATEWAY empty).  It remains for
 * hosts that cannot widen /etc/subuid.  It gives the kernel a last-resort rule
 * into the main table and puts the real default route there, so traffic whose
 * mark walks off the end of netd's rules still gets out.
 *
 *     ip rule add pref 31000 from all lookup main
 *     ip route replace default via <gw> dev eth0
 *
 * The gateway is the host's own default gateway: pasta hands the container the
 * host's address, gateway and DNS server, and run.sh passes the gateway in
 * WD_GATEWAY.  We also seed table 97 ("local_network"), which Android's own
 * rules already point at, so the ordinary app path keeps working by itself.
 *
 * The commands are re-run for a while: netd installs its rules and flushes the
 * tables it owns when the network comes up, a few seconds after we start, so
 * anything installed before that is discarded.  Two minutes is plenty -- once
 * netd has brought the network up it leaves the routing alone.
 */
#define NETFIX_ROUTE_TABLE_LOCAL_NETWORK "97"
#define NETFIX_RULE_PRIORITY "31000"

static int run_ip(char *const argv[]) {
    pid_t pid = fork();
    if (pid == 0) {
        execv("/system/bin/ip", argv);
        _exit(127);
    }
    if (pid < 0) {
        return -1;
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        return -1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static void netfix(void) {
    const char *gw = getenv("WD_GATEWAY");
    if (gw == NULL || *gw == '\0') {
        return;
    }

    /* "File exists" (our rule) and "Network is unreachable" (eth0 has no
     * address yet) are both expected while we wait; keep them out of the log. */
    int devnull = open("/dev/null", O_WRONLY | O_CLOEXEC);
    if (devnull >= 0) {
        dup2(devnull, STDERR_FILENO);
        if (devnull > STDERR_FILENO) {
            close(devnull);
        }
    }

    int ok = 0;
    for (int i = 0; i < 60; i++) {
        char *rule[] = {"ip", "rule", "add", "pref", NETFIX_RULE_PRIORITY,
                        "from", "all", "lookup", "main", NULL};
        char *route[] = {"ip", "route", "replace", "default", "via", (char *)gw,
                         "dev", "eth0", "onlink", NULL};
        char *local[] = {"ip", "route", "replace", "default", "via", (char *)gw,
                         "dev", "eth0", "table",
                         NETFIX_ROUTE_TABLE_LOCAL_NETWORK, "onlink", NULL};
        (void)run_ip(rule);
        ok = run_ip(route) == 0;
        (void)run_ip(local);
        usleep(2 * 1000 * 1000);
    }

    if (!ok) {
        const char *msg = "<6>devstage: netfix: could not add the default route\n";
        int kmsg = open("/dev/kmsg", O_WRONLY | O_APPEND | O_CLOEXEC);
        if (kmsg >= 0) {
            ssize_t ignored = write(kmsg, msg, strlen(msg));
            (void)ignored;
            close(kmsg);
        }
        fprintf(stderr, "devstage: netfix: could not add the default route\n");
    }
}

int main(void) {
    setup_binderfs();
    setup_mnt_tmpfs();
    setup_proc_sys();
    setup_cgroup_rw();
    setup_dev_input();

    /*
     * Hand the route repair to a child: netd's own routing is broken here (see
     * netfix()), and the route cannot be installed before IpClient has the
     * DHCP lease, which is long after init has taken over PID 1.
     */
    if (fork() == 0) {
        netfix();
        _exit(0);
    }

    execv("/init", (char *[]){(char *)"init", (char *)"second_stage", NULL});
    fprintf(stderr, "devstage: execv /init: %s\n", strerror(errno));
    return 1;
}
