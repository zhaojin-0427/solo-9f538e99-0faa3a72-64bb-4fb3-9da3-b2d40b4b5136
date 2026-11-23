import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  uploaded_at   TEXT NOT NULL,
  stats         TEXT NOT NULL DEFAULT '{}'
);

-- 底表：到货。同类同号以本次上传为准（按 id upsert）。
CREATE TABLE IF NOT EXISTS arrivals (
  id          TEXT PRIMARY KEY,
  po_line_no  TEXT NOT NULL,
  barcode     TEXT NOT NULL,
  ts          INTEGER NOT NULL,          -- UTC  epoch 秒
  batch_id    INTEGER NOT NULL REFERENCES batches(id)
);
CREATE INDEX IF NOT EXISTS idx_arrivals_po ON arrivals(po_line_no);
CREATE INDEX IF NOT EXISTS idx_arrivals_barcode_ts ON arrivals(barcode, ts);

-- 底表：采购行。
CREATE TABLE IF NOT EXISTS purchase_lines (
  id        TEXT PRIMARY KEY,
  barcode   TEXT NOT NULL,               -- 约定条码
  batch_id  INTEGER NOT NULL REFERENCES batches(id)
);

-- 底表：用码记录。用码期含起点、不含终点 [start_ts, end_ts)。
CREATE TABLE IF NOT EXISTS code_usages (
  id        TEXT PRIMARY KEY,
  barcode   TEXT NOT NULL,
  product   TEXT NOT NULL,
  start_ts  INTEGER NOT NULL,
  end_ts    INTEGER NOT NULL,
  batch_id  INTEGER NOT NULL REFERENCES batches(id)
);
CREATE INDEX IF NOT EXISTS idx_usage_barcode_range
  ON code_usages(barcode, start_ts, end_ts);

-- 放行状态：随批次同事务更新；checked_batch 记录本笔最近一次被回查的批次。
CREATE TABLE IF NOT EXISTS arrival_status (
  arrival_id     TEXT PRIMARY KEY REFERENCES arrivals(id),
  released       INTEGER NOT NULL,       -- 1 放行 / 0 受阻
  checked_batch  INTEGER NOT NULL
);

-- 阻断明细：清单直接从这里 GROUP BY 取受阻笔数。
-- 一笔可同时挂在两侧（如条码不符：side=po 挂采购行号，side=barcode 挂条码）。
CREATE TABLE IF NOT EXISTS blocks (
  arrival_id  TEXT NOT NULL REFERENCES arrivals(id),
  reason      TEXT NOT NULL,             -- NO_PO_LINE / BARCODE_MISMATCH / NO_OWNER / MULTI_PRODUCT
  side        TEXT NOT NULL,             -- po / barcode
  item_key    TEXT NOT NULL,             -- 采购行号或条码
  detail      TEXT NOT NULL,
  PRIMARY KEY (arrival_id, reason, side, item_key)
);
CREATE INDEX IF NOT EXISTS idx_blocks_list ON blocks(side, reason, item_key);
"""


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"], isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


def close_db(_=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.commit()
    conn.close()
