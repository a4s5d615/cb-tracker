"""
Data fetcher for CB Tracker.

Discovery strategy (v2 - ISIN-based):
  - Fetch complete CB list from TWSE ISIN database (strMode=3)
  - Single HTTP request → all 378+ active Taiwan CBs
  - No enumeration / no IP block risk

CB prices:    mis.twse.com.tw  (batch 20 codes, ~19 batches for 378 CBs)
Stock prices: TWSE/TPEX OpenAPI
CB list:      TWSE ISIN strMode=3 (bonds/convertible bonds section)
Conv. price:  MOPS scrape (blocked for many companies → manual fallback)
"""

import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional

import httpx
import pytz
from bs4 import BeautifulSoup
import aiosqlite

import io
import openpyxl

from backend.config import (
    BROWSER_HEADERS, MIS_TWSE_URL,
    TWSE_STOCK_ALL_URL, TPEX_STOCK_URL,
    TWSE_COMPANY_URL, TPEX_COMPANY_URL,
    MOPS_CB_URL, TWSE_STOCK_DAY_URL,
    FETCH_TIMEOUT, RATE_LIMIT_DELAY, DB_PATH,
    CBAS_ISSUED_URL, CBAS_HEADERS,
)
from backend.database import log_fetch


# ── Helpers ──────────────────────────────────────────────────

def _float(val, default=None) -> Optional[float]:
    if val is None:
        return default
    s = str(val).strip().replace(",", "").replace("+", "")
    if s in ("", "-", "--", "---", "X", "除權", "除息", "N/A"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def last_trading_date() -> str:
    """
    Return the most recent trading day for which market data is available.
    TWSE/TPEX APIs update after market close (~15:30 Taiwan time).
    Before 15:30, return the previous trading day so we don't store stale data.
    """
    tz = pytz.timezone("Asia/Taipei")
    now = datetime.now(tz)
    d = now
    # Before 15:30, today's closing data isn't available yet — use previous day
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        d -= timedelta(days=1)
    # Roll back to the nearest weekday
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def roc_date_str(iso: str) -> str:
    """2026-05-21 → 1150521 (TPEX format)."""
    dt = datetime.strptime(iso, "%Y-%m-%d")
    return f"{dt.year - 1911:03d}{dt.month:02d}{dt.day:02d}"


def roc_slash_str(iso: str) -> str:
    """2026-05-21 → 115/05/21 (TPEX bond query format)."""
    dt = datetime.strptime(iso, "%Y-%m-%d")
    return f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=BROWSER_HEADERS,
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        http2=False,
    )


# ── Stock price APIs ──────────────────────────────────────────

async def fetch_twse_stocks_today() -> dict[str, dict]:
    """
    Return {stock_code: {close, name}} for all TWSE stocks.
    Tries STOCK_DAY_ALL first; if the data is stale (not today's date),
    falls back to MI_INDEX which updates sooner after market close.
    """
    tz = pytz.timezone("Asia/Taipei")
    today_roc = datetime.now(tz).strftime("%Y%m%d")
    today_roc_short = f"{datetime.now(tz).year - 1911}{datetime.now(tz).strftime('%m%d')}"  # e.g. 1150601

    async with make_client() as client:
        # ── Try STOCK_DAY_ALL first ──────────────────────────────
        try:
            resp = await client.get(TWSE_STOCK_ALL_URL)
            rows = resp.json()
            if rows:
                first_date = rows[0].get("Date", "")  # ROC format e.g. "1150601"
                if first_date == today_roc_short:
                    result = {}
                    for r in rows:
                        code = r.get("Code", "")
                        price = _float(r.get("ClosingPrice"))
                        if code and price is not None and price > 0:
                            result[code] = {"close": price, "name": r.get("Name", "")}
                    print(f"[TWSE stocks] STOCK_DAY_ALL OK — {len(result)} stocks, date {first_date}")
                    return result
                else:
                    print(f"[TWSE stocks] STOCK_DAY_ALL stale ({first_date}), falling back to MI_INDEX")
        except Exception as e:
            print(f"[TWSE stocks] STOCK_DAY_ALL error: {e}")

        # ── Fallback: MI_INDEX (updates ~15-16:00 Taiwan time) ──
        # Table fields: [證券代號, 證券名稱, 成交股數, ..., 收盤價(idx 8), ...]
        try:
            resp = await client.get(
                "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                params={"date": today_roc, "type": "ALL", "response": "json"},
            )
            data = resp.json()
            result = {}
            for table in data.get("tables", []):
                fields = table.get("fields", [])
                # Stock price table has 9+ columns and contains "收盤價"
                if len(fields) < 9 or "收盤" not in str(fields):
                    continue
                for row in table.get("data", []):
                    if len(row) < 9:
                        continue
                    code = str(row[0]).strip()
                    name = str(row[1]).strip() if len(row) > 1 else ""
                    price = _float(str(row[8]).replace(",", ""))
                    if code and price and price > 0:
                        result[code] = {"close": price, "name": name}
            print(f"[TWSE stocks] MI_INDEX fallback — {len(result)} stocks")
            return result
        except Exception as e:
            print(f"[TWSE stocks] MI_INDEX error: {e}")
            return {}


async def fetch_tpex_stocks_today(trade_date: str) -> dict[str, dict]:
    """Return {stock_code: {close, name}} for all TPEX stocks."""
    async with make_client() as client:
        try:
            resp = await client.get(TPEX_STOCK_URL, params={"date": roc_date_str(trade_date)})
            result = {}
            for r in resp.json():
                code = r.get("SecuritiesCompanyCode", "")
                price = _float(r.get("Close"))
                if code and price is not None and price > 0:
                    result[code] = {
                        "close": price,
                        "name": r.get("CompanyAbbreviation", r.get("CompanyName", "")),
                    }
            return result
        except Exception as e:
            print(f"[TPEX stocks] {e}")
            return {}


