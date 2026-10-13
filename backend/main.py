"""
FastAPI entry point for CB Tracker.
Run: uvicorn backend.main:app --reload --port 8000
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
import httpx
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.config import DB_PATH, FRONTEND_DIR
from backend.database import init_db, log_fetch
from backend.fetcher import (
    refresh_all_prices, run_full_discovery, refresh_known_cbs,
    fetch_conversion_price_mops,
    last_trading_date, sync_cb_list_from_isin,
    sync_conversion_prices_from_cbas,
    sync_all_from_cbas,
    fetch_historical_prices_for_date,
    backfill_history,
    fetch_stock_ohlc,
)

TW_TZ = pytz.timezone("Asia/Taipei")
scheduler = AsyncIOScheduler(timezone=TW_TZ)


# ──────────────────────────────────────────────────────────
# Global state: track background discovery task
# ──────────────────────────────────────────────────────────

_discovery_status = {"running": False, "progress": 0, "message": "idle", "last_result": None}


# ──────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────

async def _fetch_cb_prices_close():
    """
    13:35 job: fetch CB close prices from MIS while z-field is still populated.
    Stores cb_prices only — stock prices and premiums updated later at 15:30.
    """
    print("[Scheduler 13:35] Fetching CB close prices from MIS...")
    trade_date = last_trading_date()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT cb_code, stock_code, conversion_price FROM convertible_bonds WHERE is_active=1"
        )
        cb_meta = {r["cb_code"]: dict(r) for r in await cur.fetchall()}

    from backend.fetcher import fetch_cb_prices_mis
    cb_prices = await fetch_cb_prices_mis(list(cb_meta.keys()))

    async with aiosqlite.connect(DB_PATH) as db:
        saved = 0
        for cb_code, p in cb_prices.items():
            cb_price = p.get("close")
            if cb_price:
                await db.execute(
                    """INSERT OR REPLACE INTO cb_prices
                       (cb_code, trade_date, open_price, high_price, low_price,
                        close_price, prev_close, volume, source)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (cb_code, trade_date, p.get("open"), p.get("high"), p.get("low"),
                     cb_price, p.get("prev_close"), p.get("volume", 0), "mis_close"),
                )
                saved += 1
        await db.commit()
    print(f"[Scheduler 13:35] Saved {saved} CB close prices for {trade_date}")


async def _scheduled_daily_fetch():
    """
    15:30 job: CBAS sync (CB list + metadata + stock prices) then recalculate premiums.
    CB prices already stored at 13:35 — do NOT overwrite with stale MIS data.
    """
    print("[Scheduler 15:30] Daily stock price + metadata sync triggered")
    result = await sync_all_from_cbas()
    if result.get("status") != "ok":
        await refresh_all_prices()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # 13:35 — fetch CB close prices from MIS while z-field still populated
    scheduler.add_job(
        _fetch_cb_prices_close, "cron",
        day_of_week="mon-fri", hour=13, minute=35,
        id="cb_close_fetch", replace_existing=True,
    )
    # 15:30 — CBAS metadata + stock prices (TWSE API updates by now)
    scheduler.add_job(
        _scheduled_daily_fetch, "cron",
        day_of_week="mon-fri", hour=15, minute=30,
        id="daily_fetch", replace_existing=True,
    )
    scheduler.start()
    asyncio.create_task(_auto_start_if_needed())
    yield
    scheduler.shutdown()


CBAS_CACHE_URL = "https://raw.githubusercontent.com/a4s5d615/cb-tracker/master/data/cbas_cache.json"


