"""核心业务：CSV 解析、批次入库（同事务）、增量回查、清单与放行状态。"""
import csv
import io
import json
from datetime import datetime, timezone

KIND_ARRIVALS = "arrivals"
KIND_PURCHASE_LINES = "purchase_lines"
KIND_CODE_USAGES = "code_usages"

KIND_LABELS = {
    KIND_ARRIVALS: "到货",
    KIND_PURCHASE_LINES: "采购行",
    KIND_CODE_USAGES: "用码记录",
}

REASON_NO_PO_LINE = "NO_PO_LINE"            # 采购行缺失
REASON_BARCODE_MISMATCH = "BARCODE_MISMATCH"  # 条码不符
REASON_NO_OWNER = "NO_OWNER"                # 到货时该码无归属
REASON_MULTI_PRODUCT = "MULTI_PRODUCT"      # 一码对应多商品

REASON_LABELS = {
    REASON_NO_PO_LINE: "采购行缺失",
    REASON_BARCODE_MISMATCH: "条码不符",
    REASON_NO_OWNER: "到货时该码无归属",
    REASON_MULTI_PRODUCT: "一码对应多商品",
}

SIDE_PO = "po"
SIDE_BARCODE = "barcode"
SIDE_LABELS = {SIDE_PO: "采购行号", SIDE_BARCODE: "条码"}

MAX_ERRORS = 50

TABLE_OF = {
    KIND_ARRIVALS: "arrivals",
    KIND_PURCHASE_LINES: "purchase_lines",
    KIND_CODE_USAGES: "code_usages",
}

BUSINESS_FIELDS = {
    KIND_ARRIVALS: ["po_line_no", "barcode", "ts"],
    KIND_PURCHASE_LINES: ["barcode"],
    KIND_CODE_USAGES: ["barcode", "product", "start_ts", "end_ts"],
}

# 中英文表头都认；比较前统一 strip + lower。
FIELD_ALIASES = {
    KIND_ARRIVALS: {
        "id": ["id", "编号"],
        "po_line_no": ["po_line_no", "po_line", "采购行号"],
        "barcode": ["barcode", "扫码条码", "条码"],
        "ts": ["ts_utc", "utc时刻", "到货时刻", "时刻"],
    },
    KIND_PURCHASE_LINES: {
        "id": ["id", "编号"],
        "barcode": ["barcode", "约定条码", "条码"],
    },
    KIND_CODE_USAGES: {
        "id": ["id", "编号"],
        "barcode": ["barcode", "条码"],
        "product": ["product", "商品"],
        "start_ts": ["start_utc", "utc开始", "开始时刻", "开始"],
        "end_ts": ["end_utc", "utc结束", "结束时刻", "结束"],
    },
}


class BatchError(Exception):
    """整批失败：errors 为行级错误列表；数据库保持上次结果。"""

    def __init__(self, errors):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


def parse_ts(raw):
    """ISO8601 → UTC epoch 秒；无时区按 UTC 处理。"""
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def iso_utc(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _norm(h):
    return (h or "").strip().lower()


def _build_row(kind, cell):
    rid = cell("id")
    if not rid:
        raise ValueError("编号为空")
    if kind == KIND_ARRIVALS:
        po, bc, ts_raw = cell("po_line_no"), cell("barcode"), cell("ts")
        if not po:
            raise ValueError("采购行号为空")
        if not bc:
            raise ValueError("扫码条码为空")
        if not ts_raw:
            raise ValueError("UTC时刻为空")
        try:
            ts = parse_ts(ts_raw)
        except ValueError:
            raise ValueError(f"UTC时刻无法解析：{ts_raw}")
        return {"id": rid, "po_line_no": po, "barcode": bc, "ts": ts}
    if kind == KIND_PURCHASE_LINES:
        bc = cell("barcode")
        if not bc:
            raise ValueError("约定条码为空")
        return {"id": rid, "barcode": bc}
    bc, prod = cell("barcode"), cell("product")
    s_raw, e_raw = cell("start_ts"), cell("end_ts")
    if not bc:
        raise ValueError("条码为空")
    if not prod:
        raise ValueError("商品为空")
    try:
        s = parse_ts(s_raw)
    except ValueError:
        raise ValueError(f"UTC起点无法解析：{s_raw}")
    try:
        e = parse_ts(e_raw)
    except ValueError:
        raise ValueError(f"UTC终点无法解析：{e_raw}")
    if not s < e:
        raise ValueError("用码期起点必须早于终点")
    return {"id": rid, "barcode": bc, "product": prod, "start_ts": s, "end_ts": e}


def parse_csv(kind, raw_bytes):
    """返回 (rows, errors)。同号文件内后出现的覆盖先出现的（以本次上传为准）。"""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], ["文件不是有效的 UTF-8 编码"]
    lines = [
        r for r in csv.reader(io.StringIO(text))
        if any((c or "").strip() for c in r)
    ]
    if not lines:
        return [], []
    header = [_norm(h) for h in lines[0]]
    col_of, missing = {}, []
    for field, names in FIELD_ALIASES[kind].items():
        wanted = {_norm(n) for n in names}
        hit = next((i for i, h in enumerate(header) if h in wanted), None)
        if hit is None:
            missing.append(names[-1])
        else:
            col_of[field] = hit
    if missing:
        return [], [f"缺少列：{'、'.join(missing)}"]

    errors, by_id, order = [], {}, []

    def make_cell(row):
        def cell(field):
            i = col_of[field]
            return row[i].strip() if i < len(row) else ""
        return cell

    for lineno, row in enumerate(lines[1:], start=2):
        try:
            rec = _build_row(kind, make_cell(row))
        except ValueError as e:
            errors.append(f"第{lineno}行：{e}")
            continue
        if rec["id"] not in by_id:
            order.append(rec["id"])
        by_id[rec["id"]] = rec
    if len(errors) > MAX_ERRORS:
        errors = errors[:MAX_ERRORS] + [
            f"……共 {len(errors)} 处错误，仅显示前 {MAX_ERRORS} 条"
        ]
    return [by_id[i] for i in order], errors


