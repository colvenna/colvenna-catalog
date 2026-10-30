#!/usr/bin/env bash
# Installs the pinned kubectl, Helm and crane into the repository's .tools/ (git-ignored), each
# checked against its SHA-256 before it is kept; a tool already there at its pinned version is
# skipped. Scripts put .tools first on PATH. Supports Linux on arm64 and amd64.
set -euo pipefail

case "$(uname -m)" in
  aarch64 | arm64) arch=arm64 ;;
  x86_64) arch=amd64 ;;
  *)
    echo "install-tools.sh supports arm64 and amd64, not $(uname -m)" >&2
    exit 1
    ;;
esac
tools="$(git rev-parse --show-toplevel)/.tools"
mkdir -p "$tools"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# fetch <url> <sha256> <file>: downloads into the scratch directory, keeps it only if it matches.
fetch() {
  curl -fsSL "$1" -o "$work/$3"
  if ! printf '%s  %s\n' "$2" "$work/$3" | sha256sum --check --status; then
    echo "SHA-256 mismatch for $1" >&2
    return 1
  fi
}

kubectl_version=v1.36.5
if [[ "$("$tools/kubectl" version --client -o json 2>/dev/null | jq -r .clientVersion.gitVersion 2>/dev/null)" != "$kubectl_version" ]]; then
  url="https://dl.k8s.io/release/$kubectl_version/bin/linux/$arch/kubectl"
  fetch "$url" "$(curl -fsSL "$url.sha256")" kubectl
  install -m 0755 "$work/kubectl" "$tools/kubectl"
fi

helm_version=v3.19.0
if [[ "$("$tools/helm" version --template '{{.Version}}' 2>/dev/null)" != "$helm_version" ]]; then
  archive="helm-$helm_version-linux-$arch.tar.gz"
  fetch "https://get.helm.sh/$archive" "$(curl -fsSL "https://get.helm.sh/$archive.sha256sum" | awk '{print $1}')" "$archive"
  tar -xzf "$work/$archive" -C "$work" "linux-$arch/helm"
  install -m 0755 "$work/linux-$arch/helm" "$tools/helm"
fi

# crane copies images between registries by digest.
crane_version=v0.22.1
case "$arch" in
  arm64)
    crane_archive=go-containerregistry_Linux_arm64.tar.gz
    crane_sha256=898c0cff975f898a33e8c4580bdafb0e7c02c7faa33374e946762f97c4ab7110
    ;;
  amd64)
    crane_archive=go-containerregistry_Linux_x86_64.tar.gz
    crane_sha256=0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0
    ;;
esac
if [[ "$("$tools/crane" version 2>/dev/null)" != "${crane_version#v}" ]]; then
  fetch "https://github.com/google/go-containerregistry/releases/download/$crane_version/$crane_archive" \
    "$crane_sha256" "$crane_archive"
  tar -xzf "$work/$crane_archive" -C "$work" crane
  install -m 0755 "$work/crane" "$tools/crane"
fi

echo "kubectl $kubectl_version, Helm $helm_version and crane $crane_version are in $tools"