async def _load_cbas_cache_from_github():
    """Render 啟動時從 GitHub raw 讀取轉換價 + 流通餘額快取，寫入 DB。"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(CBAS_CACHE_URL)
            if r.status_code != 200:
                print(f"[CBAS Cache] GitHub raw 回應 {r.status_code}，略過")
                return 0, 0
            cache = r.json()

        prices      = cache.get("prices", {})
        outstanding = cache.get("outstanding", {})
        updated_at  = cache.get("updated_at", "unknown")
        print(f"[CBAS Cache] 讀到 {len(prices)} 筆轉換價 / {len(outstanding)} 筆流通餘額（更新時間：{updated_at}）")

        async with aiosqlite.connect(DB_PATH) as db:
            for cb_code, price in prices.items():
                await db.execute(
                    "UPDATE convertible_bonds SET conversion_price=? WHERE cb_code=?",
                    (float(price), cb_code),
                )
            for cb_code, vals in outstanding.items():
                await db.execute(
                    "UPDATE convertible_bonds SET outstanding_lots=?, outstanding_pct=? WHERE cb_code=?",
                    (vals.get("lots"), vals.get("pct"), cb_code),
                )
            await db.commit()
        print(f"[CBAS Cache] 已套用轉換價 {len(prices)} / 流通餘額 {len(outstanding)} 到 DB")

        # 重新計算溢折價（backfill premium_history）
        backfilled = 0
        for cb_code, price in prices.items():
            backfilled += await _backfill_premium(cb_code, float(price))
        print(f"[CBAS Cache] 溢折價重算完成，{backfilled} 筆歷史記錄已更新")
        return len(prices), len(outstanding)
    except Exception as e:
        print(f"[CBAS Cache] 載入失敗：{e}")
        return 0, 0


async def _auto_start_if_needed():
    """On startup: use CBAS sync (works overseas) with ISIN as fallback."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM convertible_bonds WHERE is_active=1")
        count = (await cur.fetchone())[0]

    if count == 0:
        print("[Startup] No CBs in DB — running CBAS full sync...")
        result = await sync_all_from_cbas()
        if result.get("status") != "ok" or result.get("cbs_synced", 0) == 0:
            print("[Startup] CBAS failed, falling back to ISIN discovery...")
            await _run_discovery_bg()
        # 不管 CBAS 成不成功，都嘗試從 GitHub cache 補轉換價
        asyncio.create_task(_load_cbas_cache_from_github())
    else:
        trade_date = last_trading_date()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*) FROM cb_prices WHERE trade_date=?", (trade_date,))
            price_count = (await cur.fetchone())[0]
        if price_count == 0:
            print(f"[Startup] No prices for {trade_date} — running CBAS sync...")
            asyncio.create_task(sync_all_from_cbas(trade_date))
        else:
            print("[Startup] Prices up to date, syncing conversion prices...")
            asyncio.create_task(sync_conversion_prices_from_cbas())
        # 每次啟動都從 GitHub cache 補轉換價 + 流通餘額
        asyncio.create_task(_load_cbas_cache_from_github())


async def _run_discovery_bg():
    """Run full market scan in background, update _discovery_status."""
    global _discovery_status
    _discovery_status = {"running": True, "progress": 0, "message": "正在從 ISIN 資料庫獲取可轉債清單...", "last_result": None}

    def on_progress(pct: int, msg: str):
        global _discovery_status
        _discovery_status = {**_discovery_status, "progress": pct, "message": msg}

    try:
        result = await run_full_discovery(progress_callback=on_progress)
        _discovery_status = {
            "running": False,
            "progress": 100,
            "message": f"完成！找到 {result.get('active_cbs_found', 0)} 筆可轉債，{result.get('cb_prices_saved', 0)} 筆有價格",
            "last_result": result,
        }
    except Exception as e:
        _discovery_status = {"running": False, "progress": -1, "message": f"錯誤: {e}", "last_result": None}


# ──────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────

