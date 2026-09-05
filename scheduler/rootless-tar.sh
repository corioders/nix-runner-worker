#!/bin/sh

case "${1:-}" in
  [A-Za-z]*)
    operation=$1
    shift
    exec /usr/bin/tar "$operation" --no-same-owner "$@"
    ;;
esac

exec /usr/bin/tar --no-same-owner "$@"
