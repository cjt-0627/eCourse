#!/bin/sh
set -eu

if [ "${ALLOW_RESTORE:-}" != "yes" ]; then
  echo 'Refusing restore: set ALLOW_RESTORE=yes explicitly.' >&2
  exit 2
fi
if [ -z "${RESTORE_FILE:-}" ]; then
  echo 'RESTORE_FILE must name a dump below /backups.' >&2
  exit 2
fi

case "$RESTORE_FILE" in
  /backups/*.dump) ;;
  *) echo 'RESTORE_FILE must be /backups/<name>.dump.' >&2; exit 2 ;;
esac

test -f "$RESTORE_FILE"
pg_restore --list "$RESTORE_FILE" >/dev/null

# The caller must stop Web and Worker first. Recreate only the application
# schema; the PostgreSQL role and database remain intact.
psql --host=postgres --username=ecourse --dbname=ecourse \
  --set=ON_ERROR_STOP=1 \
  --command='DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
pg_restore \
  --host=postgres \
  --username=ecourse \
  --dbname=ecourse \
  --no-owner \
  --no-acl \
  --exit-on-error \
  "$RESTORE_FILE"

printf 'Restored %s\n' "$RESTORE_FILE"
