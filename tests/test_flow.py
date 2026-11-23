"""端到端自测：阻断原因、双侧清单、补档放行、更正退回、失败回滚、增量回查。"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})
    with app.test_client() as c:
        yield c


def _csv(text):
    return io.BytesIO(text.strip().encode("utf-8"))


def upload(client, arrivals=None, purchase_lines=None, code_usages=None):
    data = {}
    if arrivals is not None:
        data["arrivals"] = (_csv(arrivals), "arrivals.csv")
    if purchase_lines is not None:
        data["purchase_lines"] = (_csv(purchase_lines), "purchase_lines.csv")
    if code_usages is not None:
        data["code_usages"] = (_csv(code_usages), "code_usages.csv")
    return client.post("/api/upload", data=data, content_type="multipart/form-data")


def items(client):
    return client.get("/api/items").get_json()


def arrival(client, aid):
    return client.get(f"/api/arrival/{aid}")


PO_CSV = """编号,约定条码
PL-1,6900001
PL-2,6900002
"""

USAGE_CSV = """编号,条码,商品,UTC开始,UTC结束
U1,6900001,苹果,2026-11-01T00:00:00Z,2026-12-01T00:00:00Z
U2,6900002,香蕉,2026-11-01T00:00:00Z,2026-12-01T00:00:00Z
U3,6900003,橙子,2026-11-01T00:00:00Z,2026-12-01T00:00:00Z
U4,6900003,柚子,2026-11-15T00:00:00Z,2026-12-15T00:00:00Z
"""

ARRIVALS_CSV = """编号,采购行号,扫码条码,UTC时刻
A1,PL-1,6900001,2026-11-10T00:00:00Z
A2,PL-9,6900001,2026-11-10T00:00:00Z
A3,PL-2,6900001,2026-11-10T00:00:00Z
A4,PL-1,6900004,2026-11-10T00:00:00Z
A5,PL-2,6900002,2026-11-20T00:00:00Z
A6,PL-1,6900003,2026-11-20T00:00:00Z
A7,PL-1,6900003,2026-11-05T00:00:00Z
"""


def seed(client):
    r = upload(client, arrivals=ARRIVALS_CSV,
               purchase_lines=PO_CSV, code_usages=USAGE_CSV)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_block_reasons_and_two_sided_lists(client):
    seed(client)
    it = items(client)
    po = {(x["reason"], x["key"]): x["blocked"] for x in it["po"]}
    bc = {(x["reason"], x["key"]): x["blocked"] for x in it["barcode"]}
    assert po == {
        ("NO_PO_LINE", "PL-9"): 1,          # A2
        ("BARCODE_MISMATCH", "PL-2"): 1,    # A3
        ("BARCODE_MISMATCH", "PL-1"): 3,    # A4、A6、A7
    }
    assert bc == {
        ("BARCODE_MISMATCH", "6900001"): 1,  # A3（与采购行侧同一笔）
        ("BARCODE_MISMATCH", "6900004"): 1,  # A4
        ("NO_OWNER", "6900004"): 1,          # A4
        ("BARCODE_MISMATCH", "6900003"): 2,  # A6、A7
        ("MULTI_PRODUCT", "6900003"): 1,     # A6（橙子+柚子重叠期）
    }
    # 一笔同时挂在两边：A3 既在采购行 PL-2 下，也在条码 6900001 下
    a3 = arrival(client, "A3").get_json()
    sides = {(b["reason"], b["side"], b["key"]) for b in a3["blocks"]}
    assert ("BARCODE_MISMATCH", "po", "PL-2") in sides
    assert ("BARCODE_MISMATCH", "barcode", "6900001") in sides
    # 放行状态：全部阻断解除才放行
    assert arrival(client, "A1").get_json()["released"]
    assert arrival(client, "A5").get_json()["released"]
    for aid in ("A2", "A3", "A4", "A6", "A7"):
        assert not arrival(client, aid).get_json()["released"], aid
    # A6 同时有多原因
    reasons = {b["reason"] for b in arrival(client, "A6").get_json()["blocks"]}
    assert reasons == {"BARCODE_MISMATCH", "MULTI_PRODUCT"}


def test_backfill_missing_po_line_releases(client):
    """补录采购行后：阻断解除、清单两侧同步更新、到货放行。"""
    seed(client)
    assert not arrival(client, "A2").get_json()["released"]
    r = upload(client, purchase_lines="编号,约定条码\nPL-9,6900001\n")
    assert r.status_code == 200
    a2 = arrival(client, "A2").get_json()
    assert a2["released"] and a2["blocks"] == []
    assert all(x["key"] != "PL-9" for x in items(client)["po"])


def test_backfill_usage_record_clears_no_owner(client):
    """档案员补录用码记录：无归属解除，但条码不符仍在 → 仍受阻。"""
    seed(client)
    assert "NO_OWNER" in {
        b["reason"] for b in arrival(client, "A4").get_json()["blocks"]
    }
    upload(client, code_usages=(
        "编号,条码,商品,UTC开始,UTC结束\n"
        "U5,6900004,芒果,2026-11-01T00:00:00Z,2026-12-01T00:00:00Z\n"
    ))
    a4 = arrival(client, "A4").get_json()
    reasons = {b["reason"] for b in a4["blocks"]}
    assert reasons == {"BARCODE_MISMATCH"}
    assert not a4["released"]
    bc = {(x["reason"], x["key"]) for x in items(client)["barcode"]}
    assert ("NO_OWNER", "6900004") not in bc
    assert ("BARCODE_MISMATCH", "6900004") in bc


def test_correction_can_reverse_released(client):
    """更正让已放行的记录退回；再更正回来重新放行。"""
    seed(client)
    assert arrival(client, "A1").get_json()["released"]
    # U1 用码期缩短 → A1 到货时刻不再被覆盖 → 退回为受阻
    upload(client, code_usages=(
        "编号,条码,商品,UTC开始,UTC结束\n"
        "U1,6900001,苹果,2026-11-01T00:00:00Z,2026-11-05T00:00:00Z\n"
    ))
    a1 = arrival(client, "A1").get_json()
    assert not a1["released"]
    assert {b["reason"] for b in a1["blocks"]} == {"NO_OWNER"}
    # 更正回来 → 重新放行
    upload(client, code_usages=(
        "编号,条码,商品,UTC开始,UTC结束\n"
        "U1,6900001,苹果,2026-11-01T00:00:00Z,2026-12-01T00:00:00Z\n"
    ))
    assert arrival(client, "A1").get_json()["released"]


def test_usage_window_start_inclusive_end_exclusive(client):
    """用码期含起点、不含终点。"""
    upload(
        client,
        purchase_lines="编号,约定条码\nPL-5,6900005\n",
        code_usages=(
            "编号,条码,商品,UTC开始,UTC结束\n"
            "U9,6900005,桃,2026-11-10T00:00:00Z,2026-11-20T00:00:00Z\n"
        ),
        arrivals=(
            "编号,采购行号,扫码条码,UTC时刻\n"
            "B1,PL-5,6900005,2026-11-10T00:00:00Z\n"
            "B2,PL-5,6900005,2026-11-20T00:00:00Z\n"
        ),
    )
    assert arrival(client, "B1").get_json()["released"]  # 恰在起点：有归属
    b2 = arrival(client, "B2").get_json()
    assert not b2["released"]  # 恰在终点：无归属
    assert {b["reason"] for b in b2["blocks"]} == {"NO_OWNER"}


def test_failed_batch_keeps_previous_state(client):
    """同批一个文件非法 → 整批拒绝，合法文件也不入库，保留上次结果。"""
    seed(client)
    before = items(client)
    r = upload(
        client,
        arrivals=(
            "编号,采购行号,扫码条码,UTC时刻\n"
            "A100,PL-1,6900001,2026-11-10T00:00:00Z\n"
        ),
        code_usages=(
            "编号,条码,商品,UTC开始,UTC结束\n"
            "Ubad,6900001,苹果,not-a-time,2026-12-01T00:00:00Z\n"
        ),
    )
    assert r.status_code == 400
    assert r.get_json()["errors"]
    assert arrival(client, "A100").status_code == 404  # 合法部分也未生效
    assert items(client) == before
    assert arrival(client, "A1").get_json()["released"]


def test_only_related_arrivals_rechecked(client):
    """更正只回查改动前后关联的到货，其余到货的核查批次不变。"""
    seed(client)
    b1 = arrival(client, "A1").get_json()["checked_batch"]
    b5 = arrival(client, "A5").get_json()["checked_batch"]
    # 只改 6900002 的用码期 → 只有 A5（扫 6900002）被回查
    upload(client, code_usages=(
        "编号,条码,商品,UTC开始,UTC结束\n"
        "U2,6900002,香蕉,2026-11-01T00:00:00Z,2026-11-25T00:00:00Z\n"
    ))
    a1, a5 = arrival(client, "A1").get_json(), arrival(client, "A5").get_json()
    assert a5["checked_batch"] > b5
    assert a5["released"]  # 11-20 仍在新用码期内
    assert a1["checked_batch"] == b1  # 未关联的到货不被回查


def test_same_kind_same_id_latest_upload_wins(client):
    """同类同号以本次上传为准；文件内同号取最后一行。"""
    seed(client)
    # 同一文件内 A1 两行：后出现的 6900001 为准 → 放行
    upload(client, arrivals=(
        "编号,采购行号,扫码条码,UTC时刻\n"
        "A1,PL-1,6900002,2026-11-10T00:00:00Z\n"
        "A1,PL-1,6900001,2026-11-10T00:00:00Z\n"
    ))
    a1 = arrival(client, "A1").get_json()
    assert a1["barcode"] == "6900001" and a1["released"]
    # 再次上传同号不同内容 → 以本次为准，且造成退回
    upload(client, arrivals=(
        "编号,采购行号,扫码条码,UTC时刻\n"
        "A1,PL-1,6900002,2026-11-10T00:00:00Z\n"
    ))
    a1 = arrival(client, "A1").get_json()
    assert a1["barcode"] == "6900002" and not a1["released"]


def test_english_headers_accepted(client):
    r = upload(
        client,
        purchase_lines="id,barcode\nPL-E,6900010\n",
        code_usages=(
            "id,barcode,product,start_utc,end_utc\n"
            "UE,6900010,西瓜,2026-11-01T00:00:00Z,2026-12-01T00:00:00Z\n"
        ),
        arrivals="id,po_line_no,barcode,ts_utc\nAE,PL-E,6900010,2026-11-10T00:00:00Z\n",
    )
    assert r.status_code == 200, r.get_json()
    assert arrival(client, "AE").get_json()["released"]


def test_empty_upload_rejected(client):
    r = client.post("/api/upload", data={}, content_type="multipart/form-data")
    assert r.status_code == 400


def test_html_pages(client):
    seed(client)
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "采购员待修" in body and "商品档案员待修" in body
    assert "PL-9" in body and "6900004" in body
    r = client.get("/arrival/A1")
    assert r.status_code == 200 and "放行" in r.get_data(as_text=True)
    r = client.get("/items/po/BARCODE_MISMATCH/PL-1")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "A4" in body and "A6" in body and "A7" in body
    assert client.get("/arrival/NOPE").status_code == 404