app = FastAPI(title="可轉債折溢價追蹤器", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────
# Pydantic models
# ──────────────────────────────────────────────────────────

class CBCreate(BaseModel):
    cb_code: str
    cb_name: str
    stock_code: str
    stock_name: Optional[str] = ""
    stock_market: str = "TWSE"
    conversion_price: Optional[float] = None
    notes: Optional[str] = None


class CBUpdate(BaseModel):
    cb_name: Optional[str] = None
    stock_code: Optional[str] = None
    stock_name: Optional[str] = None
    stock_market: Optional[str] = None
    conversion_price: Optional[float] = None
    is_active: Optional[int] = None
    notes: Optional[str] = None
    outstanding_lots: Optional[int] = None
    outstanding_pct: Optional[float] = None


def row_to_dict(row) -> dict:
    return dict(row) if row else {}


# ──────────────────────────────────────────────────────────
# API: CB management
# ──────────────────────────────────────────────────────────

@app.get("/api/cbs")
async def list_cbs(search: str = "", active_only: bool = True, market: str = "all"):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        where, params = ["1=1"], []
        if active_only:
            where.append("is_active=1")
        if market != "all":
            where.append("stock_market=?"); params.append(market.upper())
        if search:
            where.append("(cb_code LIKE ? OR cb_name LIKE ? OR stock_code LIKE ? OR stock_name LIKE ?)")
            s = f"%{search}%"; params += [s, s, s, s]
        cur = await db.execute(
            f"SELECT * FROM convertible_bonds WHERE {' AND '.join(where)} ORDER BY cb_code",
            params,
        )
        return [row_to_dict(r) for r in await cur.fetchall()]


@app.post("/api/cbs", status_code=201)
async def add_cb(cb: CBCreate):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO convertible_bonds (cb_code,cb_name,stock_code,stock_name,stock_market,conversion_price,notes) VALUES (?,?,?,?,?,?,?)",
                (cb.cb_code, cb.cb_name, cb.stock_code, cb.stock_name, cb.stock_market, cb.conversion_price, cb.notes),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(409, f"CB {cb.cb_code} already exists")
    return {"status": "created", "cb_code": cb.cb_code}


@app.put("/api/cbs/{cb_code}")
async def update_cb(cb_code: str, data: CBUpdate):
    fields, params = [], []
    for field, val in data.model_dump(exclude_none=True).items():
        fields.append(f"{field}=?"); params.append(val)
    if not fields:
        raise HTTPException(400, "No fields to update")
    params += [datetime.now().isoformat(), cb_code]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE convertible_bonds SET {', '.join(fields)}, updated_at=? WHERE cb_code=?", params
        )
        await db.commit()

    # If conversion_price was updated, retroactively backfill premium_history
    if data.conversion_price is not None:
        backfilled = await _backfill_premium(cb_code, data.conversion_price)
        return {"status": "updated", "backfilled_days": backfilled}

    return {"status": "updated"}


async def _backfill_premium(cb_code: str, conversion_price: float) -> int:
    """Recalculate premium_history for all stored price rows of this CB."""
    if not conversion_price or conversion_price <= 0:
        return 0

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Get CB stock code
        cur = await db.execute(
            "SELECT stock_code FROM convertible_bonds WHERE cb_code=?", (cb_code,)
        )
        row = await cur.fetchone()
        if not row:
            return 0
        stock_code = row["stock_code"]

        # Join cb_prices with stock_prices for every stored date
        cur = await db.execute(
            """SELECT cbp.trade_date, cbp.close_price AS cb_price, sp.close_price AS stock_price
               FROM cb_prices cbp
               LEFT JOIN stock_prices sp
                 ON sp.stock_code=? AND sp.trade_date=cbp.trade_date
               WHERE cbp.cb_code=?
               ORDER BY cbp.trade_date""",
            (stock_code, cb_code),
        )
        price_rows = await cur.fetchall()

        count = 0
        for r in price_rows:
            cb_price = r["cb_price"]
            stock_price = r["stock_price"]
            if not cb_price or not stock_price:
                continue
            parity = stock_price * (100.0 / conversion_price)
            premium_pct = (cb_price - parity) / parity * 100.0
            await db.execute(
                """INSERT OR REPLACE INTO premium_history
                   (cb_code, trade_date, cb_price, stock_price, conversion_price, parity, premium_pct)
                   VALUES (?,?,?,?,?,?,?)""",
                (cb_code, r["trade_date"], cb_price, stock_price,
                 conversion_price, parity, premium_pct),
            )
            count += 1

        await db.commit()
    return count


class BulkConversionPrice(BaseModel):
    prices: dict  # {cb_code: conversion_price}


@app.post("/api/cbs/bulk-conversion-price")
async def bulk_set_conversion_prices(body: BulkConversionPrice):
    """Set conversion prices for multiple CBs at once and backfill history."""
    results = {}
    for cb_code, price in body.prices.items():
        try:
            price_f = float(price)
            if price_f <= 0:
                results[cb_code] = "invalid"
                continue
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE convertible_bonds SET conversion_price=?, updated_at=datetime('now','localtime') WHERE cb_code=?",
                    (price_f, cb_code),
                )
                await db.commit()
            backfilled = await _backfill_premium(cb_code, price_f)
            results[cb_code] = {"ok": True, "backfilled": backfilled}
        except Exception as e:
            results[cb_code] = {"error": str(e)}
    return {"status": "ok", "results": results}


