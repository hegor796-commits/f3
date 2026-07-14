# ИИ-агент мониторинга B2B-Center → Telegram

Агент периодически парсит список торговых процедур на
[b2b-center.ru](https://www.b2b-center.ru/market/), отбирает объявления
по ключевым словам, оценивает их релевантность через Claude API и
присылает пуш-уведомления в Telegram.

## Как это работает

```
B2B-Center (парсинг) → фильтр по ключевым словам → ИИ-оценка (Claude)
     → уведомление в Telegram (только новые и релевантные)
```

- **Без дублей**: все обработанные объявления запоминаются в `state.db` (SQLite).
- **ИИ-фильтр**: Claude сравнивает объявление с твоим «профилем интересов»
  из `config.yaml`, ставит оценку 0–10 и пишет краткое резюме прямо
  в уведомление. Формальные совпадения по словам (не по смыслу) отсекаются.
- **Деградация без ИИ**: если `ANTHROPIC_API_KEY` не задан, агент работает
  просто по ключевым словам.

## Установка

```bash
pip install -r requirements.txt
cp .env.example .env
```

### 1. Создай Telegram-бота

1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → получи токен.
2. Запиши токен в `.env` → `TELEGRAM_BOT_TOKEN`.
3. Напиши своему новому боту любое сообщение (например `/start`).
4. Узнай свой chat_id и запиши его в `.env` → `TELEGRAM_CHAT_ID`:

```bash
python -m b2b_agent get-chat-id
```

5. Проверь связку:

```bash
python -m b2b_agent test-telegram
```

### 2. (Опционально) Ключ Claude API

Получи ключ на [platform.claude.com](https://platform.claude.com) и запиши
в `.env` → `ANTHROPIC_API_KEY`.

### 3. Настрой config.yaml

- `keywords` — слова для первичного отбора;
- `exclude_keywords` — стоп-слова;
- `ai.interest_profile` — опиши своими словами, что тебе действительно
  интересно (это главный вход для ИИ-фильтра);
- `ai.min_score` — порог релевантности для уведомления;
- `schedule.interval_minutes` — как часто проверять.

## Запуск

```bash
# Один цикл проверки (удобно для cron)
python -m b2b_agent once

# Постоянный режим: проверка каждые N минут из конфига
python -m b2b_agent run
```

### Через cron (каждые 30 минут)

```cron
*/30 * * * * cd /path/to/f3 && /usr/bin/python3 -m b2b_agent once >> agent.log 2>&1
```

### Через systemd

```ini
# /etc/systemd/system/b2b-agent.service
[Unit]
Description=B2B-Center monitoring agent
After=network-online.target

[Service]
WorkingDirectory=/path/to/f3
ExecStart=/usr/bin/python3 -m b2b_agent run
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

## Пример уведомления

> 🔔 **Запрос предложений: поставка металлопроката (арматура А500С)**
> 🏢 ООО «СтройКомплект»
> ⏳ Срок: 21.07.2026 10:00
> ⭐ Релевантность: 9/10
> 💬 Закупка арматуры А500С 120 т с доставкой в Екатеринбург — точное
> попадание в профиль по металлопрокату.
> 🔑 металлопрокат
> [Открыть на B2B-Center](https://www.b2b-center.ru/market/view.html?id=...)

## Важные замечания

- Парсер работает по публичной странице списка торгов. Если B2B-Center
  изменит вёрстку, срабатывает запасной парсер по ссылкам
  `/market/view.html?id=...`; если и он ничего не найдёт — агент напишет
  предупреждение в лог, но не упадёт.
- Не ставь слишком маленький интервал проверок и не увеличивай `pages`
  без необходимости — уважай площадку (и её антибот-защиту).
- Проверь условия использования площадки: для коммерческого использования
  данных у B2B-Center есть официальное API по подписке.

## Структура проекта

```
b2b_agent/
  scraper.py    — парсинг списка торгов B2B-Center
  filters.py    — фильтр по ключевым словам
  ai_filter.py  — оценка релевантности через Claude API
  notifier.py   — отправка уведомлений в Telegram
  state.py      — SQLite-хранилище обработанных объявлений
  main.py       — CLI и основной цикл
config.yaml     — все настройки агента
.env            — секреты (токены), не коммитится
```
