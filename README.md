# Binance Margin Data

Воспроизведение данных канала [t.me/cryptocode_margin_data](https://t.me/s/cryptocode_margin_data): мониторинг маржинальных займов на Binance.

## Откуда данные

Публичный (без API-ключей и подписи) эндпоинт Binance, который питает страницу «Margin Data» на сайте/в приложении:

```
GET https://www.binance.com/bapi/margin/v1/public/margin/statistics/24h-borrow-and-repay
```

Ответ — по каждому активу cross-margin (~470 монет), скользящее окно 24 часа:

| Поле | Значение |
|---|---|
| `totalBorrow` | взято займов за 24ч, в монетах |
| `totalRepay` | погашено займов за 24ч, в монетах |
| `totalBorrowInUsdt` | то же в USDT |
| `totalRepayInUsdt` | то же в USDT |
| `calculationTime` | момент расчёта (обновляется примерно раз в минуту) |

## Формулы канала

- **BOR** = `totalBorrowInUsdt`
- **REP** = `totalRepayInUsdt`
- **B/R** = BOR / REP — в таблицу попадают только токены с **B/R ≥ 3**
- **CHNG** = изменение B/R относительно прошлого замера
- **🆕** = токен впервые прошёл фильтр

Идея сигнала: если займов берут в разы больше, чем отдают, — кто-то агрессивно
набирает маржинальную позицию по токену (лонг с плечом при займе стейблов
или шорт при займе самого токена).

Проверено: цифры из постов канала совпадают с эндпоинтом до последнего знака.

## Запуск

```bash
python3 margin_data.py
```

Без зависимостей (только стандартная библиотека). Состояние для расчёта CHNG/🆕
хранится в `state.json`.

## Отправка в Telegram

Создайте `.env` рядом со скриптом (не коммитится):

```
TG_BOT_TOKEN=<токен бота от @BotFather>
TG_CHAT_ID=<id чата/группы/канала>
```

Запуск с постингом:

```bash
python3 margin_data.py --post
```

Периодический мониторинг по cron, каждые 5 минут:

```
*/5 * * * * cd ~/projects/Binance-margin-data && /usr/bin/python3 margin_data.py --post >> cron.log 2>&1
```
