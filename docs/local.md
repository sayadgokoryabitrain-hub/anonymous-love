# Локальный запуск

## Вариант с Docker

1. Установите Docker Desktop.
2. `Copy-Item .env.example .env` и впишите `BOT_TOKEN`, если нужен бот.
3. `.\scripts\start.ps1`.
4. API: http://127.0.0.1:8000/health
5. Панель: http://127.0.0.1:8080
6. Миграции на пустой базе применятся через `create_all` при старте API. Повторный запуск Alembic: `alembic upgrade head`.

## Вариант без Docker

1. Python 3.12, `pip install -r requirements.txt`.
2. Либо поднимите только PostgreSQL и Redis, либо для разработки поставьте `DATABASE_URL=sqlite+aiosqlite:///./local.sqlite` и пустой `REDIS_URL`.
3. `python -m api.main` — панель и health.
4. `python -m bot.main` — long polling. Не запускайте polling и webhook одновременно.
5. Админка в разработке: `cd admin; npm install; npm run dev`. Запросы `/api` проксируются на порт 8000.
6. Mini App в разработке: `cd miniapp; npm install; npm run dev` на порту 5174. Вне Telegram экран честно сообщает, что сессия выдаётся только по `initData`.

Тесты: `python -m pytest`. Они используют SQLite в памяти и не вызывают Telegram.
