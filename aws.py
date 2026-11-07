#!/usr/bin/env python3
"""aws -- Aither World Services: the sovereign private cloud, one command.

    aws catalog              # the AWS map + what the bundle ships
    aws status               # live health of every catalog row
    aws status --target sovereign
    aws init --domain acme.example   # .env: domain + random secrets
    aws up                   # preflight only, changes nothing
    aws up --apply           # bring the customer bundle up
    aws --version

Design: `.AITHEROS/43-AITHER-WORLD-SERVICES.md`. Stack `aws` in `AitherOS/config/ecosystem.yaml`.

Two targets, because they are two different stacks and conflating them was the defect:
  fleet      the owner's running fleet (awnix quadlets, `aither-*` / `aitheros-*` names)
  sovereign  the CUSTOMER bundle, `.DEPLOYMENT/compose/docker-compose.sovereign.yml`
             (`sovereign-*` names). `catalog` derives which rows it ships FROM THAT FILE.

`up` targets the customer bundle only. Preflight is the default: a command that starts
twenty containers on a typo is not a convenience. `--apply` runs `compose up -d` and then
re-judges with `status --target sovereign` instead of trusting the exit of `up`.

Exit 0 every required row healthy / every preflight step true · 1 a row absent or
unhealthy, or a preflight step false · 2 could not judge (engine unreachable, compose
unreadable). Never 0 on silence. `--self-test` proves each verdict can still fire.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

VERSION = "0.6.0"
# Standalone (github.com/Aitherium/aws): the compose files ship beside this script.
# Inside the AitherOS monorepo the same file finds them under .DEPLOYMENT/compose.
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2] if (HERE.parents[2] / ".DEPLOYMENT").is_dir() else HERE
SOV_COMPOSE = (REPO / ".DEPLOYMENT" / "compose" / "docker-compose.sovereign.yml"
               if (REPO / ".DEPLOYMENT").is_dir() else HERE / "compose" / "docker-compose.sovereign.yml")
SERVICES_YAML = REPO / "AitherOS" / "config" / "services.yaml"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# One row per Amazon service. `fleet` = name tokens of the owner's containers;
# `sovereign` = compose service names in the customer bundle; `svc` = services.yaml
# key (port). `optional` rows (remote-node provisioning) never fail a single box.
CATALOG: List[Dict[str, object]] = [
    {"aws": "IAM", "part": "Identity + RBAC (SecurityCore)", "svc": "Identity",
     "fleet": ["security-core"], "sovereign": ["identity"], "bricks": ["awiam", "awbac"]},
    {"aws": "Directory Service", "part": "Directory", "svc": "Directory",
     "fleet": ["directory"], "sovereign": ["directory"], "bricks": ["awiam"]},
    {"aws": "Secrets Manager / KMS / ACM", "part": "Secrets + AitherCA", "svc": "Secrets",
     "fleet": ["secrets"], "sovereign": ["secrets"], "bricks": ["awseal"]},
    {"aws": "S3", "part": "Strata", "svc": "Strata",
     "fleet": ["strata"], "sovereign": ["strata"], "bricks": []},
    {"aws": "CloudWatch", "part": "Pulse + Watch", "svc": "Pulse",
     "fleet": ["pulse", "watch"], "sovereign": ["prometheus", "grafana"], "bricks": []},
    {"aws": "CloudTrail", "part": "Chronicle", "svc": "Chronicle",
     "fleet": ["chronicle"], "sovereign": ["chronicle"], "bricks": ["awdit"]},
    {"aws": "EventBridge / Kinesis", "part": "Flux", "svc": "Flux",
     "fleet": ["flux"], "sovereign": ["flux"], "bricks": []},
    {"aws": "SNS / SQS", "part": "Relay (in communication-core)", "svc": "Relay",
     "fleet": ["communication-core"], "sovereign": ["mail"], "bricks": ["awrelay"]},
    {"aws": "Knowledge / vector store", "part": "Nexus", "svc": "Nexus",
     "fleet": ["nexus"], "sovereign": ["qdrant", "graph"], "bricks": []},
    {"aws": "PrivateLink / tunnel", "part": "cloudflared tunnel", "svc": None,
     "fleet": ["cloudflared"], "sovereign": ["cloudflared"], "bricks": ["awtunnel"],
     "optional": True},
    {"aws": "EC2 / Batch", "part": "AitherComet (remote nodes) + awrun", "svc": "Comet",
     "fleet": ["comet"], "sovereign": ["comet"], "bricks": ["awrun"], "optional": True},
]


def _out(text: str) -> None:
    """Print without dying on a cp1252 console (the sovereign_install crash)."""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(text.encode(enc, errors="replace").decode(enc))


def _run(argv: Sequence[str], timeout: int = 300) -> Tuple[int, str]:
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        return 2, f"could not run {argv[0]}: {exc}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def engine_argv() -> List[str]:
    """podman on this host, or inside the fleet distro from Windows."""
    override = os.environ.get("AWS_ENGINE")
    if override:
        return override.split()
    if sys.platform == "win32":
        return ["wsl", "-d", os.environ.get("AITHER_FLEET_DISTRO", "awnix"),
                "-u", "root", "podman"]
    return ["podman"] if shutil.which("podman") else ["docker"]


def sovereign_services(path: Path = SOV_COMPOSE) -> List[str]:
    """Compose service names the customer bundle actually ships. Raises when unreadable."""
    import yaml  # local: --version and --self-test need no third-party import
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    names = sorted((data.get("services") or {}).keys())
    if not names:
        raise ValueError(f"{path} defines no services")
    return names


def ports() -> Dict[str, int]:
    try:
        import yaml
        svcs = (yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8")) or {}).get(
            "services") or {}
        return {k: v.get("port") for k, v in svcs.items() if isinstance(v, dict)}
    except Exception:  # noqa: BLE001 -- ports are decoration in `catalog`, never a verdict
        return {}


def classify(state: str, status: str) -> str:
    s = (status or "").lower()
    if (state or "").lower() != "running":
        return "down"
    if "unhealthy" in s:
        return "unhealthy"
    if "starting" in s:
        return "starting"
    return "healthy" if "healthy" in s else "running"


def match(rows: List[Dict[str, str]], token: str, prefix_re: str) -> List[Dict[str, str]]:
    rx = re.compile(rf"^{prefix_re}{re.escape(token)}(-[a-z0-9]+)?$")
    return [r for r in rows if rx.match(r["name"])]


def judge(containers: List[Dict[str, str]], target: str) -> Tuple[int, List[str]]:
    """Pure: container rows -> (exit code, report lines). The self-test drives this."""
    prefix = r"(aither|aitheros)-" if target == "fleet" else r"sovereign-"
    key = "fleet" if target == "fleet" else "sovereign"
    lines, failed = [], 0
    for row in CATALOG:
        tokens: List[str] = list(row[key])  # type: ignore[arg-type]
        optional = bool(row.get("optional"))
        if not tokens:
            lines.append(f"  {'n/a':<10} {row['aws']:<28} {row['part']} -- not in this target")
            continue
        verdicts = []
        for tok in tokens:
            found = match(containers, tok, prefix)
            best = "absent"
            for c in found:
                v = classify(c["state"], c["status"])
                if v in ("healthy", "running") or best == "absent":
                    best = v
            verdicts.append((tok, best))
        ok = all(v in ("healthy", "running") for _, v in verdicts)
        if not ok and not optional:
            failed += 1
        mark = "ok" if ok else ("optional" if optional else "FAIL")
        detail = ", ".join(f"{t}={v}" for t, v in verdicts)
        lines.append(f"  {mark:<10} {row['aws']:<28} {row['part']}  [{detail}]")
    return (1 if failed else 0), lines


def parse_ps_json(out: str) -> List[Dict[str, object]]:
    """podman prints ONE JSON array; docker prints one JSON object PER LINE (measured on
    the clean EC2 box, where the array-only parse read as DEAD). Accept both."""
    text = out.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip().startswith("{")]


def list_containers() ->Tuple[Optional[List[Dict[str, str]]], str]:
    rc, out = _run(engine_argv() + ["ps", "-a", "--format", "json"], timeout=240)
    if rc != 0:
        return None, out.strip()[-400:]
    try:
        raw = parse_ps_json(out)
    except ValueError as exc:
        return None, f"engine answered non-JSON: {exc}"
    rows = []
    for r in raw:
        names = r.get("Names") or r.get("Name") or []
        name = names[0] if isinstance(names, list) and names else str(names)
        rows.append({"name": name.lstrip("/"), "state": str(r.get("State", "")),
                     "status": str(r.get("Status", ""))})
    if not rows:
        return None, "engine listed ZERO containers -- refusing to read that as 'all absent'"
    return rows, ""


def cmd_catalog() -> int:
    try:
        shipped = set(sovereign_services())
    except Exception as exc:  # noqa: BLE001
        _out(f"DEAD: cannot read {SOV_COMPOSE}: {exc}")
        return 2
    p = ports()
    _out(f"Aither World Services {VERSION} -- catalog (customer bundle: {SOV_COMPOSE.name})")
    _out(f"  {'Amazon':<28} {'Aither World Services':<36} {'port':<6} {'in bundle':<10} bricks")
    for row in CATALOG:
        inb = [s for s in row["sovereign"] if s in shipped]  # type: ignore[union-attr]
        port = p.get(str(row["svc"])) if row["svc"] else None
        _out(f"  {row['aws']:<28} {row['part']:<36} {str(port or '-'):<6} "
             f"{('yes' if inb else 'NO'):<10} {', '.join(row['bricks']) or '-'}")  # type: ignore[arg-type]
    missing = [r["aws"] for r in CATALOG if not any(s in shipped for s in r["sovereign"])]  # type: ignore[union-attr]
    _out(f"\n  not in the customer bundle yet: {', '.join(missing) or 'none'}")
    return 0


def cmd_status(target: str) -> int:
    rows, err = list_containers()
    if rows is None:
        _out(f"DEAD: could not list containers via {' '.join(engine_argv())}: {err}")
        return 2
    code, lines = judge(rows, target)
    _out(f"Aither World Services {VERSION} -- status, target {target} ({len(rows)} containers)")
    for ln in lines:
        _out(ln)
    _out("RESULT: " + ("every required row is up" if code == 0 else "a required row is down"))
    return code


ENV_FILE = SOV_COMPOSE.parent / ".env"
#: Override that swaps every Aitherium image for its public-slim `aws-*` twin
#: (no registry login; each image is scanned layer by layer before it is published).
PUBLIC_OVERRIDE = SOV_COMPOSE.parent / "docker-compose.sovereign.public.yml"
_REQUIRED_RX = re.compile(r"\$\{([A-Z0-9_]+):\?")


def required_vars(path: Path = SOV_COMPOSE) -> List[str]:
    """Every `${VAR:?...}` the compose refuses to start without (measured on a clean box:
    POSTGRES_PASSWORD missing killed `up --apply` after a green preflight)."""
    return sorted(set(_REQUIRED_RX.findall(path.read_text(encoding="utf-8"))))


def read_env_file(path: Path = ENV_FILE) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def resolved(name: str, env_file: Dict[str, str]) -> str:
    return os.environ.get(name) or env_file.get(name, "")


def cmd_init(domain: str, path: Path = ENV_FILE, public: bool = False) -> int:
    """Write the bundle's .env: the domain plus a fresh random value for every missing
    secret. Never overwrites a value, never prints one, mode 600."""
    import secrets as _secrets
    existing = read_env_file(path)
    added: List[str] = []
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else [
        "# Aither World Services -- customer bundle secrets. Generated by `aws init`.",
        "# Keep this file private (mode 600). Values are never printed."]
    if public and not resolved("AWS_PUBLIC", existing):
        lines.append("AWS_PUBLIC=1")
        added.append("AWS_PUBLIC")
    for name in required_vars():
        if resolved(name, existing):
            continue
        if name == "AITHER_SOVEREIGN_DOMAIN":
            if not domain:
                _out("FAIL  AITHER_SOVEREIGN_DOMAIN is unset -- pass --domain <your domain>")
                return 1
            value = domain
        else:
            value = _secrets.token_urlsafe(32)
        lines.append(f"{name}={value}")
        added.append(name)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError as exc:  # Windows ACLs: reported, never silent
        _out(f"note: could not chmod 600 {path}: {exc}")
    _out(f"wrote {path} -- set: {', '.join(added) or 'nothing (all present)'}")
    return 0


def preflight() -> List[Tuple[str, bool, str]]:
    steps: List[Tuple[str, bool, str]] = []
    steps.append(("compose", SOV_COMPOSE.is_file(), str(SOV_COMPOSE)))
    env = read_env_file()
    dom = resolved("AITHER_SOVEREIGN_DOMAIN", env)
    steps.append(("domain", bool(dom), dom or (
        "set AITHER_SOVEREIGN_DOMAIN=<your domain> (the OIDC issuer derives from it)")))
    if SOV_COMPOSE.is_file():
        missing = [v for v in required_vars() if not resolved(v, env)]
        steps.append(("secrets", not missing, "every required variable is set" if not missing
                      else f"missing {', '.join(missing)} -- run `aws init --domain <d>`"))
    checker = REPO / "AitherOS/dev/tools/check_sovereign_replica.py"
    if not checker.is_file():
        steps.append(("sovereign", True, "skipped: repo-only check (standalone install)"))
    else:
        rc, out = _run([sys.executable, str(checker)], timeout=600)
        why = out.strip().splitlines()[-1][:160] if (rc != 0 and out.strip()) else ""
        steps.append(("sovereign", rc == 0,
                      f"check_sovereign_replica.py rc={rc}" + (f" -- {why}" if why else "")))
    rc, out = _run(engine_argv() + ["compose", "version"], timeout=120)
    steps.append(("engine", rc == 0, " ".join(engine_argv()) + " compose"
                  + ("" if rc == 0 else f" -- {out.strip()[-160:]}")))
    return steps


def profiles() -> List[str]:
    """Compose profiles `up --apply` enables. `monitoring` by default (a catalog row);
    `compute` (Comet: engine socket + spend) only when AWS_PROFILES names it."""
    raw = os.environ.get("AWS_PROFILES", "monitoring")
    return [p.strip() for p in raw.split(",") if p.strip()]


def cmd_up(apply: bool) -> int:
    steps = preflight()
    for name, ok, detail in steps:
        _out(f"  [{name}] {'ok  ' if ok else 'FAIL'}  {detail}")
    if not all(ok for _, ok, _ in steps):
        _out("PREFLIGHT: not ready -- nothing was started")
        return 1
    if not apply:
        _out("PREFLIGHT: ready. Re-run with --apply to bring the customer bundle up.")
        return 0
    argv = engine_argv() + ["compose", "-f", str(SOV_COMPOSE)]
    if resolved("AWS_PUBLIC", read_env_file()) == "1":
        argv += ["-f", str(PUBLIC_OVERRIDE)]
    if ENV_FILE.is_file():
        argv += ["--env-file", str(ENV_FILE)]
    # CloudWatch is a catalog row; its services sit behind the opt-in `monitoring`
    # profile, so a bare `up` shipped a cloud with no metrics (measured on EC2).
    for prof in profiles():
        argv += ["--profile", prof]
    # Never build on a customer box: the build contexts are not shipped, and a pull
    # miss must fail loudly instead of trying to compile from a missing tree.
    # --wait: judge the stack once healthchecks settle, not the instant `up` returns
    # (Grafana installs plugins at boot and read `starting` for minutes).
    argv += ["up", "-d", "--no-build", "--wait", "--wait-timeout", "900"]
    rc, out = _run(argv, timeout=2400)
    _out(out.strip()[-2000:])
    if rc == 0:
        return cmd_status("sovereign")
    # `compose --wait` fails the moment one container is unhealthy, including a service
    # that restarts ONCE on first boot while a peer (Secrets) is still coming up --
    # measured on a clean box: directory restarted once, then went healthy 60 s later.
    # The verdict is the stack's settled state, so re-judge for up to 10 minutes.
    _out(f"compose up exited {rc}; re-checking the stack as it settles (up to 10 min)")
    import time
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        rows, _err = list_containers()
        if rows is not None and judge(rows, "sovereign")[0] == 0:
            return cmd_status("sovereign")
        time.sleep(20)
    return cmd_status("sovereign")


def self_test() -> int:
    def c(name: str, status: str = "Up 1 hour (healthy)", state: str = "running") -> Dict[str, str]:
        return {"name": name, "state": state, "status": status}

    full = [c(f"aitheros-{t}") for r in CATALOG for t in r["fleet"]]  # type: ignore[union-attr]
    cases = [
        ("every fleet row up -> 0", judge(full, "fleet")[0], 0),
        ("a required row absent -> 1",
         judge([x for x in full if "directory" not in x["name"]], "fleet")[0], 1),
        ("a required row unhealthy -> 1",
         judge([x if "strata" not in x["name"] else c(x["name"], "Up (unhealthy)")
                for x in full], "fleet")[0], 1),
        ("optional row absent stays 0",
         judge([x for x in full if "comet" not in x["name"]], "fleet")[0], 0),
        ("HA suffix -a counts", judge([c("aitheros-security-core-a")] +
                                      [x for x in full if "security-core" not in x["name"]],
                                      "fleet")[0], 0),
        ("a prefix lookalike is not a match",
         judge([c("aitheros-directory-backup-old-x")] +
               [x for x in full if "directory" not in x["name"]], "fleet")[0], 1),
        ("classify exited", classify("exited", "Exited (0)") == "down", True),
    ]
    bad = [n for n, got, want in cases if got != want]
    for n, got, want in cases:
        _out(f"  {'ok  ' if got == want else 'FAIL'} {n} (got {got}, want {want})")
    return 1 if bad else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="aws", description="Aither World Services")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("catalog")
    st = sub.add_parser("status")
    st.add_argument("--target", choices=["fleet", "sovereign"], default="fleet")
    up = sub.add_parser("up")
    up.add_argument("--apply", action="store_true")
    ini = sub.add_parser("init", help="write the bundle .env (domain + random secrets)")
    ini.add_argument("--domain", default="")
    ini.add_argument("--public", action="store_true",
                     help="use the public aws-* images (no registry login)")
    a = ap.parse_args(argv)
    if a.version:
        _out(f"Aither World Services {VERSION}")
        return 0
    if a.self_test:
        return self_test()
    if a.cmd == "catalog":
        return cmd_catalog()
    if a.cmd == "status":
        return cmd_status(a.target)
    if a.cmd == "up":
        return cmd_up(a.apply)
    if a.cmd == "init":
        return cmd_init(a.domain, public=a.public)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
