#!/usr/bin/env python3
#
# Copyright 2017 Federico Ceratto <federico@debian.org>
# Released under GPLv3 License, see LICENSE file

from pathlib import Path
import hashlib
import os
import sys

from argparse import ArgumentParser, RawDescriptionHelpFormatter
from configparser import ConfigParser
from subprocess import Popen, PIPE

default_conf = """
[DEFAULT]
config_version = 1

# where all the layers are stored
cachedir = /var/cache/conbuilder

# where to copy the generated .deb .changes .dsc ... files
export_dir = ../build-area/

tarball_dir = ../tarballs/

# one or more capabilities to drop during the build.
# L1 and L2 creation is not affected.
# see man systemd-nspawn
# drop_capability =
#
# Some capabilities that can be disabled with most builds:
# drop_capability = CAP_CHOWN,CAP_DAC_READ_SEARCH,CAP_FOWNER,CAP_FSETID,CAP_IPC_OWNER,CAP_KILL,CAP_LEASE,CAP_LINUX_IMMUTABLE,CAP_NET_BIND_SERVICE,CAP_NET_BROADCAST,CAP_NET_RAW,CAP_SETGID,CAP_SETFCAP,CAP_SETPCAP,CAP_SETUID,CAP_SYS_ADMIN,CAP_SYS_CHROOT,CAP_SYS_NICE,CAP_SYS_PTRACE,CAP_SYS_TTY_CONFIG,CAP_SYS_RESOURCE,CAP_SYS_BOOT,CAP_AUDIT_WRITE,CAP_AUDIT_CONTROL

# one or more capabilities to drop during the build.
# L1 and L2 creation is not affected.
# see man systemd-nspawn
system_call_filter = ""

# purge layer 2 trees older than:
l2_max_age_days = 30

# *also* purge older layers 2 if there are more than:
l2_max_number = 10

# colors - ANSI escape codes 38;2;⟨r⟩;⟨g⟩;⟨b⟩
# color_info = "38;2;0;98;149"
# color_error = "38;2;200;0;0"
# color_success = "38;2;0;200;0"

"""

help_msg = """
Build Debian packages using overlay FS and systemd namespace containers
conbuilder creates a base filesystem using debootstrap, then
overlays it with a filesystem to install the required dependencies
and finally runs the build on another overlay.

Layers are created, reused and purged automatically to achieve
fast package builds while minimizing disk usage.
conbuilder also allows you to selectively disable networking,
system calls and capabilities.

show:
    show running containers, filesystem layers and overlay mounts

create:
    create new base system using debootstrap.
    Use --codename to pick sid, wheezy etc..

    $ conbuilder create --codename wheezy

    New base systems are created automatically by "build" if
    needed.

update:
    update base system using debootstrap

    $ conbuilder update --codename wheezy

build:
    build package using dpkg-buildpackage
    Creates an overlay called L2 if not already available.
    Options after '--' will be passed to dpkg-buildpackage

install:
    install built package and its dependencies using "debi"
    Creates a temporary overlay FS

Default configuration:
--
{conf}
--

""".format(
    conf=default_conf
)


colors = None


def info(*a):
    s = " ".join(a)
    c = colors["info"]
    print("\x1b[{}m{}\x1b[0m".format(c, s))


def error(*a):
    s = " ".join(a)
    c = colors["error"]
    print("\033[{}m{}\033[0m".format(c, s))


def run(cmd: str, quietcmd=False, quiet=False) -> list:
    """Run command, capture output
    """
    assert isinstance(cmd, str), repr(cmd)
    if not quietcmd:
        info(cmd)
    proc = Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE)
    out = []
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.rstrip().decode()
        out.append(line)
        if not quiet:
            info(line)

    proc.wait()
    if proc.returncode != 0:
        error("-- Error --")
        err = proc.stderr.read().decode()
        error(err)
        error("-----------")
        raise Exception("'{}' returned {}".format(cmd, proc.returncode))

    return out


def mount(lower, upper, work, mnt: Path):
    cmd = "sudo mount -t overlay overlay " "-olowerdir={},upperdir={},workdir={} {}"
    cmd = cmd.format(lower, upper, work, mnt)
    run(cmd)
    assert (mnt / "usr/bin/apt").is_file()


def umount(path):
    run("sudo umount {}".format(path))


def nspawn(rcmd: str, quiet=False, drop_capability="", system_call_filter="") -> list:
    cmd = "sudo systemd-nspawn -M conbuilder --chdir=/srv "
    if drop_capability:
        cmd += "--drop-capability={} ".format(drop_capability)
    if system_call_filter:
        cmd += "--system-call-filter={} ".format(system_call_filter)
    cmd += rcmd
    return run(cmd, quiet=quiet)


def _parse_build_deps(out: list) -> list:
    """Parse build deps from apt-get build-dep
    """
    deps = set()
    for line in out:
        if not line.startswith("Inst "):
            continue
        # Example: Inst gettext (0.19.8.1-4 Debian:unstable [amd64]) []
        _, pkgname, version, _1 = line.split(" ", 3)
        assert version.startswith("("), "Cannot parse version from %r" % line
        version = version[1:]
        assert version, "Cannot parse version from %r" % line
        deps.add((pkgname, version))

    return sorted(deps)


