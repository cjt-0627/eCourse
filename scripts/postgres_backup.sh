#!/bin/sh
set -eu

umask 077
backup_dir=/backups
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
temporary="$backup_dir/.ecourse-$timestamp.dump.tmp"
destination="$backup_dir/ecourse-$timestamp.dump"

mkdir -p "$backup_dir"
test ! -e "$destination"

cleanup() {
  rm -f "$temporary"
}
trap cleanup EXIT HUP INT TERM

pg_dump \
  --host=postgres \
  --username=ecourse \
  --dbname=ecourse \
  --format=custom \
  --no-owner \
  --no-acl \
  --file="$temporary"

pg_restore --list "$temporary" >/dev/null
mv "$temporary" "$destination"
trap - EXIT HUP INT TERM
printf '%s\n' "$destination"
