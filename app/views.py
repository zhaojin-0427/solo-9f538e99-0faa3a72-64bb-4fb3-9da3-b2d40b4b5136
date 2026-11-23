import json

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for,
)

from .core import (
    KIND_ARRIVALS, KIND_CODE_USAGES, KIND_LABELS, KIND_PURCHASE_LINES,
    REASON_LABELS, SIDE_BARCODE, SIDE_LABELS, SIDE_PO,
    BatchError, apply_batch, arrival_view, iso_utc, item_details, list_items,
    overview,
)
from .db import get_db

bp = Blueprint("main", __name__)

UPLOAD_FIELDS = (KIND_ARRIVALS, KIND_PURCHASE_LINES, KIND_CODE_USAGES)


def _collect_files(req):
    files = {}
    for kind in UPLOAD_FIELDS:
        f = req.files.get(kind)
        if f is not None and f.filename:
            files[kind] = f
    return files


def _batch_summaries(batches):
    out = []
    for b in batches:
        try:
            s = json.loads(b["stats"] or "{}")
        except ValueError:
            s = {}
        parts = []
        for kind, st in (s.get("stats") or {}).items():
            parts.append(
                f"{KIND_LABELS.get(kind, kind)} {st['rows']} 行"
                f"（新 {st['inserted']} / 改 {st['updated']} / 同 {st['unchanged']}）"
            )
        parts.append(f"回查到货 {s.get('affected_arrivals', 0)} 笔")
        out.append({
            "id": b["id"], "uploaded_at": b["uploaded_at"],
            "text": "；".join(parts),
        })
    return out


@bp.route("/")
def index():
    conn = get_db()
    stats, batches = overview(conn)
    return render_template(
        "index.html",
        po_items=list_items(conn, SIDE_PO),
        barcode_items=list_items(conn, SIDE_BARCODE),
        stats=stats,
        batches=_batch_summaries(batches),
        reason_labels=REASON_LABELS,
    )


@bp.route("/upload", methods=["POST"])
def upload():
    conn = get_db()
    try:
        summary = apply_batch(conn, _collect_files(request))
    except BatchError as e:
        for err in e.errors[:20]:
            flash(f"批次失败：{err}", "error")
        flash("整批未生效，保留上次结果。", "error")
        return redirect(url_for("main.index"))
    parts = [
        f"{KIND_LABELS[k]} {st['rows']} 行（新 {st['inserted']} / 改 {st['updated']} / 同 {st['unchanged']}）"
        for k, st in summary["stats"].items()
    ]
    flash(
        f"批次 #{summary['batch_id']} 已生效："
        + "；".join(parts)
        + f"；回查到货 {summary['affected_arrivals']} 笔。",
        "ok",
    )
    return redirect(url_for("main.index"))


@bp.route("/api/upload", methods=["POST"])
def api_upload():
    conn = get_db()
    try:
        summary = apply_batch(conn, _collect_files(request))
    except BatchError as e:
        return jsonify({"ok": False, "errors": e.errors}), 400
    return jsonify({"ok": True, **summary})


@bp.route("/items/<side>/<reason>/<path:key>")
def item(side, reason, key):
    if side not in SIDE_LABELS or reason not in REASON_LABELS:
        abort(404)
    rows = item_details(get_db(), side, reason, key)
    return render_template(
        "item.html", side=side, reason=reason, key=key, rows=rows,
        reason_labels=REASON_LABELS, side_labels=SIDE_LABELS,
    )


@bp.route("/arrival/<path:arrival_id>")
def arrival(arrival_id):
    view = arrival_view(get_db(), arrival_id)
    if view is None:
        abort(404)
    return render_template(
        "arrival.html", **view,
        reason_labels=REASON_LABELS, side_labels=SIDE_LABELS,
    )


@bp.route("/lookup")
def lookup():
    aid = request.args.get("id", "").strip()
    if not aid:
        return redirect(url_for("main.index"))
    return redirect(url_for("main.arrival", arrival_id=aid))


@bp.route("/api/items")
def api_items():
    conn = get_db()

    def pack(side):
        return [
            {
                "reason": r["reason"],
                "reason_label": REASON_LABELS[r["reason"]],
                "key": r["item_key"],
                "blocked": r["blocked"],
            }
            for r in list_items(conn, side)
        ]

    return jsonify({"po": pack(SIDE_PO), "barcode": pack(SIDE_BARCODE)})


@bp.route("/api/arrival/<path:arrival_id>")
def api_arrival(arrival_id):
    view = arrival_view(get_db(), arrival_id)
    if view is None:
        return jsonify({"ok": False, "error": "到货编号不存在"}), 404
    arr, status, blocks = view["arrival"], view["status"], view["blocks"]
    return jsonify({
        "id": arr["id"],
        "po_line_no": arr["po_line_no"],
        "barcode": arr["barcode"],
        "ts_utc": iso_utc(arr["ts"]),
        "released": bool(status["released"]) if status else False,
        "checked_batch": status["checked_batch"] if status else None,
        "blocks": [
            {
                "reason": b["reason"],
                "reason_label": REASON_LABELS[b["reason"]],
                "side": b["side"],
                "key": b["item_key"],
                "detail": b["detail"],
            }
            for b in blocks
        ],
    })
