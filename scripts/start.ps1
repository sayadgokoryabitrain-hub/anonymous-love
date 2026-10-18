if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example. Set BOT_TOKEN before the bot can connect."
}
docker compose up -d --build
