#!/bin/sh
# Runs the bridge as the unprivileged user "aldes".
# Started as root (the default): fix the ownership of the volumes first, so that
# data written by older root-only images (cert, key, logs) stays readable, then drop
# privileges. If that is not possible (e.g. a network share refusing chown), keep
# running as root with a warning rather than failing. Started with `user:` set in
# docker-compose: run as that user directly.
set -e

if [ "$(id -u)" = "0" ]; then
    cert_dir="${CERT_DIR:-/data}"
    log_dir="$(dirname "${LOG_FILE:-/logs/mitm.log}")"
    for d in "$cert_dir" "$log_dir"; do
        mkdir -p "$d"
        chown -R aldes:aldes "$d" 2>/dev/null || true
    done
    if setpriv --reuid=aldes --regid=aldes --init-groups sh -c \
        'test -w "$1" && { [ ! -e "$1/fake_azure.key" ] || test -r "$1/fake_azure.key"; }' _ "$cert_dir"
    then
        exec setpriv --reuid=aldes --regid=aldes --init-groups "$@"
    fi
    echo "WARNING: $cert_dir is not usable by user 'aldes' (chown refused?): running as root" >&2
fi
exec "$@"
