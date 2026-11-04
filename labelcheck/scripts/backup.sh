#!/usr/bin/env bash
# Nightly backup for the docker-compose stack: PostgreSQL dump + uploaded files.
# Cron example (02:30 every night):  30 2 * * * /opt/labelcheck/scripts/backup.sh /backups
# Best practice: copy the backup folder off the server (e.g. S3 with versioning) and test a restore monthly.
# With LABELCHECK_STORAGE=s3, files live in the bucket: use S3 versioning instead of the file archive.
set -euo pipefail
OUT="${1:?usage: backup.sh <backup-dir>}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
cd "$(dirname "$0")/.."
mkdir -p "$OUT"
docker compose exec -T db pg_dump -U labelcheck -d labelcheck --format=custom > "$OUT/db-$STAMP.dump"
# Local file storage only: with LABELCHECK_STORAGE=s3 there is nothing here (tar would refuse an empty archive).
if docker compose exec -T web sh -c 'cd /data && ls -d uploads checked images >/dev/null 2>&1'; then
  docker compose exec -T web sh -c 'cd /data && tar -czf - $(ls -d uploads checked images 2>/dev/null)' > "$OUT/files-$STAMP.tar.gz"
else
  echo "No local upload folders (S3 storage?): file archive skipped; rely on S3 versioning."
fi
find "$OUT" -name 'db-*.dump' -mtime +"$KEEP_DAYS" -delete
find "$OUT" -name 'files-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "Backup done: $OUT/db-$STAMP.dump and files-$STAMP.tar.gz"
# Restore:
#   docker compose exec -T db pg_restore -U labelcheck -d labelcheck --clean < db-<stamp>.dump
#   docker compose exec -T web tar -C /data -xzf - < files-<stamp>.tar.gz
