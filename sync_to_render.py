"""
每天自動從 CBAS 抓轉換價 / 流通餘額，推送到 Render。
使用 Windows 工作排程器執行：
  python C:\Users\a4s5d\cb-tracker\sync_to_render.py
"""
import asyncio, io, json, logging
import httpx, openpyxl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

RENDER_URL = "https://cb-tracker-x7wp.onrender.com"
CBAS_URL   = "https://cbas16889.pscnet.com.tw/api/MiDownloadExcel/GetExcel_IssuedCB"
HEADERS    = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
    "Referer":    "https://cbas16889.pscnet.com.tw/marketInfo/issued/",
    "Accept":     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*",
}

# CBAS Excel 欄位（第1列為標題，第2列起為資料）
# Col 1  = CB代號
# Col 6  = 流通餘額(張數)   outstanding_lots
# Col 7  = 餘額比例 0-100   outstanding_pct
# Col 10 = 轉換價格         conversion_price

def _float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None

async def main():
    log.info("=== CB Tracker 同步開始 ===")
    async with httpx.AsyncClient(verify=False, timeout=60, headers=HEADERS) as client:

        # ── 1. 下載 CBAS Excel ───────────────────────────────
        log.info("抓取 CBAS 可轉債資料...")
        r = await client.get(CBAS_URL)
        r.raise_for_status()
        wb = openpyxl.load_workbook(io.BytesIO(r.content), data_only=True)
        ws = wb.active

        prices      = {}   # {cb_code: conversion_price}
        outstanding = {}   # {cb_code: {lots, pct}}

        for row_idx in range(2, ws.max_row + 1):
            cb_code = str(ws.cell(row_idx, 1).value or "").strip()
            if not cb_code:
                continue

            conv  = _float(ws.cell(row_idx, 10).value)
            lots  = _float(ws.cell(row_idx, 6).value)
            pct   = _float(ws.cell(row_idx, 7).value)

            if conv and conv > 0:
                prices[cb_code] = conv

            if lots is not None or pct is not None:
                outstanding[cb_code] = {
                    "lots": int(lots) if lots is not None else None,
                    "pct":  pct,
                }

        log.info(f"取得轉換價 {len(prices)} 筆 / 流通餘額 {len(outstanding)} 筆")

        # ── 2. 推送轉換價 ────────────────────────────────────
        res = await client.post(
            f"{RENDER_URL}/api/cbs/bulk-conversion-price",
            json={"prices": prices},
            timeout=120,
        )
        r1 = res.json()
        ok1 = sum(1 for v in r1.get("results", {}).values() if v.get("ok"))
        log.info(f"轉換價推送：{ok1}/{len(prices)} 筆成功")

        # ── 3. 推送流通餘額（轉換進度）──────────────────────
        res2 = await client.post(
            f"{RENDER_URL}/api/cbs/bulk-outstanding",
            json={"items": outstanding},
            timeout=120,
        )
        r2 = res2.json()
        log.info(f"流通餘額推送：{r2.get('updated', 0)} 筆更新")

        # ── 4. 確認 Render 狀態 ──────────────────────────────
        wake = await client.get(f"{RENDER_URL}/api/stats")
        stats = wake.json()
        log.info(
            f"Render 狀態 — CB總數:{stats.get('total_active_cbs')} "
            f"有溢折價:{stats.get('cbs_with_premium')} "
            f"平均溢價率:{stats.get('avg_premium_pct')}%"
        )

    log.info("=== 同步完成 ===")

if __name__ == "__main__":
    asyncio.run(main())
