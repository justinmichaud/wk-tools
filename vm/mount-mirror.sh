#!/bin/sh
# vm/mount-mirror.sh <tag> <mount point>, as root: the mirror's own virtiofs tag, mounted afresh (org.wk.mirror, wk sync).
set -euo pipefail
tag=${1:?usage: mount-mirror.sh <tag> <mount point>} at=${2:?usage: mount-mirror.sh <tag> <mount point>}
if mount | grep -qF " on $at ("; then umount "$at"; fi
mkdir -p "$at" && exec mount_virtiofs "$tag" "$at"