# ---------- 批次入库 ----------

def _fetch(conn, kind, rid):
    return conn.execute(
        f"SELECT * FROM {TABLE_OF[kind]} WHERE id = ?", (rid,)
    ).fetchone()


def _as_dict(kind, row):
    if row is None:
        return None
    d = {"id": row["id"]}
    for f in BUSINESS_FIELDS[kind]:
        d[f] = row[f]
    return d


def _upsert(conn, kind, row, batch_id):
    if kind == KIND_ARRIVALS:
        conn.execute(
            "INSERT INTO arrivals(id, po_line_no, barcode, ts, batch_id)"
            " VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
            " po_line_no=excluded.po_line_no, barcode=excluded.barcode,"
            " ts=excluded.ts, batch_id=excluded.batch_id",
            (row["id"], row["po_line_no"], row["barcode"], row["ts"], batch_id),
        )
    elif kind == KIND_PURCHASE_LINES:
        conn.execute(
            "INSERT INTO purchase_lines(id, barcode, batch_id) VALUES (?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " barcode=excluded.barcode, batch_id=excluded.batch_id",
            (row["id"], row["barcode"], batch_id),
        )
    else:
        conn.execute(
            "INSERT INTO code_usages(id, barcode, product, start_ts, end_ts, batch_id)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
            " barcode=excluded.barcode, product=excluded.product,"
            " start_ts=excluded.start_ts, end_ts=excluded.end_ts,"
            " batch_id=excluded.batch_id",
            (row["id"], row["barcode"], row["product"],
             row["start_ts"], row["end_ts"], batch_id),
        )


def _collect_affected(conn, kind, old, new, affected):
    """只找改动前后关联的到货：
    - 到货自身：重查本笔；
    - 采购行 X 变动：所有 po_line_no = X 的到货；
    - 用码变动：条码 ∈ {旧, 新} 且到货时刻落在旧/新用码期内的到货。
    """
    if kind == KIND_ARRIVALS:
        affected.add(new["id"])
    elif kind == KIND_PURCHASE_LINES:
        for r in conn.execute(
            "SELECT id FROM arrivals WHERE po_line_no = ?", (new["id"],)
        ):
            affected.add(r["id"])
    else:
        conds, params = [], []
        for row in (old, new):
            if row is None:
                continue
            conds.append("(barcode = ? AND ts >= ? AND ts < ?)")
            params.extend([row["barcode"], row["start_ts"], row["end_ts"]])
        if conds:
            sql = "SELECT id FROM arrivals WHERE " + " OR ".join(conds)
            for r in conn.execute(sql, params):
                affected.add(r["id"])


def _reevaluate(conn, arrival_id, batch_id):
    """重查一笔到货的全部阻断原因，并同事务刷新放行状态。"""
    arr = conn.execute(
        "SELECT * FROM arrivals WHERE id = ?", (arrival_id,)
    ).fetchone()
    conn.execute("DELETE FROM blocks WHERE arrival_id = ?", (arrival_id,))
    if arr is None:
        conn.execute(
            "DELETE FROM arrival_status WHERE arrival_id = ?", (arrival_id,)
        )
        return

    blocks = []
    pl = conn.execute(
        "SELECT * FROM purchase_lines WHERE id = ?", (arr["po_line_no"],)
    ).fetchone()
    if pl is None:
        blocks.append((
            REASON_NO_PO_LINE, SIDE_PO, arr["po_line_no"],
            f"采购行 {arr['po_line_no']} 不存在，需采购员补录",
        ))
    elif pl["barcode"] != arr["barcode"]:
        detail = f"约定条码 {pl['barcode']}，实扫条码 {arr['barcode']}"
        # 一笔同时挂在两侧：采购行号侧 + 条码侧
        blocks.append((REASON_BARCODE_MISMATCH, SIDE_PO, arr["po_line_no"], detail))
        blocks.append((REASON_BARCODE_MISMATCH, SIDE_BARCODE, arr["barcode"], detail))

    products = [
        r["product"] for r in conn.execute(
            "SELECT DISTINCT product FROM code_usages"
            " WHERE barcode = ? AND start_ts <= ? AND end_ts > ?",
            (arr["barcode"], arr["ts"], arr["ts"]),
        )
    ]
    if not products:
        blocks.append((
            REASON_NO_OWNER, SIDE_BARCODE, arr["barcode"],
            f"到货时刻条码 {arr['barcode']} 无任何用码记录，需档案员补录",
        ))
    elif len(products) > 1:
        blocks.append((
            REASON_MULTI_PRODUCT, SIDE_BARCODE, arr["barcode"],
            "到货时刻该码对应多种商品：" + "、".join(sorted(products)),
        ))

    conn.executemany(
        "INSERT INTO blocks(arrival_id, reason, side, item_key, detail)"
        " VALUES (?,?,?,?,?)",
        [(arrival_id, reason, side, key, detail) for reason, side, key, detail in blocks],
    )
    conn.execute(
        "INSERT INTO arrival_status(arrival_id, released, checked_batch)"
        " VALUES (?,?,?) ON CONFLICT(arrival_id) DO UPDATE SET"
        " released=excluded.released, checked_batch=excluded.checked_batch",
        (arrival_id, 0 if blocks else 1, batch_id),
    )