async def fetch_stock_history_twse(stock_code: str, year: int, month: int) -> list[dict]:
    """Monthly OHLCV history for a TWSE stock."""
    async with make_client() as client:
        try:
            resp = await client.get(
                TWSE_STOCK_DAY_URL,
                params={"response": "json", "date": f"{year}{month:02d}01", "stockNo": stock_code},
            )
            data = resp.json()
            if data.get("stat") != "OK":
                return []
            rows = []
            for r in data.get("data", []):
                parts = str(r[0]).split("/")
                if len(parts) != 3:
                    continue
                iso = f"{int(parts[0]) + 1911}-{parts[1]:>02}-{parts[2]:>02}"
                rows.append({
                    "date": iso, "open": _float(r[3]), "high": _float(r[4]),
                    "low": _float(r[5]), "close": _float(r[6]),
                    "volume": int(_float(r[1], 0)),
                })
            return rows
        except Exception as e:
            print(f"[TWSE history {stock_code}] {e}")
            return []


# ── CB price API (mis.twse.com.tw) ───────────────────────────

CB_BATCH = 20   # safe batch size for mis.twse


async def _fetch_mis_batch(client: httpx.AsyncClient, codes: list[str]) -> list[dict]:
    """Fetch one batch of CB codes from mis.twse.com.tw."""
    ex_ch = "|".join(f"otc_{c}.tw" for c in codes)
    try:
        resp = await client.get(
            MIS_TWSE_URL,
            params={"ex_ch": ex_ch, "_": str(int(datetime.now().timestamp() * 1000))},
        )
        items = resp.json().get("msgArray", [])
        return [
            {
                "cb_code":    i.get("c", "").strip(),
                "cb_name":    i.get("n", ""),
                "cb_full":    i.get("nf", ""),
                "close":      _float(i.get("z")) or _float(i.get("y")),
                "prev_close": _float(i.get("y")),
                "open":       _float(i.get("o")),
                "high":       _float(i.get("h")),
                "low":        _float(i.get("l")),
                "volume":     int(_float(i.get("v"), 0)),
            }
            for i in items
            if i.get("c") and (i.get("z") != "-" or i.get("y"))
        ]
    except Exception as e:
        print(f"[MIS batch] {e}")
        return []


async def fetch_cb_prices_mis(cb_codes: list[str]) -> dict[str, dict]:
    """Fetch CB prices for a list of codes. Returns {cb_code: {...}}."""
    results: dict[str, dict] = {}
    batches = [cb_codes[i:i + CB_BATCH] for i in range(0, len(cb_codes), CB_BATCH)]
    print(f"[MIS] Fetching prices for {len(cb_codes)} CBs in {len(batches)} batches")

    async with make_client() as client:
        for idx, batch in enumerate(batches):
            items = await _fetch_mis_batch(client, batch)
            for item in items:
                code = item["cb_code"]
                if code:
                    results[code] = item
            await asyncio.sleep(RATE_LIMIT_DELAY)

    return results


# ── CB Discovery: ISIN strMode=3 ──────────────────────────────

ISIN_BONDS_URL = "https://isin.twse.com.tw/isin/C_public.jsp"
ISIN_BONDS_MODE = "3"


