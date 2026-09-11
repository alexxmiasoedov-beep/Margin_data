#!/usr/bin/env python3
"""Экспериментальный бот: маржинальная таблица + сигналы аномалий.

Постит в отдельную группу (@Margin_data_exper_bot). Основной бот
(margin_data.py) не трогаем — он шлёт чистую таблицу.

Сигналы по каждому токену из таблицы (B/R >= 3):
  - funding rate фьючерсов (сильно отрицательный = переполнены шорты)
  - изменение открытого интереса за ~4ч (набор позиций)
  - long/short ratio топ-трейдеров
  - почасовая ставка займа в годовых (высокая = пул выгребли), signed API
  - POOL EMPTY: ошибка -3045 у maxBorrowable = занять больше нечего 🔥

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


def margin_rows():
    payload = fetch_json(MARGIN_URL)
    if payload.get("code") != "000000":
        raise RuntimeError(f"Binance error: {payload}")
    data = payload["data"]
    prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
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
        chng = 0.0 if asset not in prev else ratio - prev[asset]
        rows.append((asset, bor, rep, ratio, chng, asset not in prev))
    rows.sort(key=lambda r: -r[1])
    STATE_FILE.write_text(json.dumps({r[0]: r[3] for r in rows}, indent=1))
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
    rows, calc_time = margin_rows()
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

    lines += ["", "Signals:"]
    for asset in assets:
        sig = futures_signals(asset)
        extras = []
        if sig:
            extras.append(f"f{sig['funding']:+.2f}")
            if sig["oi_chg"] is not None:
                extras.append(f"OI{sig['oi_chg']:+.0f}%")
            if sig["ls"] is not None:
                extras.append(f"LS{sig['ls']:.1f}")
        apr = rates.get(asset)
        alert = []
        if apr is not None and apr >= APR_ALERT:
            alert.append(f"APR{apr:.0f}%")
        if pool_empty(asset):
            alert.append("POOL EMPTY🔥")
        if extras:
            lines.append(f"{asset:7}" + " ".join(extras))
            if alert:
                lines.append(f"{'':7}" + " ".join(alert) + " ⚠️")
        elif alert:
            lines.append(f"{asset:7}" + " ".join(alert) + " ⚠️")

    text = "\n".join(lines)
    print(text)

    if "--post" in sys.argv:
        send_telegram(text)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
