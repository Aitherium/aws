#!/usr/bin/env python3
"""Fail if any layer of IMAGE holds a path matching $EXCLUDE_RE.

Every layer's member list is read (podman save), not the final filesystem: a file
deleted in a later layer still ships in the earlier one. Exit 0 clean, 1 match found,
2 could not judge (save failed, no layers read, EXCLUDE_RE unset). Never 0 on silence.
"""
import os, re, subprocess, sys, tarfile, tempfile
from pathlib import Path


def scan(image: str) -> int:
    rx_src = os.environ.get("EXCLUDE_RE", "")
    if not rx_src:
        print("could not judge: EXCLUDE_RE unset"); return 2
    rx = re.compile(rx_src)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "img.tar"
        r = subprocess.run(["podman", "save", "-o", str(out), image], capture_output=True, text=True)
        if r.returncode != 0 or not out.is_file():
            print(f"could not judge {image}: save failed"); return 2
        hits = layers = files = 0
        with tarfile.open(out) as outer:
            for m in outer:
                if not m.isfile():
                    continue
                try:
                    with tarfile.open(fileobj=outer.extractfile(m)) as layer:
                        layers += 1
                        for x in layer:
                            files += 1
                            hits += bool(rx.search(x.name))
                except tarfile.TarError:
                    continue
    if layers == 0:
        print(f"could not judge {image}: no layers read"); return 2
    print(f"{image}: {hits} excluded path(s) across {layers} layer(s), {files} files")
    return 1 if hits else 0


def self_test() -> int:
    os.environ["EXCLUDE_RE"] = r"(^|/)secret_dir(/|$)"
    rx = re.compile(os.environ["EXCLUDE_RE"])
    ok = (bool(rx.search("app/lib/secret_dir/x.py")) and not rx.search("app/lib/secret_dirx.py"))
    print("self-test " + ("passed" if ok else "FAILED")); return 0 if ok else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    sys.exit(max(scan(i) for i in sys.argv[1:]) if sys.argv[1:] else 2)
