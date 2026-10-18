New-Item -ItemType Directory -Force -Path backups | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
docker compose exec -T postgres pg_dump -U love anonymous_love > "backups/anonymous-love-$stamp.sql"
Write-Host "Wrote backups/anonymous-love-$stamp.sql"
