# Развёртывание на VPS

Подойдёт небольшой VPS с 1–2 ГБ RAM, Ubuntu 24.04, Docker и открытыми портами 22, 80 и 443. PostgreSQL, Redis и бот должны работать постоянно. Засыпающий бесплатный хостинг для этого набора не подходит.

1. Создайте пользователя без входа по паролю root и скопируйте проект.
2. `cp .env.example .env`.
3. Задайте свои значения:
   - `ENVIRONMENT=production`
   - длинные `ADMIN_JWT_SECRET` и `IDENTITY_PEPPER` (не короче 24 и 16 символов)
   - пароль PostgreSQL не `love` и тот же пароль в `DATABASE_URL` и `POSTGRES_PASSWORD`
   - `ADMIN_BOOTSTRAP_PASSWORD` длиннее 10 символов и не из списка слабых значений
   - `PAYMENT_MODE=disabled`, пока Stars не проверены, затем `stars`
   - `USE_WEBHOOK=true`, `WEBHOOK_BASE_URL` и `WEBHOOK_SECRET`
   - `MINIAPP_URL=https://домен/mini/` только после TLS
4. Поставьте Caddy или другой reverse proxy с автоматическим TLS перед контейнером `admin` (порт 8080). Проксируйте `/`, `/api`, `/health`, `/telegram`, `/mini`.
5. `docker compose up -d --build`.
6. Проверьте `https://домен/health`.
7. Войдите в панель и смените работу так, чтобы bootstrap-пароль больше нигде не использовался. Создайте отдельных moderator и support.
8. Убедитесь, что `docker compose logs bot` не содержит токен.

Бот в режиме webhook не вызывает `getUpdates`. Обновления принимает API на `POST /telegram/webhook`.
