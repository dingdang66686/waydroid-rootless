# Copyright 2025 Alessandro Astone
# SPDX-License-Identifier: GPL-3.0-or-later
import tools.config
import dbus

import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

WAYDROID = sys.argv[0]
TARBALL = "waydroid-bugreport.tar.xz"

def logcat(args, output_path):
    cmd = [WAYDROID, "logcat"]
    if tools.helpers.runtime.needs_root(args):
        cmd = ["sudo"] + cmd
    with open(output_path, "w+") as fd:
        return subprocess.Popen(cmd, stdout=fd, stderr=sys.stderr, stdin=None)

def dmesg(args, params, output_path):
    cmd = ["dmesg", "-T"] + params
    if os.geteuid() != 0:
        if not shutil.which("sudo"):
            with open(output_path, "w") as fd:
                fd.write("dmesg skipped: sudo is not available\n")
            return None
        if subprocess.run(["sudo", "-n", "true"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode != 0:
            with open(output_path, "w") as fd:
                fd.write("dmesg skipped: host kernel logs require root "
                         "(passwordless sudo is not available)\n")
            return None
        cmd = ["sudo", "-n"] + cmd
    with open(output_path, "w+") as fd:
        return subprocess.Popen(cmd, stdout=fd, stderr=fd, stdin=None)

def waydroid_session(output_path):
    with open(output_path, "w+") as fd:
        return subprocess.Popen([WAYDROID, "session", "start"], stdout=fd, stderr=fd, stdin=None, start_new_session=True)

def clear_line():
    sys.stdout.write("\33[2K\r")

def sleep_progress(seconds):
    bar_length = 50
    time_increment = 0.1
    spinner_chars = ['|', '/', '-', '\\']

    iteration = 0
    time_slept = 0

    try:
        while time_slept < seconds:
            spinner_char = spinner_chars[iteration % len(spinner_chars)]
            filled_length = int(bar_length * time_slept // seconds)
            sys.stdout.write(f"[{'=' * filled_length}{' ' * (bar_length - filled_length)}] {spinner_char}\r")
            sys.stdout.flush()

            time.sleep(time_increment)
            time_slept += time_increment
            iteration += 1
        clear_line()
    except KeyboardInterrupt:
        clear_line()
        print("Interrupted.")


def bugreport(args):
    tmp = tempfile.mkdtemp()

    print("""\
The following information will be collected:
  - System kernel logs (dmesg; optional, requires root)
  - Android system and user logs (logcat)
  - Waydroid container manager logs (<work>/waydroid.log)
  - Waydroid configuration files (<work>/*)

The information will be stored on your machine.

Host kernel logs are optional; if passwordless sudo is unavailable they are
skipped and the rest of the report is still collected.
""")

    procs = []
    logfiles = []
    def logfile(name):
        file = os.path.join(tmp, name)
        logfiles.append(file)
        return file

    session = None
    try:
        session = tools.helpers.ipc.DBusContainerService(args).GetSession()
    except dbus.DBusException:
        pass

    if not session:
        print("Waydroid session not found. Trying to start one...")
        procs.append(waydroid_session(logfile("session.txt")))
        sleep_progress(10)
        try:
            session = tools.helpers.ipc.DBusContainerService(args).GetSession()
        except dbus.DBusException:
            pass

    if session:
        print("\n\
\033[1mPlease try to reproduce the problem now.\033[0m\n\
Waydroid will collect logs for up to 5 minutes. You may interrupt this operation earlier.\n\
")
        procs.append(logcat(args, logfile("logcat.txt")))
        procs.append(dmesg(args, ["-w"], logfile("dmesg.txt")))
        sleep_progress(5 * 60)
    else:
        print("Session did not start\n")
        procs.append(dmesg(args, [], logfile("dmesg.txt")))

    procs = [p for p in procs if p is not None]
    for p in procs:
        p.terminate()
    for p in procs:
        p.wait()

    print("Creating archive...")

    try:
        with tarfile.open(TARBALL, "w:xz", preset=9) as tar:
            files = [
                args.work + "/waydroid.log",
                args.work + "/waydroid.cfg",
                args.work + "/waydroid_base.prop",
                args.work + "/waydroid.prop",
                args.work + "/lxc",
            ] + logfiles

            for f in files:
                try:
                    tar.add(f, arcname=os.path.basename(f), recursive=True)
                except Exception as e:
                    logging.debug(f"Failed to archive {f}: {repr(e)}")

    except Exception as e:
        raise e
    finally:
        shutil.rmtree(tmp)

    print(f"Created \033[1m{TARBALL}\033[0m")
