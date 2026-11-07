#!/bin/sh
# Aither World Services -- one-line install for Linux x86_64 (also WSL2 and Intel macOS).
#
#   curl -fsSL https://raw.githubusercontent.com/Aitherium/aws/main/install.sh | sh -s -- --domain you.example
#
# What it does, in order, and nothing else:
#   1. checks for a container engine with compose (podman or docker); stops if there is none
#   2. downloads this repository into ~/.aither-world-services (or $AWS_HOME)
#   3. `aws init --public --domain D`   -> writes compose/.env with fresh random secrets
#   4. `aws up --apply`                 -> pulls the public aws-* images and starts them
# It never asks for a registry login and never installs packages with sudo.
set -eu

DOMAIN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --domain=*) DOMAIN="${1#*=}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$DOMAIN" ] || { echo "usage: install.sh --domain you.example" >&2; exit 2; }

ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64) ;;
  *) echo "warning: images are x86_64 only; on $ARCH they run under emulation (slow)." >&2 ;;
esac

if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
  ENGINE=podman
elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ENGINE=docker
else
  echo "need podman or docker with compose. Install one, then re-run:" >&2
  echo "  Ubuntu/Debian: sudo apt-get install -y docker.io docker-compose-v2" >&2
  echo "  macOS:         install Docker Desktop or 'brew install podman'" >&2
  exit 1
fi
command -v python3 >/dev/null 2>&1 || { echo "need python3" >&2; exit 1; }
python3 -c "import yaml" 2>/dev/null || { echo "need PyYAML: pip install pyyaml (or apt-get install python3-yaml)" >&2; exit 1; }

AWS_HOME="${AWS_HOME:-$HOME/.aither-world-services}"
REF="${AWS_REF:-main}"
mkdir -p "$AWS_HOME"
curl -fsSL "https://github.com/Aitherium/aws/archive/refs/heads/$REF.tar.gz" | tar -xz -C "$AWS_HOME" --strip-components=1
cd "$AWS_HOME"

export AWS_ENGINE="$ENGINE"
python3 aws.py init --public --domain "$DOMAIN"
python3 aws.py up --apply
echo
echo "Aither World Services is up. Status any time:  python3 $AWS_HOME/aws.py status --target sovereign"
