#!/usr/bin/env python3
"""List and compare the L1 VM's run directories and archives

Usage: copied_home.py list <results dir> <archive dir>
       copied_home.py compare <listing> <host results dir> <batch name> <label...>
  list      sha256 of each run file and archive
  compare   which entries the host already holds
"""
import hashlib
import os
import re
import sys
from pathlib import Path

SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
RUN_DIR = re.compile(r".+-r[0-9]+|.+\.partial-.+")
ARCHIVE = re.compile(r".+-runs\.tgz")


def sha256(path):
    """return the sha256 hex digest of a file"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def shown(name):
    """return a name as a printable field, or its repr when it is not one"""
    return name if name.isprintable() and "\t" not in name else repr(name)


def listing(results, archives):
    """return the listing lines of the run directories and archives"""
    out = []
    res = Path(results)
    for d in sorted(res.iterdir()) if res.is_dir() else []:
        if not RUN_DIR.fullmatch(d.name) or d.is_symlink() or not d.is_dir():
            continue
        out.append(f"dir\t{shown(d.name)}")
        for top, dirs, files in os.walk(d):
            dirs.sort()
            for n in sorted(files) + [x for x in dirs if Path(top, x).is_symlink()]:
                p = Path(top, n)
                rel = str(p.relative_to(res))
                if shown(rel) != rel:
                    out.append(f"other\t{shown(d.name)}")
                elif p.is_file() and not p.is_symlink():
                    out.append(f"file\t{sha256(p)}\t{rel}")
                else:
                    out.append(f"other\t{rel}")
    archive_dir = Path(archives)
    for f in sorted(archive_dir.iterdir()) if archive_dir.is_dir() else []:
        if ARCHIVE.fullmatch(f.name) and f.is_file() and not f.is_symlink():
            out.append(f"archive\t{sha256(f)}\t{shown(f.name)}")
    return out


def compare(lines, host, name, labels):
    """return the remove and keep lines for a listing against the host's results dir"""
    dirs, archives = {}, {}
    for line in lines:
        f = line.rstrip("\n").split("\t")
        if f[0] == "dir" and len(f) == 2:
            dirs.setdefault(f[1], {"files": {}, "other": False})
        elif f[0] == "file" and len(f) == 3:
            dirs.setdefault(f[2].split("/", 1)[0], {"files": {}, "other": False})["files"][f[2]] = f[1]
        elif f[0] == "other" and len(f) == 2:
            dirs.setdefault(f[1].split("/", 1)[0], {"files": {}, "other": False})["other"] = True
        elif f[0] == "archive" and len(f) == 3:
            archives[f[2]] = f[1]
    host = Path(host)
    own = re.compile("|".join([re.escape(name) + r"\.partial-.+"] + [re.escape(lab) + r"-r[0-9]+" for lab in labels]))
    out = []
    for d in sorted(dirs):
        why = None
        if not SAFE.fullmatch(d):
            why = "not a name the cleanup handles"
        elif own.fullmatch(d):
            why = "this batch's own"
        elif dirs[d]["other"]:
            why = "holds a file that is not regular or whose name the cleanup does not handle"
        elif not (host / d).is_dir() or (host / d).is_symlink():
            why = "not on the host"
        else:
            for rel, want in sorted(dirs[d]["files"].items()):
                p = host / rel
                if not p.is_file():
                    why = f"{rel} is not on the host"
                elif sha256(p) != want:
                    why = f"{rel} differs on the host"
                if why:
                    break
        out.append(f"keep dir {d}: {why}" if why else f"remove dir {d}")
    for a in sorted(archives):
        copies = sorted(p for p in host.glob(f"*/{a}") if p.is_file()) if SAFE.fullmatch(a) else []
        if not SAFE.fullmatch(a):
            why = "not a name the cleanup handles"
        elif a == f"{name}-runs.tgz":
            why = "this batch's own"
        elif not copies:
            why = "not on the host"
        else:
            same = [p for p in copies if sha256(p) == archives[a]]
            why = None if same else "differs on the host"
        out.append(f"keep archive {a}: {why}" if why else f"remove archive {a}, same as {same[0]}")
    return out


def main(argv):
    """run list or compare and return the exit code"""
    if len(argv) == 3 and argv[0] == "list":
        print("\n".join(listing(argv[1], argv[2])))
        return 0
    if len(argv) >= 4 and argv[0] == "compare":
        try:
            lines = Path(argv[1]).read_text().splitlines()
        except OSError as e:
            print(f"copied_home: cannot read {argv[1]}: {e}", file=sys.stderr)
            return 2
        print("\n".join(compare(lines, argv[2], argv[3], argv[4:])))
        return 0
    print(__doc__.split("\n", 2)[2], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