@app.post("/api/cbs/bulk-outstanding")
async def bulk_set_outstanding(body: dict):
    """批次更新流通餘額(張數)與餘額比例。body: {cb_code: {lots, pct}}"""
    items = body.get("items", {})
    updated = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for cb_code, vals in items.items():
            lots = vals.get("lots")
            pct  = vals.get("pct")
            if lots is None and pct is None:
                continue
            await db.execute(
                "UPDATE convertible_bonds SET outstanding_lots=?, outstanding_pct=?, updated_at=datetime('now','localtime') WHERE cb_code=?",
                (lots, pct, cb_code),
            )
            updated += 1
        await db.commit()
    return {"status": "ok", "updated": updated}


@app.delete("/api/cbs/{cb_code}")
async def deactivate_cb(cb_code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE convertible_bonds SET is_active=0 WHERE cb_code=?", (cb_code,))
        await db.commit()
    return {"status": "deactivated"}


# ──────────────────────────────────────────────────────────
# API: Discovery & refresh
# ──────────────────────────────────────────────────────────

@app.post("/api/discover")
async def trigger_full_discovery():
    """Start full market scan in background. Returns immediately."""
    if _discovery_status["running"]:
        return {"status": "already_running", "message": "掃描已在進行中"}
    asyncio.create_task(_run_discovery_bg())
    return {"status": "started", "message": "開始掃描全市場可轉債..."}


@app.get("/api/discover/status")
async def discovery_status():
    return _discovery_status


@app.post("/api/discover/sync-list")
async def sync_cb_list():
    """Re-sync CB list from ISIN (add new, expire old). Fast operation."""
    result = await sync_cb_list_from_isin()
    return result


@app.post("/api/discover/sync-cbas")
async def sync_cbas_full(trade_date: Optional[str] = Body(None, embed=True)):
    """
    Full sync from CBAS Excel: CB list + metadata + stock prices.
    CB prices use INSERT OR IGNORE — preserves any MIS close prices from 13:35 job.
    """
    result = await sync_all_from_cbas(trade_date)
    return result


@app.post("/api/discover/sync-conversion-prices")
async def sync_conv_prices():
    """
    Fetch latest conversion prices from CBAS Excel, update DB, backfill history.
    Usually completes in ~5s.
    """
    result = await sync_conversion_prices_from_cbas()
    return result


@app.post("/api/discover/backfill-history")
async def trigger_backfill(days: int = Body(60, embed=True)):
    """
    Backfill historical CB and stock prices for the past N trading days.
    Fetches from TPEX OTC bond historical endpoint + TWSE/TPEX stock data.
    Takes 1-3 minutes for 60 days. Runs synchronously (await completes when done).
    """
    days = max(1, min(days, 180))
    result = await backfill_history(days)
    return result


@app.post("/api/discover/fetch-date")
async def fetch_one_date(trade_date: str = Body(..., embed=True)):
    """Fetch prices for a single specific historical date."""
    result = await fetch_historical_prices_for_date(trade_date)
    return result


@app.post("/api/prices/fetch")
async def trigger_fetch(trade_date: Optional[str] = Body(None, embed=True)):
    """Fast refresh of prices for known CBs."""
    result = await refresh_all_prices(trade_date)
    return result


# ──────────────────────────────────────────────────────────
# API: Premium table
# ──────────────────────────────────────────────────────────

@app.get("/api/premium/today")
async def get_today_premium(
    trade_date: Optional[str] = None,
    min_pct: Optional[float] = None,
    max_pct: Optional[float] = None,
    search: str = "",
    sort_by: str = "premium_pct",
    asc: bool = True,
    include_no_conv: bool = True,
):
    date = trade_date or last_trading_date()
    valid_sorts = {"premium_pct", "cb_code", "stock_code", "cb_price", "stock_price", "parity", "conversion_price"}
    if sort_by not in valid_sorts:
        sort_by = "premium_pct"
    order = "ASC" if asc else "DESC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # CBs with premium data
        where_ph = ["ph.trade_date=?"]
        params_ph: list = [date]
        if min_pct is not None:
            where_ph.append("ph.premium_pct >= ?"); params_ph.append(min_pct)
        if max_pct is not None:
            where_ph.append("ph.premium_pct <= ?"); params_ph.append(max_pct)
        if search:
            where_ph.append("(cb.cb_code LIKE ? OR cb.cb_name LIKE ? OR cb.stock_code LIKE ? OR cb.stock_name LIKE ?)")
            s = f"%{search}%"; params_ph += [s, s, s, s]

        sql_ph = f"""
            SELECT ph.cb_code, cb.cb_name, cb.stock_code, cb.stock_name, cb.stock_market,
                   ph.cb_price, ph.stock_price, ph.conversion_price,
                   ph.parity, ph.premium_pct, ph.trade_date,
                   cbp.prev_close AS cb_prev_close, cbp.volume AS cb_volume,
                   cb.maturity_date, cb.outstanding_lots, cb.outstanding_pct
            FROM premium_history ph
            JOIN convertible_bonds cb ON ph.cb_code=cb.cb_code
            LEFT JOIN cb_prices cbp ON cbp.cb_code=ph.cb_code AND cbp.trade_date=ph.trade_date
            WHERE {' AND '.join(where_ph)}
            ORDER BY {sort_by} {order}
        """
        cur = await db.execute(sql_ph, params_ph)
        rows_with = [dict(r) for r in await cur.fetchall()]

        # CBs with price but no conversion price (if requested)
        rows_without = []
        if include_no_conv and not min_pct and not max_pct:
            where_no = ["cbp.trade_date=?", "cb.is_active=1",
                        "(cb.conversion_price IS NULL OR cb.conversion_price=0)"]
            params_no: list = [date]
            if search:
                where_no.append("(cb.cb_code LIKE ? OR cb.cb_name LIKE ? OR cb.stock_code LIKE ? OR cb.stock_name LIKE ?)")
                s = f"%{search}%"; params_no += [s, s, s, s]
            sql_no = f"""
                SELECT cb.cb_code, cb.cb_name, cb.stock_code, cb.stock_name, cb.stock_market,
                       cbp.close_price AS cb_price, sp.close_price AS stock_price,
                       cb.conversion_price, NULL AS parity, NULL AS premium_pct, cbp.trade_date,
                       cbp.prev_close AS cb_prev_close, cbp.volume AS cb_volume,
                       cb.maturity_date, cb.outstanding_lots, cb.outstanding_pct
                FROM cb_prices cbp
                JOIN convertible_bonds cb ON cbp.cb_code=cb.cb_code
                LEFT JOIN stock_prices sp ON sp.stock_code=cb.stock_code AND sp.trade_date=cbp.trade_date
                WHERE {' AND '.join(where_no)}
                ORDER BY cb.cb_code ASC
            """
            cur = await db.execute(sql_no, params_no)
            rows_without = [dict(r) for r in await cur.fetchall()]

    def add_computed(r):
        prev = r.get("cb_prev_close")
        price = r.get("cb_price")
        if prev and prev > 0 and price:
            r["cb_change_pct"] = (price - prev) / prev * 100
        else:
            r["cb_change_pct"] = None
        # Distance from par (face value = 100)
        if price:
            r["par_premium_pct"] = (price - 100.0) / 100.0 * 100.0
        else:
            r["par_premium_pct"] = None
        return r

    result = [add_computed(r) for r in rows_with] + [add_computed(r) for r in rows_without]
    return {"date": date, "total": len(result), "with_premium": len(rows_with), "data": result}


@app.get("/api/analysis/cb-overbought")
async def cb_overbought_analysis(days: int = 60):
    """
    找出「股價未創新高，但 CB 價格卻偏高」的異常情況。
    邏輯：
      1. 找過去 N 天中股價最高的那天（股高日）
      2. 取那天的 CB 價格（股高CB價）
      3. 若今日 CB > 股高CB 且 今日股價 < 股高股價 → 異常
    回傳：cb_excess_pct（CB超漲幅）、stock_vs_high_pct（股價距高點）
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # 所有 CB 最新一天的完整資料
        cur = await db.execute("""
            SELECT ph.cb_code, ph.cb_price AS today_cb, ph.stock_price AS today_stock,
                   ph.parity, ph.premium_pct, ph.trade_date,
                   cb.cb_name, cb.stock_code, cb.stock_name, cb.stock_market,
                   cb.conversion_price, cb.maturity_date, cb.outstanding_lots, cb.outstanding_pct
            FROM premium_history ph
            JOIN convertible_bonds cb ON ph.cb_code = cb.cb_code
            WHERE ph.trade_date = (SELECT MAX(trade_date) FROM premium_history WHERE cb_code = ph.cb_code)
              AND cb.is_active = 1
        """)
        latest_map = {r["cb_code"]: dict(r) for r in await cur.fetchall()}

        # 過去 N 天的所有歷史紀錄
        cur = await db.execute("""
            SELECT cb_code, trade_date, cb_price, stock_price
            FROM premium_history
            WHERE trade_date >= ?
            ORDER BY cb_code, trade_date
        """, (cutoff,))
        all_history = await cur.fetchall()

    # 按 CB 分組
    from collections import defaultdict
    history_by_cb: dict = defaultdict(list)
    for row in all_history:
        history_by_cb[row["cb_code"]].append(dict(row))

    results = []
    for cb_code, rows in history_by_cb.items():
        if len(rows) < 3:
            continue
        latest = latest_map.get(cb_code)
        if not latest:
            continue

        today_stock = latest["today_stock"]
        today_cb    = latest["today_cb"]
        today_date  = latest["trade_date"]

        # 排除今天，在歷史資料中找股價最高那天
        hist_rows = [r for r in rows if r["trade_date"] != today_date]
        if not hist_rows:
            continue

        stock_high_row   = max(hist_rows, key=lambda x: x["stock_price"] or 0)
        stock_60d_high   = stock_high_row["stock_price"]
        cb_at_stock_high = stock_high_row["cb_price"]
        stock_high_date  = stock_high_row["trade_date"]

        if not all([stock_60d_high, cb_at_stock_high, today_stock, today_cb,
                    stock_60d_high > 0, cb_at_stock_high > 0]):
            continue

        # 今日股價距 60 日高點的幅度（負 = 低於高點）
        stock_vs_high_pct = (today_stock / stock_60d_high - 1) * 100
        # 今日 CB 相對「股高日 CB 價」的超漲幅度（正 = CB 反而更貴）
        cb_excess_pct = (today_cb / cb_at_stock_high - 1) * 100

        # 條件：股價已從高點回落，但 CB 仍高於股高日 CB 價
        min_stock_drop = -3 if len(rows) >= 20 else -1  # 資料少時放寬條件
        if stock_vs_high_pct < min_stock_drop and cb_excess_pct > 0:
            r = dict(latest)
            r["cb_price"]         = today_cb
            r["stock_price"]      = today_stock
            r["stock_60d_high"]   = round(stock_60d_high, 2)
            r["cb_at_stock_high"] = round(cb_at_stock_high, 2)
            r["stock_high_date"]  = stock_high_date
            r["stock_vs_high_pct"] = round(stock_vs_high_pct, 2)
            r["cb_excess_pct"]    = round(cb_excess_pct, 2)
            r["divergence"]       = round(cb_excess_pct - stock_vs_high_pct, 2)
            results.append(r)

    results.sort(key=lambda x: x["divergence"], reverse=True)
    return {"days": days, "count": len(results), "data": results}


@app.get("/api/cbs/{cb_code}/kline-data")
async def get_kline_data(cb_code: str, days: int = 120):
    """CB close price + stock OHLC for K-line comparison chart."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            "SELECT cb_code, cb_name, stock_code, stock_name, stock_market FROM convertible_bonds WHERE cb_code=?",
            (cb_code,)
        )
        cb = await cur.fetchone()
        if not cb:
            raise HTTPException(status_code=404, detail="CB not found")
        cb = dict(cb)

        cur = await db.execute(
            "SELECT trade_date, close_price FROM cb_prices WHERE cb_code=? AND trade_date>=? ORDER BY trade_date ASC",
            (cb_code, cutoff)
        )
        cb_prices = [{"date": r["trade_date"], "value": r["close_price"]} for r in await cur.fetchall()]

    stock_ohlc = await fetch_stock_ohlc(cb["stock_code"], cb["stock_market"], days)

    return {"cb": cb, "cb_prices": cb_prices, "stock_ohlc": stock_ohlc}


