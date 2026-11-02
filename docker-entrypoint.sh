#!/bin/sh
# Make the data directory writable by the app user, then drop privileges.
#
# The image declares a volume at /data. A *named* volume inherits ownership from
# the image, but a **bind mount** (which is what Dokploy and most compose
# deployments actually provide) is created by the host as root:root and ignores
# whatever the Dockerfile chowned. The app then runs as uid 1000, cannot create
# a file in it, and SQLite reports the famously unhelpful "unable to open
# database file".
#
# So: start as root, fix the one directory, and hand off to the unprivileged
# user with exec so the app still receives signals directly (no shell in the
# middle swallowing SIGTERM).
set -e

DATA_DIR=$(dirname "${DATABASE_PATH:-/data/simplixity.db}")

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    # Only touch ownership when it is actually wrong: a large restored volume
    # should not be walked on every boot.
    if [ "$(stat -c '%u' "$DATA_DIR")" != "$(id -u simplixity)" ]; then
        echo "entrypoint: taking ownership of $DATA_DIR for the simplixity user"
        chown -R simplixity:simplixity "$DATA_DIR"
    fi
    exec setpriv --reuid=simplixity --regid=simplixity --init-groups "$@"
fi

# Already unprivileged: the platform pinned a user. Nothing to fix from here;
# if the directory is not writable the app says so in plain language at startup.
mkdir -p "$DATA_DIR" 2>/dev/null || true
exec "$@"
