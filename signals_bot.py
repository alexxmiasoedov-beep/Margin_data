#!/usr/bin/env python3
"""Экспериментальный бот: маржинальная таблица + сигналы аномалий.

Постит в отдельную группу (@Margin_data_exper_bot). Основной бот
(margin_data.py) не трогаем — он шлёт чистую таблицу.

Сводка по каждому токену из таблицы (B/R >= 3):
  - funding rate фьючерсов (сильно отрицательный = переполнены шорты)
  - изменение открытого интереса за ~4ч (набор позиций)
  - long/short ratio топ-трейдеров
  - почасовая ставка займа в годовых (высокая = пул выгребли), signed API
  - ПУЛ ПУСТ: ошибка -3045 у maxBorrowable = занять больше нечего 🔥

Сигналы-события (отдельными сообщениями ⚡, с кулдауном):
  - LONG: пул пуст + funding < 0 + цена пошла вверх за WINDOW_MIN — старт сквиза
  - SHORT: резкий прирост займов + цена вниз за WINDOW_MIN — старт дампа
История цены/займов копится в state_exp.json при каждом запуске крона.

Запуск: python3 signals_bot.py [--post]
.env: BINANCE_API_KEY, BINANCE_API_SECRET, TG_BOT_TOKEN_EXP, TG_CHAT_ID_EXP
"""

import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MARGIN_URL = "https://www.binance.com/bapi/margin/v1/public/margin/statistics/24h-borrow-and-repay"
STATE_FILE = Path(__file__).with_name("state_exp.json")
ENV_FILE = Path(__file__).with_name(".env")

MIN_RATIO = 3.0
EXCLUDE = {"USDT", "USDC", "FDUSD", "TUSD", "DAI"}
APR_ALERT = 20.0  # показывать ставку займа, если выше этого % годовых

# сигналы-события: сочетание параметров в момент времени, не висящее состояние
WINDOW_MIN = 30      # окно изменения цены/займов, минут
PRICE_TRIG = 2.0     # % движения цены за окно для триггера
BOR_TRIG_ABS = 20_000  # мин. прирост займов за окно, USDT
COOLDOWN_MIN = 120   # повторный сигнал по токену/направлению не чаще
HISTORY_KEEP = 20    # точек истории на токен (~100 минут при кроне 5м)


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


ENV = load_env()


def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def signed_get(path, params):
    params = dict(params, timestamp=int(time.time() * 1000), recvWindow=10000)
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(
        ENV["BINANCE_API_SECRET"].encode(), qs.encode(), hashlib.sha256
    ).hexdigest()
    url = f"https://api.binance.com{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": ENV["BINANCE_API_KEY"]})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def fmt_k(value):
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 100_000:
        return f"{value / 1000:.0f}K"
    if value >= 1_000:
        return f"{value / 1000:.1f}K"
    return f"{value:.0f}"


def load_state():
    if not STATE_FILE.exists():
        return {"ratios": {}, "history": {}, "alerts": {}}
    state = json.loads(STATE_FILE.read_text())
    if "ratios" not in state:  # миграция со старого формата {asset: ratio}
        state = {"ratios": state, "history": {}, "alerts": {}}
    return state


def margin_rows(prev_ratios):
    payload = fetch_json(MARGIN_URL)
    if payload.get("code") != "000000":
        raise RuntimeError(f"Binance error: {payload}")
    data = payload["data"]
    rows = []
    for c in data["coins"]:
        asset = c["asset"]
        bor = float(c["totalBorrowInUsdt"])
        rep = float(c["totalRepayInUsdt"])
        if asset in EXCLUDE or rep <= 0:
            continue
        ratio = bor / rep
        if ratio < MIN_RATIO:
            continue
        chng = 0.0 if asset not in prev_ratios else ratio - prev_ratios[asset]
        rows.append((asset, bor, rep, ratio, chng, asset not in prev_ratios))
    rows.sort(key=lambda r: -r[1])
    return rows, data["calculationTime"] / 1000