@app.get("/api/premium/{cb_code}/history")
async def get_premium_history(cb_code: str, days: int = 90):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT ph.trade_date, ph.cb_price, ph.stock_price, ph.conversion_price,
                      ph.parity, ph.premium_pct, cbp.volume AS cb_volume
               FROM premium_history ph
               LEFT JOIN cb_prices cbp ON cbp.cb_code=ph.cb_code AND cbp.trade_date=ph.trade_date
               WHERE ph.cb_code=? AND ph.trade_date>=?
               ORDER BY ph.trade_date ASC""",
            (cb_code, cutoff),
        )
        return [row_to_dict(r) for r in await cur.fetchall()]


# ──────────────────────────────────────────────────────────
# API: CB detail & price history
# ──────────────────────────────────────────────────────────

@app.get("/api/cbs/{cb_code}")
async def get_cb_detail(cb_code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM convertible_bonds WHERE cb_code=?", (cb_code,))
        cb = row_to_dict(await cur.fetchone())
        if not cb:
            raise HTTPException(404, "CB not found")
        cur = await db.execute("SELECT * FROM cb_prices WHERE cb_code=? ORDER BY trade_date DESC LIMIT 1", (cb_code,))
        cb["latest_price"] = row_to_dict(await cur.fetchone())
        cur = await db.execute("SELECT * FROM premium_history WHERE cb_code=? ORDER BY trade_date DESC LIMIT 1", (cb_code,))
        cb["latest_premium"] = row_to_dict(await cur.fetchone())
    return cb


@app.get("/api/prices/cb/{cb_code}")
async def get_cb_price_history(cb_code: str, days: int = 90):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM cb_prices WHERE cb_code=? AND trade_date>=? ORDER BY trade_date ASC",
            (cb_code, cutoff),
        )
        return [row_to_dict(r) for r in await cur.fetchall()]


@app.get("/api/prices/stock/{stock_code}")
async def get_stock_price_history(stock_code: str, days: int = 90):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM stock_prices WHERE stock_code=? AND trade_date>=? ORDER BY trade_date ASC",
            (stock_code, cutoff),
        )
        return [row_to_dict(r) for r in await cur.fetchall()]


# ──────────────────────────────────────────────────────────
# API: Conversion price auto-fetch
# ──────────────────────────────────────────────────────────

@app.post("/api/cbs/{cb_code}/fetch-conversion-price")
async def auto_fetch_conversion_price(cb_code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT stock_code FROM convertible_bonds WHERE cb_code=?", (cb_code,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "CB not found")
        stock_code = row["stock_code"]

    price = await fetch_conversion_price_mops(stock_code)
    if price:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE convertible_bonds SET conversion_price=?, updated_at=datetime('now','localtime') WHERE cb_code=?",
                (price, cb_code),
            )
            await db.commit()
        return {"status": "ok", "conversion_price": price}
    return {"status": "not_found", "message": "無法從MOPS取得，請手動輸入"}


# ──────────────────────────────────────────────────────────
# API: Stats & health
# ──────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM convertible_bonds WHERE is_active=1")
        active = (await cur.fetchone())[0]
        cur = await db.execute("SELECT MAX(trade_date) FROM premium_history")
        latest = (await cur.fetchone())[0]
    return {"status": "ok", "active_cbs": active, "latest_data_date": latest,
            "discovery": _discovery_status}


@app.get("/api/stats")
async def get_stats():
    date = last_trading_date()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM convertible_bonds WHERE is_active=1")
        total_cbs = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*),AVG(premium_pct),MIN(premium_pct),MAX(premium_pct) FROM premium_history WHERE trade_date=?",
            (date,),
        )
        row = await cur.fetchone()
        count, avg_pct, min_pct, max_pct = row
        cur = await db.execute(
            "SELECT COUNT(*) FROM premium_history WHERE trade_date=? AND premium_pct < 0", (date,)
        )
        discount_count = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM premium_history WHERE trade_date=? AND premium_pct >= 0", (date,)
        )
        premium_count = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM cb_prices WHERE trade_date=?", (date,)
        )
        price_count = (await cur.fetchone())[0]

    return {
        "trade_date": date,
        "total_active_cbs": total_cbs,
        "cbs_with_prices": price_count,
        "cbs_with_premium": count,
        "avg_premium_pct": round(avg_pct, 2) if avg_pct else None,
        "min_premium_pct": round(min_pct, 2) if min_pct else None,
        "max_premium_pct": round(max_pct, 2) if max_pct else None,
        "discount_count": discount_count,
        "premium_count": premium_count,
        "discovery": _discovery_status,
    }


# ──────────────────────────────────────────────────────────
# Serve frontend (must be last)
# ──────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
