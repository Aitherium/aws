#!/bin/sh
# aws-public-slim.sh SRC_IMAGE DST_IMAGE
# Single-layer copy of SRC with every path matching $EXCLUDE_RE removed. Deleting files in
# a new layer does not remove them from an image (the old layer still ships them), so the
# root filesystem is exported, filtered and re-imported from scratch; the runtime config
# (entrypoint, cmd, user, workdir, env, ports, labels) is carried over.
# EXCLUDE_RE is supplied by the caller (a CI secret) and is never stored in this repo.
set -eu
SRC="$1"; DST="$2"
: "${EXCLUDE_RE:?EXCLUDE_RE must be set}"
W=$(mktemp -d /var/tmp/slim.XXXXXX)
trap 'rm -rf "$W"; podman rm -f slim-src >/dev/null 2>&1 || true' EXIT
podman pull -q "$SRC" >/dev/null
CFG=$(podman image inspect "$SRC" --format '{{json .Config}}')
podman create --name slim-src "$SRC" true >/dev/null
podman export slim-src > "$W/root.tar"
podman rm -f slim-src >/dev/null
python3 - "$W/root.tar" "$W/slim.tar" <<'PY'
import os, re, sys, tarfile
src, dst = sys.argv[1], sys.argv[2]
rx = re.compile(os.environ["EXCLUDE_RE"])
kept = dropped = 0
with tarfile.open(src) as i, tarfile.open(dst, "w") as o:
    for m in i:
        if rx.search(m.name):
            dropped += 1
            continue
        o.addfile(m, i.extractfile(m) if m.isfile() else None)
        kept += 1
print(f"kept={kept} dropped={dropped}")
PY
rm -f "$W/root.tar"
ARGS=$(python3 - "$CFG" <<'PY'
import json, shlex, sys
c = json.loads(sys.argv[1]) or {}
out = []
def ch(s): out.append("--change=" + s)
if c.get("Entrypoint"): ch("ENTRYPOINT " + json.dumps(c["Entrypoint"]))
if c.get("Cmd"): ch("CMD " + json.dumps(c["Cmd"]))
if c.get("User"): ch("USER " + c["User"])
if c.get("WorkingDir"): ch("WORKDIR " + c["WorkingDir"])
for e in c.get("Env") or []:
    k, _, v = e.partition("=")
    ch(f"ENV {k}={json.dumps(v)}")
for p in (c.get("ExposedPorts") or {}): ch("EXPOSE " + p)
for k, v in (c.get("Labels") or {}).items(): ch(f"LABEL {k}={json.dumps(v)}")
ch('LABEL org.opencontainers.image.source="https://github.com/Aitherium/aws"')
print(" ".join(shlex.quote(x) for x in out))
PY
)
eval podman import $ARGS "$W/slim.tar" "$DST" >/dev/null
echo "built $DST"