def futures_signals(asset):
    """funding / изменение OI за ~4ч / L/S топ-трейдеров. None — нет фьючерса."""
    symbol = None
    funding = None
    for candidate in (f"{asset}USDT", f"1000{asset}USDT"):
        try:
            prem = fetch_json(
                f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={candidate}"
            )
            funding = float(prem["lastFundingRate"]) * 100
            symbol = candidate
            break
        except Exception:  # noqa: BLE001
            continue
    if symbol is None:
        return None
    oi_chg = ls_ratio = None
    try:
        hist = fetch_json(
            "https://fapi.binance.com/futures/data/openInterestHist"
            f"?symbol={symbol}&period=1h&limit=5"
        )
        if len(hist) >= 2 and float(hist[0]["sumOpenInterestValue"]) > 0:
            oi_chg = (
                float(hist[-1]["sumOpenInterestValue"])
                / float(hist[0]["sumOpenInterestValue"])
                - 1
            ) * 100
    except Exception:  # noqa: BLE001
        pass
    try:
        ls = fetch_json(
            "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
            f"?symbol={symbol}&period=1h&limit=1"
        )
        if ls:
            ls_ratio = float(ls[0]["longShortRatio"])
    except Exception:  # noqa: BLE001
        pass
    return {"funding": funding, "oi_chg": oi_chg, "ls": ls_ratio}


def borrow_rates(assets):
    """Почасовые ставки займа -> % годовых, батчами по 20 активов."""
    rates = {}
    for i in range(0, len(assets), 20):
        batch = assets[i : i + 20]
        try:
            data = signed_get(
                "/sapi/v1/margin/next-hourly-interest-rate",
                {"assets": ",".join(batch), "isIsolated": "FALSE"},
            )
            for item in data:
                rates[item["asset"]] = (
                    float(item["nextHourlyInterestRate"]) * 24 * 365 * 100
                )
        except Exception:  # noqa: BLE001
            pass
    return rates


def pool_empty(asset):
    """True — пул займов исчерпан (ошибка -3045 у maxBorrowable)."""
    try:
        signed_get("/sapi/v1/margin/maxBorrowable", {"asset": asset})
        return False
    except urllib.error.HTTPError as err:
        try:
            return json.loads(err.read().decode()).get("code") == -3045
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False


def spot_tickers():
    """{symbol: (изменение за 24ч %, последняя цена)} по всем спот-парам."""
    try:
        tickers = fetch_json("https://api.binance.com/api/v3/ticker/24hr")
        return {
            t["symbol"]: (float(t["priceChangePercent"]), float(t["lastPrice"]))
            for t in tickers
        }
    except Exception:  # noqa: BLE001
        return {}


def window_delta(points, now, minutes):
    """(Δцены %, Δзаймов USDT) относительно точки ~minutes назад, иначе None."""
    target = now - minutes * 60
    past = [p for p in points if p[0] <= target + 150]  # допуск полшага крона
    if not past:
        return None
    ts, price, bor = past[-1]
    if now - ts < (minutes - 10) * 60:  # истории ещё мало
        return None
    _, cur_price, cur_bor = points[-1]
    if price <= 0:
        return None
    return ((cur_price / price - 1) * 100, cur_bor - bor)


def detect_signals(assets_data, state, now):
    """Сигналы-события: возвращает список текстов алертов.

    LONG ⚡ — пул пуст, funding отрицательный и цена пошла ВВЕРХ за окно:
             зажатые шорты начинают гореть — старт сквиза.
    SHORT ⚡ — за окно резко приросли займы и цена пошла ВНИЗ:
             кто-то занимает и продаёт прямо сейчас — старт дампа.
    Кулдаун COOLDOWN_MIN на (токен, направление).
    """
    alerts = []
    for asset, info in assets_data.items():
        points = state["history"].get(asset, [])
        if len(points) < 2:
            continue
        delta = window_delta(points, now, WINDOW_MIN)
        if delta is None:
            continue
        price_chg, bor_chg = delta

        fired = []
        if (
            info["empty"]
            and info["funding"] is not None
            and info["funding"] <= -0.05
            and price_chg >= PRICE_TRIG
        ):
            fired.append((
                "LONG",
                f"⚡ LONG сигнал: {asset}\n"
                f"цена +{price_chg:.1f}% за {WINDOW_MIN}м, пул займов пуст,\n"
                f"funding {info['funding']:+.2f}% — старт сквиза?",
            ))
        if bor_chg >= BOR_TRIG_ABS and price_chg <= -PRICE_TRIG:
            fired.append((
                "SHORT",
                f"⚡ SHORT сигнал: {asset}\n"
                f"займы +{fmt_k(bor_chg)} и цена {price_chg:.1f}% за {WINDOW_MIN}м\n"
                f"— занимают и продают, старт дампа?",
            ))

        for direction, text in fired:
            key = f"{asset}:{direction}"
            last = state["alerts"].get(key, 0)
            if now - last >= COOLDOWN_MIN * 60:
                state["alerts"][key] = now
                alerts.append(text)
    return alerts