def extract_build_dependencies(l1dir):
    """Run apt-get build-deps in simulated mode on a read-only FS that
    contains only the base system and the pkg sources

    :returns: ([('pkgname', 'version'), ... ], 'fingerprint')
    """
    cmd = "-D %s --read-only " % l1dir
    cmd += "--overlay=$(pwd)::/srv  -- /usr/bin/apt-get build-dep -s ."
    out = nspawn(cmd, quiet=True)
    deps = _parse_build_deps(out)

    # generate deterministic fingerprint
    block = (str(sorted(deps))).encode()
    fprint = hashlib.sha224(block).hexdigest()[:10]
    return (deps, fprint)


def load_conf_and_parse_args():
    confighome = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    default_conf_fn = confighome / "conbuilder.conf"

    ap = ArgumentParser(epilog=help_msg, formatter_class=RawDescriptionHelpFormatter)
    ap.add_argument(
        "--conf",
        default=default_conf_fn,
        help="Config file path (default: {})".format(default_conf_fn),
    )
    ap.add_argument(
        "action", choices=["create", "update", "build", "install", "purge", "show"]
    )
    ap.add_argument("--codename", default="sid", help="codename (default: sid)")
    ap.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="increase verbosity up to 3 times",
    )
    ap.add_argument("extra_args", nargs="*")  # for dpkg-buildpackage
    args = ap.parse_args()
    if args.extra_args:
        if args.action not in ("build", "install"):
            ap.error(
                "Extra arguments should be passed only during build \
                     or install"
            )

    # generate default conf file if needed
    if args.conf == default_conf_fn and not os.path.isfile(args.conf):
        info("Configuration file not found. Generating {}".format(args.conf))
        with open(args.conf, "w") as f:
            f.write(default_conf)

    cp = ConfigParser()
    with open(args.conf) as f:
        cp.read_file(f)
    args.cachedir = Path(cp["DEFAULT"]["cachedir"])
    assert args.cachedir not in ("", "/"), "Invalid cache dir"
    args.export_dir = cp["DEFAULT"]["export_dir"]

    drop_capability = cp["DEFAULT"].get("drop_capability", "")
    args.drop_capability = drop_capability.strip().replace(", ", ",")
    args.system_call_filter = cp["DEFAULT"].get("system_call_filter", "")
    args.color_info = cp["DEFAULT"].get("color_info", "38;2;0;98;149")
    args.color_error = cp["DEFAULT"].get("color_error", "38;2;200;0;0")
    args.color_success = cp["DEFAULT"].get("color_success", "38;2;0;200;0")
    return args


def create_l1(conf, l1dir: Path):
    """Run debootstrap to create the L1 FS
    """
    if l1dir.exists():
        error("Error: the base filesystem (L1) already exists")
        sys.exit(1)
    info("Creating", l1dir)
    l1dir.mkdir(parents=True)
    cmd = (
        "sudo debootstrap --include=apt "
        " --force-check-gpg {} {} http://deb.debian.org/debian"
    )
    cmd = cmd.format(conf.codename, l1dir)
    run(cmd)
    assert (l1dir / "usr/bin/apt").is_file()
    assert (l1dir / "etc").is_dir(), "/etc not found in {} ".format(l1dir)


def update_l1(conf, l1dir):
    """Update the L1 FS
    """
    # TODO invalidate L2s
    info(f"Updating {l1dir}")
    nspawn("-D {} -- /usr/bin/apt-get -y update".format(l1dir))
    nspawn(
        "-D {} -E DEBIAN_FRONTEND=noninteractive -- /usr/bin/apt-get -y dist-upgrade".format(
            l1dir
        )
    )


def create_l2(conf, l1dir, l2dir, l2workdir, l2mountdir, expected_deps):
    """Run apt-get build-deps in the L2 FS to install dependencies
    """
    info("[L2] Creating", l2dir)
    os.makedirs(l2dir)
    os.makedirs(l2workdir)
    os.makedirs(l2mountdir)
    try:
        mount(l1dir, l2dir, l2workdir, l2mountdir)

        deps_list = ["{}:{}".format(name, ver) for name, ver in expected_deps]
        info("[L2] Installing dependencies...")
        if conf.verbose == 0:
            info("[L2]", " ".join(deps_list))

        cmd = "-D {} -E DEBIAN_FRONTEND=noninteractive --overlay=$(pwd)::/srv  -- /usr/bin/apt-get build-dep -y ."
        cmd = cmd.format(l2mountdir)
        out = nspawn(cmd, quiet=(conf.verbose == 0))
        _parse_build_deps(out)
        cmd = "-D {} --overlay=$(pwd)::/srv  -- /usr/bin/apt-get clean"
        with (l2mountdir / ".deps.conbuilder").open("w") as f:
            f.write("\n".join(deps_list))

    finally:
        umount(l2mountdir)


