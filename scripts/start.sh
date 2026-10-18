#!/bin/sh
set -eu
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Set BOT_TOKEN before the bot can connect."
fi
docker compose up -d --build
