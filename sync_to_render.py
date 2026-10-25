# 每天自動從 CBAS 抓轉換價 / 流通餘額：
#   1. 存到 data/cbas_cache.json 並推上 GitHub（Render 啟動時自動讀取）
#   2. 直接推送到 Render（即時生效）
#
# Windows 工作排程器設定：
#   程式：python
#   引數：C:/Users/a4s5d/cb-tracker/sync_to_render.py
#   起始位置：C:/Users/a4s5d/cb-tracker
import asyncio, io, json, logging, subprocess
from datetime import datetime, timezone, timedelta
import httpx, openpyxl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

RENDER_URL   = "https://cb-tracker-x7wp.onrender.com"
CBAS_URL     = "https://cbas16889.pscnet.com.tw/api/MiDownloadExcel/GetExcel_IssuedCB"
CACHE_PATH   = "data/cbas_cache.json"
CB_HIST_PATH = "data/cb_prices_history.json"
REPO_DIR     = "C:/Users/a4s5d/cb-tracker"
HEADERS      = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
    "Referer":    "https://cbas16889.pscnet.com.tw/marketInfo/issued/",
    "Accept":     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*",
}

def _f(v):
    try: return float(str(v).replace(",", "").strip())
    except: return None

async def main():
    log.info("=== CB Tracker 同步開始 ===")

    # ── 1. 下載 CBAS Excel ──────────────────────────────────
    async with httpx.AsyncClient(verify=False, timeout=60, headers=HEADERS) as client:
        log.info("抓取 CBAS 可轉債資料...")
        r = await client.get(CBAS_URL)
        r.raise_for_status()
        wb = openpyxl.load_workbook(io.BytesIO(r.content), data_only=True)
        ws = wb.active

        prices, outstanding, cb_prices_today = {}, {}, {}
        for i in range(2, ws.max_row + 1):
            cb = str(ws.cell(i, 1).value or "").strip()
            if not cb:
                continue
            conv     = _f(ws.cell(i, 10).value)
            lots     = _f(ws.cell(i, 6).value)
            pct      = _f(ws.cell(i, 7).value)
            cb_price = _f(ws.cell(i, 14).value)   # CB 市價
            if conv and conv > 0:
                prices[cb] = conv
            if lots is not None or pct is not None:
                outstanding[cb] = {
                    "lots": int(lots) if lots is not None else None,
                    "pct":  pct,
                }
            if cb_price and cb_price > 0:
                cb_prices_today[cb] = cb_price

        # 取最近交易日（週末往回推）
        d = datetime.now()
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        trade_date = d.strftime("%Y-%m-%d")

        log.info(f"CBAS: 轉換價 {len(prices)} 筆 / 流通餘額 {len(outstanding)} 筆 / CB價格 {len(cb_prices_today)} 筆 ({trade_date})")

        # ── 2. 存 JSON cache + CB 價格歷史 並 push 到 GitHub ──
        cache = {
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "prices":      prices,
            "outstanding": outstanding,
        }
        with open(f"{REPO_DIR}/{CACHE_PATH}", "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        log.info(f"已儲存 {CACHE_PATH}")

        # 讀取舊的 CB 價格歷史，追加今天，保留 90 天
        cb_hist_file = f"{REPO_DIR}/{CB_HIST_PATH}"
        try:
            with open(cb_hist_file, "r", encoding="utf-8") as f:
                cb_prices_history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            cb_prices_history = {}
        cb_prices_history[trade_date] = cb_prices_today
        cutoff = (datetime.now() - timedelta(days=92)).strftime("%Y-%m-%d")
        cb_prices_history = {k: v for k, v in cb_prices_history.items() if k >= cutoff}
        with open(cb_hist_file, "w", encoding="utf-8") as f:
            json.dump(cb_prices_history, f, ensure_ascii=False, indent=2)
        log.info(f"已儲存 {CB_HIST_PATH}（共 {len(cb_prices_history)} 天）")

        subprocess.run(["git", "-C", REPO_DIR, "add", CACHE_PATH, CB_HIST_PATH], capture_output=True)
        result = subprocess.run(
            ["git", "-C", REPO_DIR, "commit", "-m",
             f"chore: update CBAS cache + CB prices {datetime.now().strftime('%Y-%m-%d')}"],
            capture_output=True, text=True
        )
        if "nothing to commit" in result.stdout:
            log.info("CBAS cache 無變化，不需 push")
        else:
            push = subprocess.run(
                ["git", "-C", REPO_DIR, "push", "origin", "master"],
                capture_output=True, text=True
            )
            log.info(f"GitHub push: {push.returncode == 0 and 'OK' or push.stderr[:80]}")

        # ── 3. 直接推送到 Render（即時生效）────────────────
        log.info("推送到 Render...")

        # 等 Render 醒來（最多 90 秒）
        for attempt in range(9):
            try:
                probe = await client.get(f"{RENDER_URL}/api/stats", timeout=15)
                if probe.status_code == 200:
                    break
            except Exception:
                pass
            log.info(f"等待 Render 回應... ({attempt+1}/9)")
            await asyncio.sleep(10)

        # 等 Discovery 完成
        for attempt in range(12):
            try:
                st = (await client.get(f"{RENDER_URL}/api/discover/status", timeout=10)).json()
                if not st.get("running"):
                    break
                log.info(f"Discovery 進行中 {st.get('progress')}%... ({attempt+1}/12)")
            except Exception:
                pass
            await asyncio.sleep(10)

        # 推轉換價
        r1 = await client.post(
            f"{RENDER_URL}/api/cbs/bulk-conversion-price",
            json={"prices": prices}, timeout=120,
        )
        ok1 = sum(1 for v in r1.json().get("results", {}).values() if v.get("ok"))
        log.info(f"轉換價推送：{ok1}/{len(prices)} 筆成功")

        # 推流通餘額
        r2 = await client.post(
            f"{RENDER_URL}/api/cbs/bulk-outstanding",
            json={"items": outstanding}, timeout=120,
        )
        log.info(f"流通餘額推送：{r2.json().get('updated', 0)} 筆更新")

        # ── 4. 觸發 backfill 並等待完成，再匯出溢折價歷史 ──────
        log.info("觸發 Render 補充近期歷史資料...")
        await client.post(f"{RENDER_URL}/api/discover/backfill-history",
                          json={"days": 5}, timeout=30)

        # 等 backfill 完成（最多 10 分鐘）
        for attempt in range(60):
            try:
                st = (await client.get(f"{RENDER_URL}/api/discover/backfill-status", timeout=10)).json()
                if not st.get("running"):
                    log.info(f"Backfill 完成：{st.get('message')}")
                    break
                log.info(f"Backfill 進行中 {st.get('progress')}%... ({attempt+1}/60)")
            except Exception:
                pass
            await asyncio.sleep(10)

        # 匯出溢折價歷史並存到 GitHub
        log.info("匯出溢折價歷史...")
        r3 = await client.get(f"{RENDER_URL}/api/premium-history/export?days=90", timeout=120)
        records = r3.json()
        ph_path = "data/premium_history.json"
        with open(f"{REPO_DIR}/{ph_path}", "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        log.info(f"已儲存 {len(records)} 筆溢折價歷史到 {ph_path}")

        subprocess.run(["git", "-C", REPO_DIR, "add", ph_path], capture_output=True)
        result_ph = subprocess.run(
            ["git", "-C", REPO_DIR, "commit", "-m",
             f"chore: update premium history {datetime.now().strftime('%Y-%m-%d')}"],
            capture_output=True, text=True
        )
        if "nothing to commit" in result_ph.stdout:
            log.info("Premium history 無變化，不需 push")
        else:
            push_ph = subprocess.run(
                ["git", "-C", REPO_DIR, "push", "origin", "master"],
                capture_output=True, text=True
            )
            log.info(f"GitHub push (premium history): {push_ph.returncode == 0 and 'OK' or push_ph.stderr[:80]}")

        # 確認最終狀態
        stats = (await client.get(f"{RENDER_URL}/api/stats")).json()
        log.info(
            f"完成 — CB總數:{stats.get('total_active_cbs')} "
            f"有溢折價:{stats.get('cbs_with_premium')} "
            f"平均溢價率:{stats.get('avg_premium_pct')}%"
        )

    log.info("=== 同步完成 ===")

if __name__ == "__main__":
    asyncio.run(main())
