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
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

URL = "https://www.binance.com/bapi/margin/v1/public/margin/statistics/24h-borrow-and-repay"
STATE_FILE = Path(__file__).with_name("state.json")

MIN_RATIO = 3.0        # фильтр канала: B/R >= 3
MIN_BORROW_USDT = 10_000  # отсечка мелочи, чтобы не ловить B/R=inf на нулях
EXCLUDE = {"USDT", "USDC", "FDUSD", "TUSD", "DAI"}  # стейблы не интересны


def fetch():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)
    if payload.get("code") != "000000":
        raise RuntimeError(f"Binance error: {payload}")
    return payload["data"]


def fmt_k(value):
    return f"{value / 1000:.1f}K"


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
        if asset in EXCLUDE or bor < MIN_BORROW_USDT or rep <= 0:
            continue
        ratio = bor / rep
        if ratio < MIN_RATIO:
            continue
        prev_ratio = prev.get(asset)
        chng = 0.0 if prev_ratio is None else ratio - prev_ratio
        is_new = asset not in prev
        rows.append((asset, bor, rep, ratio, chng, is_new))

    rows.sort(key=lambda r: -r[1])

    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(calc_time))
    print(f"Margin borrow/repay 24h — {ts}\n")
    print(f"{'SYM':10} {'BOR':>9} {'REP':>9} {'B/R':>5} {'CHNG':>6}")
    for asset, bor, rep, ratio, chng, is_new in rows:
        mark = " 🆕" if is_new else ""
        print(f"{asset:10} {fmt_k(bor):>9} {fmt_k(rep):>9} {ratio:>5.1f} {chng:>6.2f}{mark}")

    # сохраняем текущие ratio всех прошедших фильтр токенов для расчёта CHNG
    STATE_FILE.write_text(json.dumps({a: r for a, _, _, r, _, _ in rows}, indent=1))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
