import aiosqlite
from backend.config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS convertible_bonds (
    cb_code          TEXT PRIMARY KEY,
    cb_name          TEXT NOT NULL,
    cb_full_name     TEXT,
    stock_code       TEXT NOT NULL,
    stock_name       TEXT,
    stock_market     TEXT DEFAULT 'TWSE',
    conversion_price REAL,
    face_value       REAL DEFAULT 100.0,
    maturity_date    TEXT,
    issue_size       REAL,
    is_active        INTEGER DEFAULT 1,
    notes            TEXT,
    created_at       TEXT DEFAULT (datetime('now','localtime')),
    updated_at       TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS cb_prices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cb_code      TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    open_price   REAL,
    high_price   REAL,
    low_price    REAL,
    close_price  REAL NOT NULL,
    prev_close   REAL,
    volume       INTEGER DEFAULT 0,
    source       TEXT DEFAULT 'mis',
    fetched_at   TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(cb_code, trade_date)
);

CREATE TABLE IF NOT EXISTS stock_prices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code   TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    close_price  REAL NOT NULL,
    open_price   REAL,
    high_price   REAL,
    low_price    REAL,
    volume       INTEGER,
    source       TEXT DEFAULT 'twse',
    fetched_at   TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(stock_code, trade_date)
);

CREATE TABLE IF NOT EXISTS premium_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    cb_code          TEXT NOT NULL,
    trade_date       TEXT NOT NULL,
    cb_price         REAL NOT NULL,
    stock_price      REAL NOT NULL,
    conversion_price REAL NOT NULL,
    parity           REAL NOT NULL,
    premium_pct      REAL NOT NULL,
    UNIQUE(cb_code, trade_date)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT NOT NULL,
    trade_date  TEXT,
    status      TEXT NOT NULL,
    records     INTEGER DEFAULT 0,
    message     TEXT,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_cb_prices_date   ON cb_prices(trade_date);
CREATE INDEX IF NOT EXISTS idx_cb_prices_code   ON cb_prices(cb_code);
CREATE INDEX IF NOT EXISTS idx_stock_prices_date ON stock_prices(trade_date);
CREATE INDEX IF NOT EXISTS idx_stock_prices_code ON stock_prices(stock_code);
CREATE INDEX IF NOT EXISTS idx_premium_date     ON premium_history(trade_date);
CREATE INDEX IF NOT EXISTS idx_premium_code     ON premium_history(cb_code);
"""


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def log_fetch(task: str, trade_date: str, status: str, records: int = 0, message: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO fetch_log (task, trade_date, status, records, message) VALUES (?,?,?,?,?)",
            (task, trade_date, status, records, message),
        )
        await db.commit()