def build(conf):
    """Run a package build
    """
    success = False

    # L1: base system
    l1dir = conf.cachedir / "l1" / conf.codename
    if not l1dir.exists():
        create_l1(conf, l1dir)

    # L2: build dependencies

    deps, dep_hash = extract_build_dependencies(l1dir)

    l2dir = conf.cachedir / "l2" / "fs" / dep_hash
    l2workdir = conf.cachedir / "l2" / "overlay_work" / dep_hash
    l2mountdir = conf.cachedir / "l2" / "overlay_mount" / dep_hash
    info("[L1] Ready")

    if not os.path.exists(l2dir):
        create_l2(conf, l1dir, l2dir, l2workdir, l2mountdir, deps)

    try:
        mount(l1dir, l2dir, l2workdir, l2mountdir)
        info("[L2] Ready")

        l3dir = conf.cachedir / "l3" / "fs" / dep_hash
        l3workdir = conf.cachedir / "l3" / "overlay_work" / dep_hash
        l3mountdir = conf.cachedir / "l3" / "overlay_mount" / dep_hash
        if not os.path.exists(l3dir):
            info("[L3] Creating", l3dir)
            os.makedirs(l3dir)
            os.makedirs(l3workdir)
            os.makedirs(l3mountdir)
        try:
            mount(l2mountdir, l3dir, l3workdir, l3mountdir)
            run("sudo cp -a . {}".format(l3mountdir / "srv"))
            # TODO: configurable --private-network
            cmd = "--private-network -D {} -- /usr/bin/dpkg-buildpackage {}"
            cmd = cmd.format(l3mountdir, " ".join(conf.extra_args))
            nspawn(
                cmd,
                drop_capability=conf.drop_capability,
                system_call_filter=conf.system_call_filter,
            )
            success = True

        finally:
            umount(l3mountdir)

    finally:
        umount(l2mountdir)

    if not success:
        return

    if conf.export_dir:
        dest = os.path.abspath(conf.export_dir)
        exts = ("deb", "changes", "xz", "gz", "buildinfo", "dsc")
        for e in exts:
            cmd = "cp -a {}/*.{} {}/ || true".format(l3dir, e, dest)
            run(cmd)

        info("\n[Success]")

    else:
        info("\n[Success] Output is at {}".format(l3dir))


def install(conf):
    """Create temporary layer, install package and deps
    """
    # L1: base system
    l1dir = conf.cachedir / "l1" / conf.codename
    if not l1dir.exists():
        create_l1(conf, l1dir)

    # L2t: install
    dep_hash = "foo"
    l2dir = conf.cachedir / "l2i" / "fs" / dep_hash
    l2workdir = conf.cachedir / "l2i" / "overlay_work" / dep_hash
    l2mountdir = conf.cachedir / "l2i" / "overlay_mount" / dep_hash

    info("[L2i] Creating", l2dir)
    for d in (l2dir, l2workdir, l2mountdir):
        d.mkdir(parents=True, exist_ok=True)

    try:
        mount(l1dir, l2dir, l2workdir, l2mountdir)
        info("[L2] Ready")

        deb_fnames = conf.extra_args
        for fn in deb_fnames:
            cmd = "sudo cp -a {} {}/srv/".format(fn, l2mountdir)
            run(cmd)
        cmd = "-D {} -- /usr/bin/apt install -y /srv/safeeyes_2.0.0-2_all.deb"
        cmd = "-D {} -- /bin/bash"
        cmd = cmd.format(l2mountdir)
        nspawn(cmd)

    finally:
        umount(l2mountdir)


def show(conf):
    info("Mounted overlays:")
    run("mount | grep ^overlay | cat", quietcmd=True)

    info("Running containers:")
    run("machinectl list | grep conbuilder | cat", quietcmd=True)

    info("Layers:")
    for nick, path in (("L1", "l1"), ("L2", "l2/fs"), ("L3", "l3/fs")):
        info("  {}:".format(nick))
        d = conf.cachedir / path
        if not d.exists():
            continue
        for item in d.iterdir():
            size = run("sudo du -hs {}".format(item), quietcmd=True, quiet=True)
            size = size[0].split("\t")[0]
            info("    {:35} {}".format(item.name, size))
            deps_fn = item / ".deps.conbuilder"
            if not deps_fn.is_file():
                continue
            with deps_fn.open() as f:
                for line in f:
                    info("     ", line.rstrip())
            # TODO add age
            info("")
        info("")


def main():
    global colors
    conf = load_conf_and_parse_args()
    colors = dict(
        info=conf.color_info, error=conf.color_error, success=conf.color_success
    )

    if conf.action == "create":
        l1dir = conf.cachedir / "l1" / conf.codename
        create_l1(conf, l1dir)

    elif conf.action == "update":
        l1dir = conf.cachedir / "l1" / conf.codename
        update_l1(conf, l1dir)

    if conf.action == "build":
        build(conf)

    elif conf.action == "install":
        install(conf)

    elif conf.action == "purge":
        # TODO
        raise NotImplementedError

    elif conf.action == "show":
        show(conf)


if __name__ == "__main__":
    main()
