#!/bin/sh
set -eu
mkdir -p backups
stamp=$(date +%Y%m%d-%H%M%S)
docker compose exec -T postgres pg_dump -U love anonymous_love > "backups/anonymous-love-$stamp.sql"
echo "Wrote backups/anonymous-love-$stamp.sql"
