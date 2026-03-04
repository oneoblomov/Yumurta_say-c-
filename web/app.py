"""
app.py - FastAPI Ana Uygulama
==============================
Tüm route'lar, WebSocket, MJPEG streaming,
HTMX partial render, API endpointleri.
"""

import io
import csv
import json
import asyncio
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict

from fastapi import (
    FastAPI, Request, WebSocket, WebSocketDisconnect,
    UploadFile, File, Form, Query, HTTPException,
)
from fastapi.responses import (
    HTMLResponse, JSONResponse, StreamingResponse, FileResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .database import Database
from .pipeline_manager import PipelineManager
from .i18n import (
    load_translations, t, get_all_translations,
    SUPPORTED_LANGUAGES, DEFAULT_LANG,
)

# ============================================================ App
BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

app = FastAPI(title="Yumurta Sayıcı", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")),
          name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Global instances
db = Database()
pipeline = PipelineManager(db)

# WebSocket connections
ws_connections: List[WebSocket] = []


# ============================================================ Helpers
def _lang(request: Request) -> str:
    """İstek dili. Cookie > DB setting > default."""
    lang = request.cookies.get("lang")
    if not lang:
        lang = db.get_setting("language", DEFAULT_LANG)
    return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANG


def _ctx(request: Request, **extra) -> dict:
    """Template context oluştur."""
    lang = _lang(request)
    tr = load_translations(lang)
    return {
        "request": request,
        "lang": lang,
        "langs": SUPPORTED_LANGUAGES,
        "t": tr,
        "alert_count": db.get_unacknowledged_count(),
        **extra,
    }


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# ============================================================ Pages
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("base.html", {
        **_ctx(request),
        "page": "dashboard",
        "page_content": "partials/dashboard.html",
    })


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    ctx = _ctx(request, page="dashboard")
    if _is_htmx(request):
        return templates.TemplateResponse("partials/dashboard.html", ctx)
    ctx["page_content"] = "partials/dashboard.html"
    return templates.TemplateResponse("base.html", ctx)


@app.get("/records", response_class=HTMLResponse)
async def records_page(request: Request,
                       page_num: int = Query(1, alias="page")):
    per_page = 20
    offset = (page_num - 1) * per_page
    summaries = db.get_daily_summaries(limit=per_page)
    sessions = db.get_sessions(limit=per_page, offset=offset)
    total_sessions = db.get_sessions_count()
    ctx = _ctx(request,
               page="records",
               summaries=summaries,
               sessions=sessions,
               page_num=page_num,
               total_pages=max(1, (total_sessions + per_page - 1) // per_page))
    if _is_htmx(request):
        return templates.TemplateResponse("partials/records.html", ctx)
    ctx["page_content"] = "partials/records.html"
    return templates.TemplateResponse("base.html", ctx)


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    days = int(request.query_params.get("days", "30"))
    ctx = _ctx(
        request,
        page="stats",
        today_count=db.get_today_count(),
        week_count=db.get_week_count(),
        month_count=db.get_month_count(),
        all_time=db.get_all_time_count(),
        daily_avg=db.get_daily_average(days),
        peak_day=db.get_peak_day(),
        daily_trend=json.dumps(db.get_daily_trend(days)),
        monthly_trend=json.dumps(db.get_monthly_trend()),
        hourly_dist=json.dumps(db.get_hourly_distribution()),
        goals=db.get_active_goals(),
        days=days,
    )
    if _is_htmx(request):
        return templates.TemplateResponse("partials/stats.html", ctx)
    ctx["page_content"] = "partials/stats.html"
    return templates.TemplateResponse("base.html", ctx)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    ctx = _ctx(
        request,
        page="settings",
        settings_all=db.get_all_settings_detailed(),
        settings=db.get_settings(),
        versions=db.get_versions(),
        active_version=db.get_active_version(),
    )
    if _is_htmx(request):
        return templates.TemplateResponse("partials/settings.html", ctx)
    ctx["page_content"] = "partials/settings.html"
    return templates.TemplateResponse("base.html", ctx)


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    date_str = request.query_params.get("date", date.today().isoformat())
    events = db.get_events(date_str=date_str, limit=200)
    alerts = db.get_alerts(limit=50)
    ctx = _ctx(
        request,
        page="logs",
        events=events,
        alerts=alerts,
        log_date=date_str,
    )
    if _is_htmx(request):
        return templates.TemplateResponse("partials/logs.html", ctx)
    ctx["page_content"] = "partials/logs.html"
    return templates.TemplateResponse("base.html", ctx)


# ============================================================ API: Pipeline
@app.post("/api/pipeline/start")
async def api_start(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    source = body.get("source")
    return JSONResponse(pipeline.start(source=source))


@app.post("/api/pipeline/stop")
async def api_stop():
    return JSONResponse(pipeline.stop())


@app.post("/api/pipeline/pause")
async def api_pause():
    return JSONResponse(pipeline.pause())


@app.post("/api/pipeline/resume")
async def api_resume():
    return JSONResponse(pipeline.resume())


@app.post("/api/pipeline/reset")
async def api_reset():
    return JSONResponse(pipeline.reset_count())


@app.post("/api/pipeline/debug")
async def api_debug():
    v = pipeline.toggle_debug()
    return JSONResponse({"ok": True, "debug": v})


@app.get("/api/pipeline/status")
async def api_status():
    return JSONResponse(pipeline.get_status())


@app.get("/api/pipeline/events")
async def api_events():
    return JSONResponse(pipeline.get_recent_events())


# ============================================================ API: Stream
@app.get("/api/stream")
async def video_stream():
    return StreamingResponse(
        pipeline.frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ============================================================ API: Records
@app.get("/api/sessions")
async def api_sessions(limit: int = 50, offset: int = 0):
    return JSONResponse(db.get_sessions(limit, offset))


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: int):
    db.delete_session(session_id)
    return JSONResponse({"ok": True})


@app.get("/api/events")
async def api_get_events(session_id: int = None,
                         date_str: str = None,
                         limit: int = 100):
    return JSONResponse(
        db.get_events(session_id=session_id, date_str=date_str, limit=limit)
    )


@app.get("/api/daily")
async def api_daily(start: str = None, end: str = None):
    return JSONResponse(db.get_daily_summaries(start, end))


@app.delete("/api/daily/{date_str}")
async def api_delete_daily(date_str: str):
    db.delete_daily(date_str)
    return JSONResponse({"ok": True})


@app.post("/api/daily/{date_str}/reset")
async def api_reset_daily(date_str: str):
    db.reset_daily(date_str)
    return JSONResponse({"ok": True})


# ============================================================ API: Stats
@app.get("/api/stats")
async def api_stats(days: int = 30):
    return JSONResponse({
        "today": db.get_today_count(),
        "week": db.get_week_count(),
        "month": db.get_month_count(),
        "all_time": db.get_all_time_count(),
        "daily_avg": db.get_daily_average(days),
        "peak_day": db.get_peak_day(),
        "daily_trend": db.get_daily_trend(days),
        "monthly_trend": db.get_monthly_trend(),
        "hourly_dist": db.get_hourly_distribution(),
    })


@app.get("/api/stats/today")
async def api_stats_today():
    return JSONResponse({
        "count": db.get_today_count(),
        "goal": int(db.get_setting("daily_goal", "0")),
    })


# ============================================================ API: Settings
@app.get("/api/settings")
async def api_get_settings(category: str = None):
    return JSONResponse(db.get_settings(category))


@app.post("/api/settings")
async def api_save_settings(request: Request):
    body = await request.json()
    db.set_settings_bulk(body)
    return JSONResponse({"ok": True})


@app.post("/api/settings/language")
async def api_set_language(request: Request):
    body = await request.json()
    lang = body.get("language", "tr")
    db.set_setting("language", lang)
    response = JSONResponse({"ok": True, "language": lang})
    response.set_cookie("lang", lang, max_age=365 * 24 * 3600)
    return response


# ============================================================ API: Goals
@app.get("/api/goals")
async def api_goals():
    return JSONResponse(db.get_active_goals())


@app.post("/api/goals")
async def api_set_goal(request: Request):
    body = await request.json()
    db.set_goal(body["type"], body["target"])
    db.set_setting(f"{body['type']}_goal", str(body["target"]))
    return JSONResponse({"ok": True})


# ============================================================ API: Alerts
@app.get("/api/alerts")
async def api_alerts(unack_only: bool = False):
    return JSONResponse(db.get_alerts(unack_only))


@app.post("/api/alerts/{alert_id}/ack")
async def api_ack_alert(alert_id: int):
    db.acknowledge_alert(alert_id)
    return JSONResponse({"ok": True})


@app.post("/api/alerts/ack-all")
async def api_ack_all_alerts():
    db.acknowledge_all_alerts()
    return JSONResponse({"ok": True})


@app.get("/api/alerts/count")
async def api_alert_count():
    return JSONResponse({"count": db.get_unacknowledged_count()})


# ============================================================ API: Export
@app.get("/api/export/csv")
async def export_csv(date_str: str = None):
    events = db.get_events(date_str=date_str, limit=100000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "session_id", "timestamp", "track_id",
        "cx", "cy", "x1", "y1", "x2", "y2",
        "confidence", "running_total",
    ])
    for e in events:
        writer.writerow([
            e["id"], e["session_id"], e["timestamp"],
            e["track_id"], e["cx"], e["cy"],
            e["x1"], e["y1"], e["x2"], e["y2"],
            e["confidence"], e["running_total"],
        ])

    fname = f"yumurta_rapor_{date_str or 'tum'}.csv"
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@app.get("/api/export/excel")
async def export_excel(date_str: str = None):
    try:
        from openpyxl import Workbook
    except ImportError:
        raise HTTPException(500, "openpyxl yüklü değil")

    events = db.get_events(date_str=date_str, limit=100000)
    wb = Workbook()
    ws = wb.active
    ws.title = "Sayım Olayları"
    headers = [
        "ID", "Oturum", "Zaman", "Track ID",
        "CX", "CY", "X1", "Y1", "X2", "Y2",
        "Güven", "Toplam",
    ]
    ws.append(headers)
    for e in events:
        ws.append([
            e["id"], e["session_id"], e["timestamp"],
            e["track_id"], e["cx"], e["cy"],
            e["x1"], e["y1"], e["x2"], e["y2"],
            e["confidence"], e["running_total"],
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"yumurta_rapor_{date_str or 'tum'}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@app.get("/api/export/pdf")
async def export_pdf(date_str: str = None):
    try:
        from fpdf import FPDF
    except ImportError:
        raise HTTPException(500, "fpdf2 yüklü değil")

    events = db.get_events(date_str=date_str, limit=10000)
    summary = db.get_daily_summary(date_str) if date_str else None

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Yumurta Sayim Raporu", ln=True, align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, f"Tarih: {date_str or 'Tum zamanlar'}", ln=True)
    if summary:
        pdf.cell(0, 8,
                 f"Toplam: {summary['total_count']}  |  "
                 f"Oturum: {summary['session_count']}", ln=True)
    pdf.ln(5)

    # Table header
    pdf.set_font("Helvetica", "B", 8)
    col_w = [15, 35, 20, 20, 20, 30]
    headers = ["#", "Zaman", "Track", "CX,CY", "Guven", "Toplam"]
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 7, h, border=1)
    pdf.ln()

    pdf.set_font("Helvetica", "", 7)
    for idx, e in enumerate(events[:500]):
        pdf.cell(col_w[0], 6, str(idx + 1), border=1)
        pdf.cell(col_w[1], 6, str(e["timestamp"])[11:], border=1)
        pdf.cell(col_w[2], 6, str(e["track_id"] or ""), border=1)
        pdf.cell(col_w[3], 6, f"{e['cx']},{e['cy']}", border=1)
        pdf.cell(col_w[4], 6, f"{e['confidence']:.2f}", border=1)
        pdf.cell(col_w[5], 6, str(e["running_total"]), border=1)
        pdf.ln()

    buf = io.BytesIO(pdf.output())
    fname = f"yumurta_rapor_{date_str or 'tum'}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ============================================================ API: Import
@app.post("/api/import/csv")
async def import_csv(file: UploadFile = File(...)):
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    events = []
    for row in reader:
        events.append({
            "timestamp": row.get("timestamp", ""),
            "track_id": int(row.get("track_id", 0) or 0),
            "cx": int(row.get("cx", 0) or 0),
            "cy": int(row.get("cy", 0) or 0),
            "x1": int(row.get("x1", 0) or 0),
            "y1": int(row.get("y1", 0) or 0),
            "x2": int(row.get("x2", 0) or 0),
            "y2": int(row.get("y2", 0) or 0),
            "confidence": float(row.get("confidence", 0) or 0),
            "running_total": int(row.get("running_total", 0) or 0),
        })
    count = db.import_count_events(events)
    return JSONResponse({"ok": True, "imported": count})


# ============================================================ WebSocket
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_connections.append(websocket)
    try:
        while True:
            # Send status + events
            status = pipeline.get_status()
            new_events = pipeline.get_new_events(max_count=10)
            msg = {
                "status": status,
                "events": new_events,
                "alert_count": db.get_unacknowledged_count(),
            }
            await websocket.send_json(msg)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if websocket in ws_connections:
            ws_connections.remove(websocket)


# ============================================================ Version/Update
@app.get("/api/versions")
async def api_versions():
    return JSONResponse(db.get_versions())


@app.post("/api/versions")
async def api_add_version(request: Request):
    body = await request.json()
    db.add_version(
        body["version"],
        body.get("changelog"),
        body.get("backup_path"),
    )
    return JSONResponse({"ok": True})


@app.post("/api/versions/{vid}/rollback")
async def api_rollback(vid: int):
    ok = db.rollback_version(vid)
    return JSONResponse({"ok": ok})