async def discover_cbs_from_isin() -> list[dict]:
    """
    Fetch the complete list of active Taiwan convertible bonds from
    the TWSE ISIN database (strMode=3, 轉換公司債 section).

    Returns list of dicts:
      {cb_code, cb_name, stock_code, isin, issue_date, maturity_date,
       interest_rate, market, cb_sequence}
    """
    async with make_client() as client:
        try:
            resp = await client.get(
                ISIN_BONDS_URL,
                params={"strMode": ISIN_BONDS_MODE},
                headers={**BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"},
            )
            # ISIN page uses MS950 (Big5 superset)
            text = resp.content.decode("big5", errors="replace")
        except Exception as e:
            print(f"[ISIN] Failed to fetch: {e}")
            return []

    soup = BeautifulSoup(text, "lxml")
    rows = soup.find_all("tr")

    in_cb_section = False
    cbs: list[dict] = []

    for tr in rows:
        cells = tr.find_all("td")
        if not cells:
            continue

        first = cells[0]
        txt = first.get_text(strip=True)

        # Section header (merged cell with colspan)
        if first.get("colspan"):
            in_cb_section = "轉換公司債" in txt
            continue

        if not in_cb_section:
            continue

        # Data row: first cell = "CBCODE\u3000CB名稱"
        if len(txt) < 5 or not txt[:5].isdigit():
            continue

        code_name = txt.split("\u3000", 1)  # ideographic space separator
        cb_code = code_name[0].strip()[:6]  # at most 6 chars
        if not (len(cb_code) == 5 and cb_code.isdigit()):
            continue

        cb_name = code_name[1].strip() if len(code_name) > 1 else ""
        isin_code = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        issue_date = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        maturity_date = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        interest_rate = _float(cells[4].get_text(strip=True)) if len(cells) > 4 else None
        market = cells[5].get_text(strip=True) if len(cells) > 5 else "上櫃"

        stock_code = cb_code[:4]
        cb_sequence = cb_code[4]

        cbs.append({
            "cb_code":       cb_code,
            "cb_name":       cb_name,
            "stock_code":    stock_code,
            "isin":          isin_code,
            "issue_date":    issue_date,
            "maturity_date": maturity_date,
            "interest_rate": interest_rate,
            "market":        market,
            "cb_sequence":   cb_sequence,
        })

    print(f"[ISIN] Found {len(cbs)} convertible bonds")
    return cbs


# ── CBAS full sync (works from overseas — no geo-restriction) ──

async def _download_cbas_excel_wb():
    """Download CBAS issued CB Excel and return openpyxl workbook."""
    async with httpx.AsyncClient(headers=CBAS_HEADERS, timeout=30, follow_redirects=True) as client:
        resp = await client.get(CBAS_ISSUED_URL)
        resp.raise_for_status()
    return openpyxl.load_workbook(io.BytesIO(resp.content), data_only=True)


async def fetch_cbas_full_data() -> list[dict]:
    """
    Download CBAS Excel and return full CB data.
    Column mapping (1-indexed):
      Col 1  = CB代號       Col 2  = CB名稱
      Col 4  = 到期日       Col 10 = 轉換價格
      Col 11 = 股票代號     Col 12 = 股票參考價格
      Col 14 = CB市價(可賣出價格)
    """
    try:
        wb = await _download_cbas_excel_wb()
    except Exception as e:
        print(f"[CBAS full] Download failed: {e}")
        return []

    ws = wb.active
    result = []
    for row_idx in range(2, ws.max_row + 1):
        cb_code = str(ws.cell(row_idx, 1).value or "").strip()
        if not (len(cb_code) == 5 and cb_code.isdigit()):
            continue
        cb_name          = str(ws.cell(row_idx, 2).value  or "").strip()
        maturity         = ws.cell(row_idx, 4).value
        outstanding_lots = _float(ws.cell(row_idx, 6).value)   # 流通餘額(張數)
        outstanding_pct  = _float(ws.cell(row_idx, 7).value)   # 餘額比例 0-100
        conv_price       = _float(ws.cell(row_idx, 10).value)
        stock_code       = str(ws.cell(row_idx, 11).value or "").strip().lstrip("'")
        stock_ref        = _float(ws.cell(row_idx, 12).value)
        cb_price         = _float(ws.cell(row_idx, 14).value)

        # Normalise maturity date → YYYY-MM-DD
        maturity_str = None
        if maturity:
            s = str(maturity).replace("/", "-").strip()
            if len(s) == 10:
                maturity_str = s

        result.append({
            "cb_code":          cb_code,
            "cb_name":          cb_name,
            "stock_code":       stock_code,
            "maturity_date":    maturity_str,
            "outstanding_lots": int(outstanding_lots) if outstanding_lots else None,
            "outstanding_pct":  outstanding_pct,
            "conversion_price": conv_price,
            "stock_ref_price":  stock_ref,
            "cb_price":         cb_price,
        })

    print(f"[CBAS full] Parsed {len(result)} CBs from Excel")
    return result


async def sync_all_from_cbas(trade_date: Optional[str] = None) -> dict:
    """
    Full CB discovery + price sync using CBAS Excel as the only data source.
    Works from overseas (no geo-restricted TWSE/ISIN APIs needed).

    Steps:
      1. CBAS Excel → CB list, CB prices, conversion prices
      2. TWSE/TPEX OpenAPI → stock prices (public APIs, no geo-restriction)
      3. Store everything; compute premium_history where possible.
    """
    trade_date = trade_date or last_trading_date()
    print(f"[CBAS sync-all] Starting for {trade_date}")

    cbas_rows = await fetch_cbas_full_data()
    if not cbas_rows:
        return {"status": "failed", "reason": "cbas_fetch_failed"}

    # Parallel: stock prices from TWSE + TPEX (both are public APIs)
    twse_stocks, tpex_stocks = await asyncio.gather(
        fetch_twse_stocks_today(),
        fetch_tpex_stocks_today(trade_date),
    )
    all_stocks = {**tpex_stocks, **twse_stocks}
    print(f"[CBAS sync-all] {len(twse_stocks)} TWSE + {len(tpex_stocks)} TPEX stock prices")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Save all stock prices
        for stock_code, sp in all_stocks.items():
            await db.execute(
                "INSERT OR REPLACE INTO stock_prices (stock_code, trade_date, close_price, source) VALUES (?,?,?,?)",
                (stock_code, trade_date, sp["close"],
                 "twse" if stock_code in twse_stocks else "tpex"),
            )

        # Resolve stock names from API data
        stock_names = {
            code: info.get("name", "") for code, info in {**tpex_stocks, **twse_stocks}.items()
        }

        cb_saved = premium_saved = 0
        for item in cbas_rows:
            cb_code    = item["cb_code"]
            stock_code = item["stock_code"]
            cb_price   = item["cb_price"]
            conv_price = item["conversion_price"]
            stock_name = stock_names.get(stock_code, "")
            market     = "TWSE" if stock_code in twse_stocks else "TPEX"

            # Upsert CB record
            await db.execute(
                """INSERT INTO convertible_bonds
                     (cb_code, cb_name, stock_code, stock_name, stock_market,
                      maturity_date, conversion_price, outstanding_lots, outstanding_pct, is_active)
                   VALUES (?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(cb_code) DO UPDATE SET
                     cb_name          = CASE WHEN cb_name='' THEN excluded.cb_name ELSE cb_name END,
                     stock_code       = excluded.stock_code,
                     stock_name       = CASE WHEN (stock_name IS NULL OR stock_name='')
                                             THEN excluded.stock_name ELSE stock_name END,
                     stock_market     = excluded.stock_market,
                     maturity_date    = CASE WHEN maturity_date IS NULL
                                             THEN excluded.maturity_date ELSE maturity_date END,
                     conversion_price = excluded.conversion_price,
                     outstanding_lots = excluded.outstanding_lots,
                     outstanding_pct  = excluded.outstanding_pct,
                     is_active        = 1,
                     updated_at       = datetime('now','localtime')""",
                (cb_code, item["cb_name"], stock_code, stock_name, market,
                 item.get("maturity_date"), conv_price,
                 item.get("outstanding_lots"), item.get("outstanding_pct")),
            )

            # Save CB price — INSERT OR IGNORE so we don't overwrite a real MIS close price
            # (13:35 job stores source='mis_close'; CBAS is a lower-priority fallback)
            if cb_price and cb_price > 0:
                await db.execute(
                    "INSERT OR IGNORE INTO cb_prices (cb_code, trade_date, close_price, source) VALUES (?,?,?,?)",
                    (cb_code, trade_date, cb_price, "cbas"),
                )
                cb_saved += 1

                # Use actual MIS close price if already stored, else CBAS/ref price
                cur2 = await db.execute(
                    "SELECT close_price FROM cb_prices WHERE cb_code=? AND trade_date=?",
                    (cb_code, trade_date),
                )
                row2 = await cur2.fetchone()
                actual_cb_price = row2[0] if row2 else cb_price

                # Use API stock price first, fall back to CBAS reference price
                stock_price = (all_stocks.get(stock_code) or {}).get("close") \
                              or item.get("stock_ref_price")

                if conv_price and conv_price > 0 and stock_price:
                    parity = stock_price * (100.0 / conv_price)
                    premium_pct = (actual_cb_price - parity) / parity * 100.0
                    await db.execute(
                        """INSERT OR REPLACE INTO premium_history
                           (cb_code, trade_date, cb_price, stock_price, conversion_price, parity, premium_pct)
                           VALUES (?,?,?,?,?,?,?)""",
                        (cb_code, trade_date, actual_cb_price, stock_price, conv_price, parity, premium_pct),
                    )
                    premium_saved += 1

        await db.commit()

    result = {
        "status":             "ok",
        "trade_date":         trade_date,
        "cbs_synced":         len(cbas_rows),
        "cb_prices_saved":    cb_saved,
        "premiums_calculated": premium_saved,
        "twse_stocks":        len(twse_stocks),
        "tpex_stocks":        len(tpex_stocks),
    }
    print(f"[CBAS sync-all] Done: {result}")
    await log_fetch("cbas_sync_all", trade_date, "success", len(cbas_rows), str(result))
    return result


# ── Conversion prices from CBAS (cbas16889.pscnet.com.tw) ────

async def fetch_conversion_prices_cbas() -> dict[str, float]:
    """
    Download the public 已發行CB Excel from CBAS and extract conversion prices.
    Returns {cb_code: conversion_price} for all CBs in the file.
    Column mapping (1-indexed):
      Col 1  = CB代號 (bond_code)
      Col 10 = 轉換價格 (conversion_price)
      Col 11 = 股票代號 (for verification)
    """
    async with httpx.AsyncClient(headers=CBAS_HEADERS, timeout=30, follow_redirects=True) as client:
        try:
            resp = await client.get(CBAS_ISSUED_URL)
            resp.raise_for_status()
        except Exception as e:
            print(f"[CBAS] Download failed: {e}")
            return {}

    try:
        wb = openpyxl.load_workbook(io.BytesIO(resp.content), data_only=True)
        ws = wb.active

        result: dict[str, float] = {}
        for row_idx in range(2, ws.max_row + 1):   # skip header row
            cb_code = str(ws.cell(row_idx, 1).value or "").strip()
            conv_raw = ws.cell(row_idx, 10).value
            if not cb_code or not conv_raw:
                continue
            try:
                conv_price = float(conv_raw)
                if conv_price > 0 and len(cb_code) == 5 and cb_code.isdigit():
                    result[cb_code] = conv_price
            except (ValueError, TypeError):
                continue

        print(f"[CBAS] Extracted conversion prices for {len(result)} CBs")
        return result

    except Exception as e:
        print(f"[CBAS] Parse failed: {e}")
        return {}


async def sync_conversion_prices_from_cbas() -> dict:
    """
    Fetch conversion prices from CBAS, update DB, and backfill premium history.
    Returns summary dict.
    """
    conv_prices = await fetch_conversion_prices_cbas()
    if not conv_prices:
        return {"status": "failed", "updated": 0, "backfilled": 0}

    updated = 0
    backfilled_total = 0

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Get all active CBs
        cur = await db.execute(
            "SELECT cb_code, stock_code, conversion_price FROM convertible_bonds WHERE is_active=1"
        )
        active_cbs = {r["cb_code"]: dict(r) for r in await cur.fetchall()}

    for cb_code, new_price in conv_prices.items():
        if cb_code not in active_cbs:
            continue
        old_price = active_cbs[cb_code].get("conversion_price")
        # Update if price changed or was null
        if old_price is None or abs(float(old_price) - new_price) > 0.001:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE convertible_bonds SET conversion_price=?, updated_at=datetime('now','localtime') WHERE cb_code=?",
                    (new_price, cb_code),
                )
                await db.commit()
            updated += 1

        # Backfill premium_history using new price
        backfilled = await _backfill_premium_fetcher(cb_code, new_price)
        backfilled_total += backfilled

    result = {
        "status": "ok",
        "cbas_prices_fetched": len(conv_prices),
        "cbs_updated": updated,
        "history_days_backfilled": backfilled_total,
    }
    print(f"[CBAS sync] {result}")
    return result