def apply_batch(conn, uploads):
    """uploads: {kind: 文件对象}。成功返回批次摘要；失败抛 BatchError，库保持原样。

    底表 upsert、阻断清单、放行状态在同一事务生效；任何一步失败整体回滚。
    """
    parsed, errors = {}, []
    for kind, storage in uploads.items():
        if storage is None:
            continue
        rows, errs = parse_csv(kind, storage.read())
        if errs:
            errors.extend(f"{KIND_LABELS[kind]}CSV：{e}" for e in errs)
        elif rows:
            parsed[kind] = rows
    if errors:
        raise BatchError(errors)
    if not parsed:
        raise BatchError(["没有可入库的数据行"])

    conn.execute("BEGIN IMMEDIATE")
    try:
        batch_id = conn.execute(
            "INSERT INTO batches(uploaded_at) VALUES (?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
        ).lastrowid

        affected, stats = set(), {}
        for kind, rows in parsed.items():
            inserted = updated = unchanged = 0
            for row in rows:
                old = _as_dict(kind, _fetch(conn, kind, row["id"]))
                if old is not None and all(
                    old[f] == row[f] for f in BUSINESS_FIELDS[kind]
                ):
                    unchanged += 1
                    continue
                _upsert(conn, kind, row, batch_id)
                _collect_affected(conn, kind, old, row, affected)
                if old is None:
                    inserted += 1
                else:
                    updated += 1
            stats[kind] = {
                "rows": len(rows), "inserted": inserted,
                "updated": updated, "unchanged": unchanged,
            }

        for arrival_id in affected:
            _reevaluate(conn, arrival_id, batch_id)

        summary = {
            "batch_id": batch_id,
            "affected_arrivals": len(affected),
            "stats": stats,
        }
        conn.execute(
            "UPDATE batches SET stats = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), batch_id),
        )
        conn.execute("COMMIT")
        return summary
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- 查询 ----------

def list_items(conn, side):
    """待修清单：直接对阻断明细表取受阻笔数，不从底表重算。"""
    return conn.execute(
        "SELECT reason, item_key, COUNT(DISTINCT arrival_id) AS blocked"
        " FROM blocks WHERE side = ?"
        " GROUP BY reason, item_key ORDER BY reason, item_key",
        (side,),
    ).fetchall()


def item_details(conn, side, reason, key):
    return conn.execute(
        "SELECT b.arrival_id, b.detail, a.po_line_no, a.barcode, a.ts"
        " FROM blocks b JOIN arrivals a ON a.id = b.arrival_id"
        " WHERE b.side = ? AND b.reason = ? AND b.item_key = ?"
        " ORDER BY b.arrival_id",
        (side, reason, key),
    ).fetchall()


def arrival_view(conn, arrival_id):
    arr = conn.execute(
        "SELECT * FROM arrivals WHERE id = ?", (arrival_id,)
    ).fetchone()
    if arr is None:
        return None
    status = conn.execute(
        "SELECT * FROM arrival_status WHERE arrival_id = ?", (arrival_id,)
    ).fetchone()
    blocks = conn.execute(
        "SELECT reason, side, item_key, detail FROM blocks"
        " WHERE arrival_id = ? ORDER BY reason, side",
        (arrival_id,),
    ).fetchall()
    return {"arrival": arr, "status": status, "blocks": blocks}


def overview(conn):
    stats = conn.execute(
        "SELECT (SELECT COUNT(*) FROM arrivals) AS arrivals,"
        " (SELECT COUNT(*) FROM arrival_status WHERE released = 1) AS released,"
        " (SELECT COUNT(*) FROM arrival_status WHERE released = 0) AS blocked,"
        " (SELECT COUNT(*) FROM purchase_lines) AS purchase_lines,"
        " (SELECT COUNT(*) FROM code_usages) AS code_usages"
    ).fetchone()
    batches = conn.execute(
        "SELECT * FROM batches ORDER BY id DESC LIMIT 10"
    ).fetchall()
    return stats, batches
