# ANONYMOUS LOVE

Анонимный Telegram-бот знакомств: парень ищет девушку, девушка ищет парня. Минимальный возраст — 18 лет. LoveCoins — внутренние бонусы, это не деньги и их нельзя вывести.

Бот работает без Mini App. Веб-панель и Mini App — дополнительные интерфейсы к тому же API.

## Что запускается локально

- Бот на aiogram 3 (`python -m bot.main`).
- API и админ-эндпоинты (`python -m api.main`).
- PostgreSQL и Redis через Docker Compose.
- Админ-панель React: `admin/`.
- Необязательный Mini App: `miniapp/`.

Без Redis бот и API остаются работоспособными: лимиты и состояния диалога хранятся в памяти процесса. Для нескольких процессов Redis нужен.

## Быстрый старт без Docker

Нужны Python 3.12 и Node.js 22, если собираете панели.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

В `.env` для проверки без PostgreSQL можно временно поставить:

```
DATABASE_URL=sqlite+aiosqlite:///./local.sqlite
REDIS_URL=
BOT_TOKEN=
ADMIN_BOOTSTRAP_USERNAME=admin
ADMIN_BOOTSTRAP_PASSWORD=local-admin-change-me
PAYMENT_MODE=disabled
```

```powershell
python -m api.main
```

Панель:

```powershell
cd admin
npm install
npm run dev
```

Откройте http://127.0.0.1:5173 и войдите под bootstrap-администратором. Токен бота для панели не нужен. Для самого бота получите токен у @BotFather и запустите `python -m bot.main` в отдельном окне.

Тесты не ходят в Telegram и не проводят платежи:

```powershell
python -m pytest
```

## Docker Compose

```powershell
Copy-Item .env.example .env
.\scripts\start.ps1
```

Сервисы: PostgreSQL, Redis, API на порту 8000, бот, админ-панель с Mini App на порту 8080. Остановка: `.\scripts\stop.ps1`.

Пароль `love` в compose — только для локальной машины. В production `validate_runtime` откажется стартовать с таким паролем, слабым JWT, тестовыми платежами и пустым секретом webhook.

Первый администратор создаётся из `ADMIN_BOOTSTRAP_USERNAME` и `ADMIN_BOOTSTRAP_PASSWORD`, если таблица администраторов пуста. Пароль не короче 10 символов. Дополнительных сотрудников создаёт superadmin в панели или так:

```powershell
python -m scripts.create_admin --username moderator1 --password "long-password" --role moderator
```

## Документы

- [BotFather](docs/botfather.md)
- [Локальный запуск](docs/local.md)
- [VPS](docs/vps.md)
- [Платежи](docs/payments.md)
- [Модерация](docs/moderation.md)
- [Резервные копии](docs/backup.md)
- [Обновление](docs/update.md)
- [Восстановление](docs/recovery.md)
- [Ограничения](docs/limitations.md)
- [Архитектура](docs/architecture.md)

## Что бесплатно, а что может стоить денег

Бесплатно при самостоятельной установке: Python, aiogram, PostgreSQL, Redis, FastAPI, React, Telegram Bot API, Telegram Stars как платёжный канал Telegram (комиссию удерживает Telegram, отдельный эквайер не нужен).

Может потребовать оплаты: VPS, домен и TLS-сертификат для webhook и Mini App, внешний провайдер проверки возраста, если вы его подключите. Бесплатные хостинги с засыпающими процессами не подходят для постоянно работающего бота, PostgreSQL и фоновой очистки.