async def _backfill_premium_fetcher(cb_code: str, conversion_price: float) -> int:
    """Same logic as main.py _backfill_premium but callable from fetcher."""
    if not conversion_price or conversion_price <= 0:
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT stock_code FROM convertible_bonds WHERE cb_code=?", (cb_code,)
        )
        row = await cur.fetchone()
        if not row:
            return 0
        stock_code = row["stock_code"]
        cur = await db.execute(
            """SELECT cbp.trade_date, cbp.close_price AS cb_price, sp.close_price AS stock_price
               FROM cb_prices cbp
               LEFT JOIN stock_prices sp ON sp.stock_code=? AND sp.trade_date=cbp.trade_date
               WHERE cbp.cb_code=? ORDER BY cbp.trade_date""",
            (stock_code, cb_code),
        )
        price_rows = await cur.fetchall()
        count = 0
        for r in price_rows:
            cb_price, stock_price = r["cb_price"], r["stock_price"]
            if not cb_price or not stock_price:
                continue
            parity = stock_price * (100.0 / conversion_price)
            premium_pct = (cb_price - parity) / parity * 100.0
            await db.execute(
                """INSERT OR REPLACE INTO premium_history
                   (cb_code, trade_date, cb_price, stock_price, conversion_price, parity, premium_pct)
                   VALUES (?,?,?,?,?,?,?)""",
                (cb_code, r["trade_date"], cb_price, stock_price, conversion_price, parity, premium_pct),
            )
            count += 1
        await db.commit()
    return count


# ── Conversion price scraping (MOPS) ─────────────────────────

