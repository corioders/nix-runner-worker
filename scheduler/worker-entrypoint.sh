#!/bin/sh
set -eu

staging_directory=/mnt/corioders-writable
writable_paths="/home/runner /root /tmp /nix /etc/nix"
mkdir -p "$staging_directory"

mount --make-rprivate /
index=0
for path in $writable_paths; do
  index=$((index + 1))
  staging_path="$staging_directory/$index"
  mkdir -p "$path" "$staging_path"
  mount --bind "$path" "$staging_path"
done

mount --rbind / /
mount -o remount,bind,ro /

index=0
for path in $writable_paths; do
  index=$((index + 1))
  mount --bind "$staging_directory/$index" "$path"
done

exec setpriv \
  --bounding-set=-all,+dac_override \
  --inh-caps=+dac_override \
  --ambient-caps=+dac_override \
  --securebits=+noroot,+noroot_locked \
  --no-new-privs \
  "$@"