def send_telegram(text):
    token = ENV.get("TG_BOT_TOKEN_EXP")
    chat_id = ENV.get("TG_CHAT_ID_EXP")
    if not token or not chat_id:
        raise RuntimeError("TG_BOT_TOKEN_EXP/TG_CHAT_ID_EXP not set in .env")
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": f"```\n{text}\n```",
        "parse_mode": "MarkdownV2",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


def main():
    now = time.time()
    state = load_state()
    rows, calc_time = margin_rows(state["ratios"])
    assets = [r[0] for r in rows]
    rates = borrow_rates(assets)

    ts = time.strftime("%d.%m %H:%M UTC", time.gmtime(calc_time))
    lines = [f"Margin 24h — {ts}", ""]
    lines.append(f"{'SYM':7}{'BOR':>6}{'REP':>7} {'B/R':>4} {'CHNG':>5}")
    for asset, bor, rep, ratio, chng, is_new in rows:
        mark = "🆕" if is_new else ""
        lines.append(
            f"{asset:7}{fmt_k(bor):>6}{fmt_k(rep):>7} {ratio:>4.1f} {chng:>5.2f}{mark}"
        )

    tickers = spot_tickers()
    empty_map = {asset: pool_empty(asset) for asset in assets}

    # обновляем историю (цена + займы) и собираем данные для детектора
    assets_data = {}
    bor_map = {r[0]: r[1] for r in rows}
    fut_lines = []
    for asset in assets:
        sig = futures_signals(asset)
        tick = tickers.get(f"{asset}USDT") or tickers.get(f"1000{asset}USDT")
        chg24, last_price = tick if tick else (None, None)
        funding = sig["funding"] if sig else None

        if last_price is not None:
            points = state["history"].setdefault(asset, [])
            points.append([now, last_price, bor_map[asset]])
            del points[:-HISTORY_KEEP]
        assets_data[asset] = {"empty": empty_map[asset], "funding": funding}

        if sig is None and chg24 is None:
            continue
        fund_s = f"{funding:+6.2f}" if funding is not None else f"{'-':>6}"
        oi = f"{sig['oi_chg']:+.0f}%" if sig and sig["oi_chg"] is not None else "-"
        ls = f"{sig['ls']:.1f}" if sig and sig["ls"] is not None else "-"
        chg_s = f"{chg24:+.0f}%" if chg24 is not None else "-"
        fut_lines.append(f"{asset:7}{fund_s} {oi:>4} {ls:>3} {chg_s:>4}")
    if fut_lines:
        lines += ["", f"{'FUT':7}{'FUND':>6} {'OI4H':>4} {'LS':>3} {'P24':>4}"] + fut_lines

    # чистим историю токенов, выпавших из фильтра
    for stale in set(state["history"]) - set(assets):
        del state["history"][stale]

    # блок займов: ставка в годовых + состояние пула
    loan_lines = []
    for asset in assets:
        apr = rates.get(asset)
        empty = empty_map[asset]
        if (apr is None or apr < APR_ALERT) and not empty:
            continue
        apr_s = f"{apr:.0f}%" if apr is not None else "-"
        loan_lines.append(f"{asset:7}{apr_s:>5}  {'ПУЛ ПУСТ 🔥' if empty else ''}".rstrip())
    if loan_lines:
        lines += ["", f"{'LOAN':7}{'APR':>5}"] + loan_lines

    # сигналы-события: срабатывают в момент сочетания параметров
    alerts = detect_signals(assets_data, state, now)

    state["ratios"] = {r[0]: r[3] for r in rows}
    STATE_FILE.write_text(json.dumps(state))

    text = "\n".join(lines)
    print(text)
    for alert in alerts:
        print("\n" + alert)

    if "--post" in sys.argv:
        send_telegram(text)
        for alert in alerts:
            send_telegram(alert)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