async def fetch_conversion_price_mops(stock_code: str) -> Optional[float]:
    """Try to scrape conversion price from MOPS. Returns None if blocked."""
    async with make_client() as client:
        try:
            resp = await client.post(
                MOPS_CB_URL,
                data={
                    "encodeURIComponent": "1", "step": "1",
                    "firstin": "1", "co_id": stock_code,
                    "TYPEK": "all", "isnew": "false",
                },
                headers={**BROWSER_HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                         "Origin": "https://mops.twse.com.tw"},
            )
            if "SECURITY" in resp.text or len(resp.text) < 500:
                return None
            soup = BeautifulSoup(resp.text, "lxml")
            for td in soup.find_all("td"):
                if any(kw in td.get_text() for kw in ("轉換價格", "換股價格", "轉換價")):
                    sib = td.find_next_sibling("td")
                    if sib:
                        m = re.search(r"([\d,]+\.?\d*)", sib.get_text())
                        if m:
                            return _float(m.group(1))
        except Exception as e:
            print(f"[MOPS {stock_code}] {e}")
    return None


# ── Main refresh pipelines ────────────────────────────────────

async def run_full_discovery(
    trade_date: Optional[str] = None,
    progress_callback=None,
) -> dict:
    """
    Full discovery + price fetch pipeline.
    1. Fetch CB list from ISIN database (single request)
    2. Fetch stock prices from TWSE/TPEX OpenAPI
    3. Fetch CB prices from mis.twse.com.tw
    4. Store everything to DB
    """
    trade_date = trade_date or last_trading_date()
    print(f"[Full Discovery] Starting for {trade_date}")

    if progress_callback:
        progress_callback(5, "正在從 ISIN 資料庫獲取可轉債清單...")

    # Step 1: Discover CBs from ISIN
    isin_cbs = await discover_cbs_from_isin()
    if not isin_cbs:
        return {"status": "isin_fetch_failed", "trade_date": trade_date}

    if progress_callback:
        progress_callback(20, f"找到 {len(isin_cbs)} 筆可轉債，獲取股價中...")

    # Step 2: Fetch stock prices in parallel
    twse_stocks, tpex_stocks = await asyncio.gather(
        fetch_twse_stocks_today(),
        fetch_tpex_stocks_today(trade_date),
    )
    all_stocks = {**tpex_stocks, **twse_stocks}
    print(f"[Full Discovery] {len(twse_stocks)} TWSE + {len(tpex_stocks)} TPEX stock prices")

    if progress_callback:
        progress_callback(40, f"獲取 {len(isin_cbs)} 筆可轉債價格...")

    # Step 3: Fetch CB prices from mis.twse
    cb_codes = [cb["cb_code"] for cb in isin_cbs]
    cb_prices = await fetch_cb_prices_mis(cb_codes)
    print(f"[Full Discovery] Got prices for {len(cb_prices)} CBs")

    if progress_callback:
        progress_callback(80, "儲存資料中...")

    # Step 4: Store to DB
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Save stock prices
        for stock_code, sp in all_stocks.items():
            await db.execute(
                "INSERT OR REPLACE INTO stock_prices (stock_code, trade_date, close_price, source) VALUES (?,?,?,?)",
                (stock_code, trade_date, sp["close"], "twse" if stock_code in twse_stocks else "tpex"),
            )

        cb_saved = 0
        premium_saved = 0
        for cb in isin_cbs:
            cb_code = cb["cb_code"]
            stock_code = cb["stock_code"]
            market = "TWSE" if stock_code in twse_stocks else "TPEX"
            stock_name = all_stocks.get(stock_code, {}).get("name", "")
            price_data = cb_prices.get(cb_code, {})
            cb_price = price_data.get("close")

            # Upsert CB metadata
            await db.execute(
                """INSERT INTO convertible_bonds
                     (cb_code, cb_name, cb_full_name, stock_code, stock_name, stock_market,
                      maturity_date, is_active)
                   VALUES (?,?,?,?,?,?,?,1)
                   ON CONFLICT(cb_code) DO UPDATE SET
                     cb_name = CASE WHEN cb_name='' THEN excluded.cb_name ELSE cb_name END,
                     cb_full_name = CASE WHEN cb_full_name IS NULL THEN excluded.cb_full_name ELSE cb_full_name END,
                     stock_code = CASE WHEN stock_code='' THEN excluded.stock_code ELSE stock_code END,
                     stock_name = CASE WHEN (stock_name IS NULL OR stock_name='') THEN excluded.stock_name ELSE stock_name END,
                     stock_market = excluded.stock_market,
                     maturity_date = CASE WHEN maturity_date IS NULL THEN excluded.maturity_date ELSE maturity_date END,
                     is_active = 1,
                     updated_at = datetime('now','localtime')
                """,
                (
                    cb_code,
                    cb.get("cb_name", ""),
                    price_data.get("cb_full", cb.get("cb_name", "")),
                    stock_code,
                    stock_name,
                    market,
                    cb.get("maturity_date"),
                ),
            )

            # Save CB price if available
            if cb_price:
                await db.execute(
                    """INSERT OR REPLACE INTO cb_prices
                       (cb_code, trade_date, open_price, high_price, low_price, close_price, prev_close, volume, source)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        cb_code, trade_date,
                        price_data.get("open"), price_data.get("high"),
                        price_data.get("low"), cb_price,
                        price_data.get("prev_close"), price_data.get("volume", 0),
                        "mis",
                    ),
                )
                cb_saved += 1

            # Calculate premium if conversion price is known
            cur = await db.execute(
                "SELECT conversion_price FROM convertible_bonds WHERE cb_code=?", (cb_code,)
            )
            row = await cur.fetchone()
            conv_price = row[0] if row else None
            stock_price = all_stocks.get(stock_code, {}).get("close")

            if conv_price and conv_price > 0 and stock_price and cb_price:
                parity = stock_price * (100.0 / conv_price)
                premium_pct = (cb_price - parity) / parity * 100.0
                await db.execute(
                    """INSERT OR REPLACE INTO premium_history
                       (cb_code, trade_date, cb_price, stock_price, conversion_price, parity, premium_pct)
                       VALUES (?,?,?,?,?,?,?)""",
                    (cb_code, trade_date, cb_price, stock_price, conv_price, parity, premium_pct),
                )
                premium_saved += 1

        await db.commit()

    if progress_callback:
        progress_callback(100, f"完成！找到 {len(isin_cbs)} 筆可轉債，{cb_saved} 筆有價格資料")

    summary = {
        "status": "ok",
        "trade_date": trade_date,
        "active_cbs_found": len(isin_cbs),
        "cb_prices_saved": cb_saved,
        "premiums_calculated": premium_saved,
        "twse_stocks": len(twse_stocks),
        "tpex_stocks": len(tpex_stocks),
    }
    print(f"[Full Discovery] Done: {summary}")
    await log_fetch("full_discovery", trade_date, "success", len(isin_cbs), str(summary))
    return summary


async def refresh_known_cbs(trade_date: Optional[str] = None) -> dict:
    """
    Fast daily refresh: fetch prices for already-known active CBs.
    Use this after the initial full discovery.
    """
    trade_date = trade_date or last_trading_date()
    print(f"[Refresh] Starting for {trade_date}")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT cb_code, stock_code, conversion_price FROM convertible_bonds WHERE is_active=1"
        )
        rows = await cur.fetchall()

    if not rows:
        print("[Refresh] No active CBs — running full discovery instead")
        return await run_full_discovery(trade_date)

    cb_meta = {r["cb_code"]: dict(r) for r in rows}
    cb_codes = list(cb_meta.keys())
    stock_codes = list({r["stock_code"] for r in rows if r["stock_code"]})

    # Parallel: CB prices + stock prices
    cb_prices_task = fetch_cb_prices_mis(cb_codes)
    stocks_tasks = asyncio.gather(fetch_twse_stocks_today(), fetch_tpex_stocks_today(trade_date))
    cb_prices, (twse_stocks, tpex_stocks) = await asyncio.gather(cb_prices_task, stocks_tasks)
    all_stocks = {**tpex_stocks, **twse_stocks}

    async with aiosqlite.connect(DB_PATH) as db:
        # Stock prices
        for stock_code in stock_codes:
            sp = all_stocks.get(stock_code)
            if sp:
                await db.execute(
                    "INSERT OR REPLACE INTO stock_prices (stock_code, trade_date, close_price, source) VALUES (?,?,?,?)",
                    (stock_code, trade_date, sp["close"],
                     "twse" if stock_code in twse_stocks else "tpex"),
                )

        cb_saved = premium_saved = 0
        for cb_code, p in cb_prices.items():
            meta = cb_meta.get(cb_code, {})
            stock_code = meta.get("stock_code", cb_code[:4])
            cb_price = p.get("close")
            conv_price = meta.get("conversion_price")
            stock_price = all_stocks.get(stock_code, {}).get("close")

            if cb_price:
                await db.execute(
                    """INSERT OR REPLACE INTO cb_prices
                       (cb_code, trade_date, open_price, high_price, low_price, close_price, prev_close, volume, source)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (cb_code, trade_date, p.get("open"), p.get("high"), p.get("low"),
                     cb_price, p.get("prev_close"), p.get("volume", 0), "mis"),
                )
                cb_saved += 1

                if conv_price and conv_price > 0 and stock_price:
                    parity = stock_price * (100.0 / conv_price)
                    premium_pct = (cb_price - parity) / parity * 100.0
                    await db.execute(
                        """INSERT OR REPLACE INTO premium_history
                           (cb_code, trade_date, cb_price, stock_price, conversion_price, parity, premium_pct)
                           VALUES (?,?,?,?,?,?,?)""",
                        (cb_code, trade_date, cb_price, stock_price, conv_price, parity, premium_pct),
                    )
                    premium_saved += 1

        await db.commit()

    summary = {
        "status": "ok", "trade_date": trade_date,
        "cb_prices_saved": cb_saved, "premiums_calculated": premium_saved,
    }
    print(f"[Refresh] Done: {summary}")
    return summary


