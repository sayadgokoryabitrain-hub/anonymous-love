# Настройка бота в BotFather

1. В Telegram откройте @BotFather и отправьте `/newbot`.
2. Имя: `ANONYMOUS LOVE`. Username должен заканчиваться на `bot`.
3. Скопируйте токен в `BOT_TOKEN`. Username без `@` — в `BOT_USERNAME`.
4. `/setdescription` и `/setabouttext`: анонимные знакомства 18+, парень и девушка, без обещания полной секретности.
5. `/setcommands` можно не задавать вручную: при старте бот регистрирует команды для ru, hy и en.
6. Команды личного чата: `/start`, `/profile`, `/search`, `/stop`, `/verify`, `/balance`, `/referral`, `/settings`, `/help`, `/delete_account`.
7. Для Mini App: `/newapp` или Menu Button → URL вида `https://ваш-домен/mini/`. Этот же URL запишите в `MINIAPP_URL`. Пока переменная пустая, кнопка приложения в боте скрыта.
8. Telegram Stars включаются для бота в BotFather, раздел платежей. Отдельный provider token для цифровых товаров в Stars не используется: счёт выставляется в валюте `XTR`.
9. Для webhook: `/setdomain` не нужен самому боту. Укажите `USE_WEBHOOK=true`, `WEBHOOK_BASE_URL=https://ваш-домен` и длинный `WEBHOOK_SECRET`. API примет обновления только с заголовком `X-Telegram-Bot-Api-Secret-Token`.

Токен не коммитьте и не пишите в логи. При утечке отзовите его в BotFather командой `/revoke`.
