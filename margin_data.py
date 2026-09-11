#!/usr/bin/env python3
"""Binance margin borrow/repay monitor.

Воспроизводит данные канала t.me/cryptocode_margin_data.

Источник — публичный (без API-ключей) эндпоинт Binance:
    GET https://www.binance.com/bapi/margin/v1/public/margin/statistics/24h-borrow-and-repay

Он возвращает по каждому активу cross-margin суммы займов (borrow) и
погашений (repay) за скользящие 24 часа — в монетах и в USDT.

Формулы канала:
    BOR  = totalBorrowInUsdt   (объём взятых займов за 24ч, USDT)
    REP  = totalRepayInUsdt    (объём погашенных займов за 24ч, USDT)
    B/R  = BOR / REP           (в таблицу попадают только токены с B/R >= 3)
    CHNG = изменение B/R по сравнению с прошлым замером
    🆕   = токен, которого не было в прошлом замере

Состояние между запусками хранится в state.json рядом со скриптом.

Запуск с флагом --post отправляет таблицу в Telegram: токен и чат берутся
из .env рядом со скриптом (TG_BOT_TOKEN, TG_CHAT_ID).
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

URL = "https://www.binance.com/bapi/margin/v1/public/margin/statistics/24h-borrow-and-repay"
STATE_FILE = Path(__file__).with_name("state.json")
ENV_FILE = Path(__file__).with_name(".env")

MIN_RATIO = 3.0        # фильтр канала: B/R >= 3 (при REP > 0)
EXCLUDE = {"USDT", "USDC", "FDUSD", "TUSD", "DAI"}  # стейблы не интересны


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch():
    payload = fetch_json(URL)
    if payload.get("code") != "000000":
        raise RuntimeError(f"Binance error: {payload}")
    return payload["data"]


def futures_signals(asset):
    """Сигналы с фьючерсов по токену: funding, динамика OI за ~4ч, L/S топов.

    Возвращает None, если у токена нет USDT-M фьючерса.
    """
    symbol = None
    funding = None
    # мемкоины на фьючерсах торгуются с префиксом 1000 (1000SHIBUSDT и т.п.)
    for candidate in (f"{asset}USDT", f"1000{asset}USDT"):
        try:
            prem = fetch_json(
                f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={candidate}"
            )
            funding = float(prem["lastFundingRate"]) * 100  # в %
            symbol = candidate
            break
        except Exception:  # noqa: BLE001 — нет фьючерса или временная ошибка
            continue
    if symbol is None:
        return None
    oi_chg = None
    try:
        hist = fetch_json(
            "https://fapi.binance.com/futures/data/openInterestHist"
            f"?symbol={symbol}&period=1h&limit=5"
        )
        if len(hist) >= 2:
            first = float(hist[0]["sumOpenInterestValue"])
            last = float(hist[-1]["sumOpenInterestValue"])
            if first > 0:
                oi_chg = (last / first - 1) * 100
    except Exception:  # noqa: BLE001
        pass
    ls_ratio = None
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


def fmt_k(value):
    """Компактный формат под ширину мобильного экрана: 804K, 29.6K, 298, 1.2M."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 100_000:
        return f"{value / 1000:.0f}K"
    if value >= 1_000:
        return f"{value / 1000:.1f}K"
    return f"{value:.0f}"


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def send_telegram(text):
    env = load_env()
    token = env.get("TG_BOT_TOKEN")
    chat_id = env.get("TG_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TG_BOT_TOKEN/TG_CHAT_ID not set in .env")
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
    data = fetch()
    coins = data["coins"]
    calc_time = data["calculationTime"] / 1000

    prev = {}
    if STATE_FILE.exists():
        prev = json.loads(STATE_FILE.read_text())

    rows = []
    for c in coins:
        asset = c["asset"]
        bor = float(c["totalBorrowInUsdt"])
        rep = float(c["totalRepayInUsdt"])
        if asset in EXCLUDE or rep <= 0:
            continue
        ratio = bor / rep
        if ratio < MIN_RATIO:
            continue
        prev_ratio = prev.get(asset)
        chng = 0.0 if prev_ratio is None else ratio - prev_ratio
        is_new = asset not in prev
        rows.append((asset, bor, rep, ratio, chng, is_new))

    rows.sort(key=lambda r: -r[1])

    ts = time.strftime("%d.%m %H:%M UTC", time.gmtime(calc_time))
    lines = [f"Margin 24h — {ts}", ""]
    lines.append(f"{'SYM':7}{'BOR':>6}{'REP':>7} {'B/R':>4} {'CHNG':>5}")
    for asset, bor, rep, ratio, chng, is_new in rows:
        mark = "🆕" if is_new else ""
        lines.append(
            f"{asset:7}{fmt_k(bor):>6}{fmt_k(rep):>7} {ratio:>4.1f} {chng:>5.2f}{mark}"
        )
    # блок фьючерсных сигналов по токенам из таблицы
    sig_lines = []
    for asset, _, _, _, _, _ in rows:
        sig = futures_signals(asset)
        if sig is None:
            continue
        parts = [f"{asset:7}f{sig['funding']:+.2f}%"]
        if sig["oi_chg"] is not None:
            parts.append(f"OI{sig['oi_chg']:+.0f}%")
        if sig["ls"] is not None:
            parts.append(f"LS{sig['ls']:.1f}")
        sig_lines.append(" ".join(parts))
    if sig_lines:
        lines += ["", "Futures (fund/OI 4h/topLS):"] + sig_lines

    text = "\n".join(lines)
    print(text)

    if "--post" in sys.argv:
        if rows:
            send_telegram(text)
        else:
            print("(empty table — not posting)")

    # сохраняем текущие ratio всех прошедших фильтр токенов для расчёта CHNG
    STATE_FILE.write_text(json.dumps({a: r for a, _, _, r, _, _ in rows}, indent=1))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