async def refresh_all_prices(trade_date: Optional[str] = None) -> dict:
    """Entry point: fast refresh if we have CBs, full discovery if not."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM convertible_bonds WHERE is_active=1")
        count = (await cur.fetchone())[0]

    if count == 0:
        return await run_full_discovery(trade_date)
    return await refresh_known_cbs(trade_date)


# ── Re-sync CB list from ISIN (call weekly to catch new/expired CBs) ─────

async def fetch_tpex_bond_prices_for_date(trade_date: str) -> dict[str, float]:
    """
    Fetch OTC (TPEX) bond close prices for a specific historical date.
    Returns {bond_code: close_price} for convertible bonds found.
    Uses: https://www.tpex.org.tw/web/bond/transaction/bdo_dd/ajax.php
    """
    roc = roc_slash_str(trade_date)
    url = "https://www.tpex.org.tw/web/bond/transaction/bdo_dd/ajax.php"
    params = {"l": "zh-tw", "o": "json", "d": roc, "s": "0,asc,0"}
    async with make_client() as client:
        try:
            resp = await client.get(url, params=params)
            data = resp.json()
        except Exception as e:
            print(f"[TPEX bond hist {trade_date}] {e}")
            return {}

    result: dict[str, float] = {}
    # Response: {"aaData": [["code","name",...,"close",...], ...]}
    rows = data.get("aaData", []) if isinstance(data, dict) else []
    for row in rows:
        if not row or len(row) < 7:
            continue
        code = str(row[0]).strip()
        # TPEX bond codes for CBs are typically 5 digits
        if not (len(code) == 5 and code.isdigit()):
            continue
        close = _float(row[6])  # column 7 is typically close price
        if close and close > 0:
            result[code] = close
    print(f"[TPEX bond hist {trade_date}] Got {len(result)} CB prices")
    return result


async def fetch_twse_stocks_for_date(trade_date: str) -> dict[str, float]:
    """
    Fetch TWSE stock close prices for a specific historical date.
    Uses: https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX
    Returns {stock_code: close_price}.
    """
    dt = datetime.strptime(trade_date, "%Y-%m-%d")
    date_str = dt.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    async with make_client() as client:
        try:
            resp = await client.get(url, params={"date": date_str, "type": "ALL", "response": "json"})
            data = resp.json()
        except Exception as e:
            print(f"[TWSE stocks hist {trade_date}] {e}")
            return {}

    result: dict[str, float] = {}
    # The "tables" field has multiple tables; stock prices are in table with 9 columns
    for table in data.get("tables", []):
        fields = table.get("fields", [])
        if len(fields) < 9 or "收盤" not in str(fields):
            continue
        for row in table.get("data", []):
            if len(row) < 9:
                continue
            code = str(row[0]).strip()
            close = _float(row[8])
            if code and close and close > 0:
                result[code] = close
    print(f"[TWSE stocks hist {trade_date}] Got {len(result)} stock prices")
    return result


async def fetch_tpex_stocks_for_date(trade_date: str) -> dict[str, float]:
    """
    Fetch TPEX stock close prices for a specific historical date.
    Returns {stock_code: close_price}.
    """
    try:
        stocks = await fetch_tpex_stocks_today(trade_date)
        return {code: info["close"] for code, info in stocks.items()}
    except Exception as e:
        print(f"[TPEX stocks hist {trade_date}] {e}")
        return {}


async def fetch_historical_prices_for_date(trade_date: str) -> dict:
    """
    Fetch and store CB prices + stock prices for one specific historical date.
    - CB prices: TPEX OTC bond historical endpoint
    - Stock prices: TWSE/TPEX historical endpoints
    - Computes premium_history for any CB with a known conversion price.
    Returns summary dict.
    """
    print(f"[HistFetch] Fetching historical data for {trade_date}")

    # Parallel: stock prices + TPEX CB prices
    twse_stocks_task = fetch_twse_stocks_for_date(trade_date)
    tpex_stocks_task = fetch_tpex_stocks_for_date(trade_date)
    tpex_cb_task = fetch_tpex_bond_prices_for_date(trade_date)

    twse_stocks, tpex_stocks, tpex_cb_prices = await asyncio.gather(
        twse_stocks_task, tpex_stocks_task, tpex_cb_task
    )
    all_stocks = {**tpex_stocks, **twse_stocks}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Load known CBs with their conversion prices
        cur = await db.execute(
            "SELECT cb_code, stock_code, conversion_price FROM convertible_bonds WHERE is_active=1"
        )
        cb_meta = {r["cb_code"]: dict(r) for r in await cur.fetchall()}

        stocks_saved = 0
        cb_saved = 0
        premium_saved = 0

        # Save stock prices
        for stock_code, close in all_stocks.items():
            await db.execute(
                "INSERT OR REPLACE INTO stock_prices (stock_code, trade_date, close_price, source) VALUES (?,?,?,?)",
                (stock_code, trade_date, close, "twse" if stock_code in twse_stocks else "tpex"),
            )
            stocks_saved += 1

        # Save CB prices and compute premiums
        for cb_code, close in tpex_cb_prices.items():
            meta = cb_meta.get(cb_code)
            if not meta:
                continue
            stock_code = meta["stock_code"]
            conv_price = meta.get("conversion_price")

            await db.execute(
                """INSERT OR REPLACE INTO cb_prices
                   (cb_code, trade_date, close_price, source) VALUES (?,?,?,?)""",
                (cb_code, trade_date, close, "tpex_hist"),
            )
            cb_saved += 1

            stock_price = all_stocks.get(stock_code)
            if conv_price and conv_price > 0 and stock_price:
                parity = stock_price * (100.0 / conv_price)
                premium_pct = (close - parity) / parity * 100.0
                await db.execute(
                    """INSERT OR REPLACE INTO premium_history
                       (cb_code, trade_date, cb_price, stock_price, conversion_price, parity, premium_pct)
                       VALUES (?,?,?,?,?,?,?)""",
                    (cb_code, trade_date, close, stock_price, conv_price, parity, premium_pct),
                )
                premium_saved += 1

        await db.commit()

    summary = {
        "trade_date": trade_date,
        "stocks_saved": stocks_saved,
        "cb_prices_saved": cb_saved,
        "premiums_calculated": premium_saved,
    }
    print(f"[HistFetch] {trade_date}: {summary}")
    return summary


async def backfill_history(days: int = 90) -> dict:
    """
    Backfill historical stock prices for the past N trading days.
    Uses TWSE afterTrading/MI_INDEX and TPEX daily quotes (both support date params).
    After storing stock prices, re-runs premium calculation for any dates where
    we also have CB prices stored (fills gaps in premium_history).
    """
    today = datetime.now()

    # Generate list of past weekdays (most recent first)
    past_dates = []
    d = today - timedelta(days=1)  # start from yesterday
    while len(past_dates) < days:
        if d.weekday() < 5:  # Mon-Fri
            past_dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)

    print(f"[Backfill] Processing stock prices for {len(past_dates)} trading days")

    stocks_total = 0
    days_done = 0

    for trade_date in past_dates:
        try:
            # Parallel: TWSE + TPEX stocks for this date
            twse_task = fetch_twse_stocks_for_date(trade_date)
            tpex_task = fetch_tpex_stocks_for_date(trade_date)
            twse_stocks, tpex_stocks = await asyncio.gather(twse_task, tpex_task)
            all_stocks = {**tpex_stocks, **twse_stocks}

            if all_stocks:
                async with aiosqlite.connect(DB_PATH) as db:
                    for stock_code, close in all_stocks.items():
                        await db.execute(
                            "INSERT OR REPLACE INTO stock_prices (stock_code, trade_date, close_price, source) VALUES (?,?,?,?)",
                            (stock_code, trade_date, close,
                             "twse" if stock_code in twse_stocks else "tpex"),
                        )
                    await db.commit()
                stocks_total += len(all_stocks)

            days_done += 1
            await asyncio.sleep(0.3)  # be polite to APIs
        except Exception as e:
            print(f"[Backfill] Error for {trade_date}: {e}")

    # Re-run premium backfill for all CBs that have stored prices
    # This fills premium_history for any dates where CB prices + stock prices are both now available
    print("[Backfill] Re-computing premiums for all stored CB prices...")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT cb_code, conversion_price FROM convertible_bonds WHERE is_active=1 AND conversion_price IS NOT NULL AND conversion_price>0"
        )
        cbs_with_price = await cur.fetchall()

    prem_total = 0
    for row in cbs_with_price:
        n = await _backfill_premium_fetcher(row["cb_code"], row["conversion_price"])
        prem_total += n

    result = {
        "status": "ok",
        "days_processed": days_done,
        "stock_prices_stored": stocks_total,
        "premiums_recalculated": prem_total,
    }
    print(f"[Backfill] Done: {result}")
    return result


async def sync_cb_list_from_isin(trade_date: Optional[str] = None) -> dict:
    """
    Re-fetch CB list from ISIN and mark expired CBs as inactive.
    Also adds any newly listed CBs to the database.
    """
    trade_date = trade_date or last_trading_date()
    isin_cbs = await discover_cbs_from_isin()
    if not isin_cbs:
        return {"status": "failed"}

    isin_codes = {cb["cb_code"] for cb in isin_cbs}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Mark CBs not in ISIN as inactive
        cur = await db.execute("SELECT cb_code FROM convertible_bonds WHERE is_active=1")
        existing = {r[0] for r in await cur.fetchall()}

        expired = existing - isin_codes
        new_cbs = isin_codes - existing

        for code in expired:
            await db.execute(
                "UPDATE convertible_bonds SET is_active=0, updated_at=datetime('now','localtime') WHERE cb_code=?",
                (code,),
            )

        for cb in isin_cbs:
            if cb["cb_code"] in new_cbs:
                await db.execute(
                    """INSERT OR IGNORE INTO convertible_bonds
                         (cb_code, cb_name, stock_code, stock_market, maturity_date, is_active)
                       VALUES (?,?,?,'TPEX',?,1)""",
                    (cb["cb_code"], cb["cb_name"], cb["stock_code"], cb.get("maturity_date")),
                )

        await db.commit()

    result = {"status": "ok", "total": len(isin_codes), "new": len(new_cbs), "expired": len(expired)}
    print(f"[Sync ISIN] {result}")
    return result


# ── OHLC chart data ───────────────────────────────────────────

async def fetch_stock_ohlc(stock_code: str, market: str, days: int = 120) -> list[dict]:
    """
    Fetch daily OHLC for a TWSE or TPEX stock over the past N days.
    TWSE: afterTrading/STOCK_DAY (monthly endpoint, fetch needed months)
    TPEX: daily quotes
    Returns list of {date, open, high, low, close} sorted ascending.
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    results: list[dict] = []

    # Determine which month-start strings to query
    today = datetime.now()
    month_starts: list[str] = []
    seen: set = set()
    for i in range(days + 31):
        d = today - timedelta(days=i)
        key = d.strftime("%Y%m01")
        if key not in seen:
            seen.add(key)
            month_starts.append(key)

    async with make_client() as client:
        if market == "TWSE":
            for ms in sorted(month_starts):
                try:
                    resp = await client.get(
                        "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
                        params={"stockNo": stock_code, "date": ms, "response": "json"},
                    )
                    data = resp.json()
                    if data.get("stat") != "OK":
                        continue
                    for row in data.get("data", []):
                        try:
                            parts = row[0].split("/")
                            date_str = f"{int(parts[0])+1911}-{parts[1]}-{parts[2]}"
                            if date_str < cutoff:
                                continue
                            o = _float(row[3].replace(",", ""))
                            h = _float(row[4].replace(",", ""))
                            l = _float(row[5].replace(",", ""))
                            c = _float(row[6].replace(",", ""))
                            vol = int(row[1].replace(",", "")) if row[1] else 0
                            if c:
                                results.append({"date": date_str, "open": o or c,
                                                "high": h or c, "low": l or c,
                                                "close": c, "volume": vol})
                        except Exception:
                            continue
                    await asyncio.sleep(0.3)
                except Exception:
                    continue
        else:  # TPEX — use Yahoo Finance (one request, full OHLCV history)
            from datetime import timezone
            TW_TZ = timezone(timedelta(hours=8))
            ticker = f"{stock_code}.TWO"
            try:
                resp = await client.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                    params={"interval": "1d", "range": "2y"},
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "Accept": "application/json",
                    },
                )
                data = resp.json()
                chart_result = data.get("chart", {}).get("result", [])
                if chart_result:
                    r0 = chart_result[0]
                    timestamps = r0.get("timestamp", [])
                    q = r0.get("indicators", {}).get("quote", [{}])[0]
                    opens   = q.get("open",   [])
                    highs   = q.get("high",   [])
                    lows    = q.get("low",    [])
                    closes  = q.get("close",  [])
                    volumes = q.get("volume", [])
                    for i, ts in enumerate(timestamps):
                        try:
                            date_str = datetime.fromtimestamp(ts, tz=TW_TZ).strftime("%Y-%m-%d")
                            if date_str < cutoff:
                                continue
                            c = _float(closes[i]) if i < len(closes) else None
                            o = _float(opens[i])   if i < len(opens)  else None
                            h = _float(highs[i])   if i < len(highs)  else None
                            l = _float(lows[i])    if i < len(lows)   else None
                            vol = int(volumes[i])  if i < len(volumes) and volumes[i] else 0
                            if c:
                                results.append({
                                    "date": date_str,
                                    "open": o or c,
                                    "high": h or c,
                                    "low":  l or c,
                                    "close": c,
                                    "volume": vol,
                                })
                        except Exception:
                            continue
            except Exception as e:
                print(f"[Yahoo OHLC {stock_code}] {e}")

    results.sort(key=lambda x: x["date"])
    # Deduplicate by date
    seen_dates: set = set()
    deduped = []
    for r in results:
        if r["date"] not in seen_dates:
            seen_dates.add(r["date"])
            deduped.append(r)
    return deduped
