import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).parent.parent
# DATA_DIR: use env var for cloud deployment (e.g., Railway volume at /data)
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "cb_tracker.db"
FRONTEND_DIR = BASE_DIR / "frontend"

# HTTP Headers (simulate browser to avoid blocks)
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept": "application/json, text/html, */*;q=0.8",
    "Referer": "https://mis.twse.com.tw/",
}

# External API URLs
MIS_TWSE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
TWSE_STOCK_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_STOCK_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TWSE_AUCTION_URL = "https://www.twse.com.tw/rwd/zh/announcement/auction"
TWSE_COMPANY_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_COMPANY_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
MOPS_CB_URL = "https://mops.twse.com.tw/mops/web/ajax_t158sb05"

# CBAS (PSC) issued CB data — public Excel download, no auth required
CBAS_ISSUED_URL = "https://cbas16889.pscnet.com.tw/api/MiDownloadExcel/GetExcel_IssuedCB"
CBAS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://cbas16889.pscnet.com.tw/marketInfo/issued/",
    "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*",
}

FETCH_TIMEOUT = 20
RATE_LIMIT_DELAY = 0.4  # seconds between batch requests
CB_BATCH_SIZE = 20       # max CB codes per mis.twse request
