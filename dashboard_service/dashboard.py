from flask import Flask, jsonify, request, render_template, redirect, url_for, session, flash
from datetime import datetime, timedelta
import pymysql
from flask_cors import CORS
import os                
import requests      
from flask import send_file  # add
from io import BytesIO       # add

# PDF (uses the same libs you already use in your Pi app)
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm

app = Flask(__name__, static_url_path='/static')
CORS(app)
app.secret_key = 'your_secret_key'  # Change to a strong secret key

# Local folder where mirrored snapshots will be stored and served by :5002
SNAP_DIR = os.path.join(app.root_path, "static", "snapshots")
os.makedirs(SNAP_DIR, exist_ok=True)

# ========================
# 🔹 MySQL Connection Helper
# ========================
def get_db():
    return pymysql.connect(
        host="localhost",
        port=3307,
        user="lpr_user",
        password="vistasummerose",
        database="lpr_system",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor
    )

# ========================
# 🔹 Utilities
# ========================
def fmt_ts(v):
    """Safely format MySQL DATETIME or string or None to 'YYYY-mm-dd HH:MM:SS' or 'N/A'."""
    if v in (None, "", "0000-00-00 00:00:00"):
        return "N/A"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    # Some drivers return bytes/string
    try:
        s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        # If it's already in a readable form, just return it
        # Optionally try to parse a few common formats:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                # If date only, show 00:00:00
                if fmt == "%Y-%m-%d":
                    dt = dt.replace(hour=0, minute=0, second=0)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        return s
    except Exception:
        return "N/A"

def parse_any_dt(s: str, is_end: bool = False) -> datetime:
    """
    Accepts 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM[:SS]', or 'YYYY-MM-DDTHH:MM[:SS]'.
    If only a date is provided:
      - is_end=False -> 00:00:00 of that day
      - is_end=True  -> 00:00:00 of NEXT day (exclusive upper bound)
    """
    if not s:
        return None
    s = s.strip()

    fmts = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%Y-%m-%d":
                if is_end:
                    dt = dt + timedelta(days=1)  # exclusive end at next day 00:00:00
                else:
                    dt = dt.replace(hour=0, minute=0, second=0)
            return dt
        except ValueError:
            continue

    # last resort: ISO-ish
    dt = datetime.fromisoformat(s.replace("T", " "))
    if is_end and len(s) == 10:
        dt = dt + timedelta(days=1)
    return dt

# --- Hidden plates helpers (MySQL) ---
def normalize_plate(p):
    return (p or "").strip().upper()

def ensure_hidden_table():
    """Create table once if it doesn't exist."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hidden_plates (
              plate VARCHAR(32) PRIMARY KEY,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    conn.close()

def abs_snapshot_url(host, snapshot_value):
    """Return absolute URL for snapshot if needed; handle empty/None."""
    snap = snapshot_value or ""
    if not snap:
        return ""  # or a default image like "http://{host}/static/default-car.png"
    return snap if snap.startswith("http") else f"http://{host}/{snap.lstrip('/')}"

def mirror_snapshot_to_5002(src_url: str, host_for_5002: str) -> str:
    """
    Download src_url (e.g., http://<pi>:5001/static/snapshots/<file>.jpg)
    into dashboard_service/static/snapshots/, and return the :5002 URL.
    Falls back to src_url if download fails.
    """
    try:
        if not src_url or not src_url.startswith("http"):
            return src_url

        fname = src_url.rsplit("/", 1)[-1]
        dst = os.path.join(SNAP_DIR, fname)

        # Skip download if already present
        if not os.path.exists(dst):
            r = requests.get(src_url, timeout=10)
            r.raise_for_status()
            with open(dst, "wb") as f:
                f.write(r.content)

        return f"http://{host_for_5002}:5002/static/snapshots/{fname}"
    except Exception as e:
        print(f"❌ Mirror failed for {src_url}: {e}")
        return src_url

def _image_flowable_from_any(snapshot_url_or_path: str, width_mm=170, height_mm=95):
    """
    Return a ReportLab Image flowable from local path or http(s) URL.
    If anything fails, return None.
    """
    try:
        if not snapshot_url_or_path:
            return None

        # If absolute URL, download to memory
        if snapshot_url_or_path.startswith("http"):
            r = requests.get(snapshot_url_or_path, timeout=8)
            r.raise_for_status()
            data = BytesIO(r.content)
            return RLImage(data, width=width_mm*mm, height=height_mm*mm)

        # Else assume local path relative to app root
        p = snapshot_url_or_path
        if not os.path.isabs(p):
            p = os.path.join(app.root_path, p)
        if os.path.exists(p):
            return RLImage(p, width=width_mm*mm, height=height_mm*mm)
    except Exception as e:
        print(f"⚠️ Snapshot load failed: {e}")
    return None


# ========================
# 🔹 Summons Fetch Helper
# ========================

# Where your Summons service lives (adjust host/port & path)
SUMMONS_API_BASE = os.environ.get("SUMMONS_API_BASE", "http://192.168.8.101:5001").rstrip("/")
# If your service is GET /api/summons?plate=ABC123:
SUMMONS_API = f"{SUMMONS_API_BASE}/summons"
# If instead yours is POST http://<host>:3000/summons, use:
# SUMMONS_API = f"{SUMMONS_API_BASE}/summons"


def fetch_summons(plate: str):
    """
    Ask the summons API for data for a plate.
    Expected response:
      - either {"summons":[...]} or a raw list [...]
    """
    try:
        url = SUMMONS_API
        # Use GET if your API is GET /api/summons?plate=ABC123
        r = requests.get(url, params={"plate": plate}, timeout=10)

        # Or, if your API expects POST {vehicleNumber: "..."}:
        # r = requests.post(url, json={"vehicleNumber": plate}, timeout=12)

        r.raise_for_status()
        js = r.json()

        if isinstance(js, dict) and "summons" in js:
            return js["summons"] or []
        if isinstance(js, list):
            return js
        return []
    except Exception as e:
        print(f"❌ Summons fetch error for {plate}: {e}")
        return []


# ========================
# 🔹 Login & Logout
# ========================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
            user = cursor.fetchone()
        conn.close()

        if user and password == user["password"]:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))
        else:
            flash("Invalid login credentials.")
            return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ========================
# 🔹 Web Pages
# ========================
@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return render_template("dashboard.html", username=session["username"])

@app.route("/api/hidden-plates", methods=["GET", "POST", "DELETE"])
def api_hidden_plates():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    ensure_hidden_table()

    if request.method == "GET":
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT plate FROM hidden_plates")
            rows = cur.fetchall()
        conn.close()
        return jsonify([r["plate"] for r in rows])

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        plate = normalize_plate(data.get("plate"))
        if not plate:
            return jsonify({"error": "plate required"}), 400
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO hidden_plates (plate) VALUES (%s)", (plate,))
        conn.close()
        return jsonify({"ok": True, "plate": plate}), 201

    # DELETE -> clear all hidden plates (Restore Hidden)
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE hidden_plates")
    conn.close()
    return ("", 204)

# ========================
# 🔹 GPS API
# ========================
GPS_LOGS = []

@app.route("/gps-tracking")
def gps_tracking():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if GPS_LOGS:
        return jsonify(GPS_LOGS[-1])
    return jsonify({"error": "No GPS data"}), 404

@app.route("/gps-tracking-history")
def gps_tracking_history():
    if "user_id" not in session:
        return redirect(url_for("login"))

    plate = (request.args.get("plate") or "").strip().upper()
    start = request.args.get("start")  # optional: "YYYY-MM-DD" or "YYYY-MM-DD HH:MM[:SS]"
    end   = request.args.get("end")    # optional

    # Build WHERE parts
    where = []
    params = []

    if plate:
        where.append("plate = %s")
        params.append(plate)

    # Parse optional dates (date-only expands to full-day range)
    def norm(s, is_end=False):
        if not s:
            return None
        try:
            # full datetime
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                # date-only
                d = datetime.strptime(s, "%Y-%m-%d")
                return (d + timedelta(days=1)) if is_end else d  # end is exclusive next-day 00:00
            except ValueError:
                # accept ISO-ish
                s2 = s.replace("T", " ")
                try:
                    return datetime.fromisoformat(s2)
                except Exception:
                    return None

    s_dt = norm(start, is_end=False)
    e_dt = norm(end,   is_end=True)

    if s_dt and e_dt:
        where.append("time >= %s AND time < %s")
        params += [s_dt.strftime("%Y-%m-%d %H:%M:%S"), e_dt.strftime("%Y-%m-%d %H:%M:%S")]
    elif s_dt:
        where.append("time >= %s")
        params.append(s_dt.strftime("%Y-%m-%d %H:%M:%S"))
    elif e_dt:
        where.append("time < %s")
        params.append(e_dt.strftime("%Y-%m-%d %H:%M:%S"))

    sql = "SELECT plate, latitude, longitude, speed, time FROM gps_logs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY time ASC"  # route drawing usually prefers ASC

    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.close()

        out = [{
            "plate": r["plate"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "speed": r.get("speed", 0),
            "time": (r["time"].strftime("%Y-%m-%d %H:%M:%S")
                     if hasattr(r["time"], "strftime") else str(r["time"]))
        } for r in rows]

        return jsonify(out), 200

    except Exception as e:
        print("❌ gps-tracking-history error:", e)
        return jsonify({"error": "server error"}), 500

@app.route("/api/gps", methods=["POST"])
def receive_gps():
    global GPS_LOGS
    data = request.json
    if data:
        data["plate"] = "VP1728"
        data["time"] = data.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        GPS_LOGS.append(data)
        if len(GPS_LOGS) > 1000:
            GPS_LOGS = GPS_LOGS[-1000:]

        try:
            conn = get_db()
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO gps_logs (plate, latitude, longitude, speed, time)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    data["plate"],
                    data.get("latitude"),
                    data.get("longitude"),
                    data.get("speed"),
                    data["time"]
                ))
            conn.close()
            print("📍 GPS_LOG updated:", data)
        except Exception as e:
            print("❌ Failed to insert GPS:", e)

        return jsonify({"status": "received"}), 200
    return jsonify({"error": "no data"}), 400

# ========================
# 🔹 Plate API
# ========================
@app.route("/api/receive-plate", methods=["POST"])
def receive_plate():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    # 1) Scofflaw if summons present
    if data.get("summons") and len(data["summons"]) > 0:
        data["status"] = "Scofflaw"

    # 2) Normalize time once
    data["time"] = fmt_ts(data.get("time"))

    # 3) Mirror snapshot from Pi -> dashboard :5002 (if HTTP URL)
    snap_in = (data.get("snapshot") or "").strip()
    dashboard_host = request.host.split(":")[0]  # e.g., lpr.vista-summerose.com

    if snap_in.startswith("http"):
        snap_out = mirror_snapshot_to_5002(snap_in, dashboard_host)
    elif snap_in:
        # relative path submitted; keep it (your GET APIs will make it absolute)
        snap_out = snap_in
    else:
        snap_out = "static/default-car.png"

    try:
        print("📝 Inserting into dashboard_plates:", {**data, "snapshot": snap_out})
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO dashboard_plates
                    (plate, status, snapshot, time, latitude, longitude, officer_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                data.get("plate"),
                data.get("status"),
                snap_out,
                None if data["time"] == "N/A" else data["time"],
                data.get("latitude"),
                data.get("longitude"),
                data.get("officer_id"),
            ))
        conn.close()
        print("✅ Plate saved.")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        print("❌ Failed to insert into dashboard_plates:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/received-plates", methods=["GET"])
def get_received_plates():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    start = request.args.get("start")
    end = request.args.get("end")

    try:
        conn = get_db()
        with conn.cursor() as cursor:
            query = "SELECT * FROM dashboard_plates"
            params = []

            if start and end:
                try:
                    start_dt = parse_any_dt(start, is_end=False)
                    end_dt   = parse_any_dt(end,   is_end=True)
                except Exception as e:
                    return jsonify({"error": f"Bad datetime: {e}"}), 400

                query += " WHERE time >= %s AND time < %s"
                params = [
                    start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                ]

            query += " ORDER BY id DESC LIMIT 2000"
            cursor.execute(query, params)
            rows = cursor.fetchall()

            # 🔹 Get globally hidden plates and build a set
            ensure_hidden_table()
            cursor.execute("SELECT plate FROM hidden_plates")
            hidden_set = {normalize_plate(r["plate"]) for r in cursor.fetchall()}

        plates = []
        for row in rows:
            if normalize_plate(row.get("plate")) in hidden_set:
                continue  # skip hidden
            formatted_time = fmt_ts(row.get("time"))
            snapshot = abs_snapshot_url(request.host, row.get("snapshot"))
            plates.append({
                "plate": row.get("plate"),
                "status": row.get("status"),
                "snapshot": snapshot,
                "time": formatted_time,
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "officer_id": row.get("officer_id")
            })

        conn.close()
        return jsonify(plates)

    except Exception as e:
        print("❌ Error retrieving plates:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/summons/<plate>")
def api_summons(plate):
    # secure like the rest
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    p = normalize_plate(plate)
    items = fetch_summons(p)
    return jsonify({"plate": p, "count": len(items), "items": items})

@app.route("/summons-pdf/<plate>")
def summons_pdf(plate):
    if "user_id" not in session:
        return redirect(url_for("login"))

    p = normalize_plate(plate)
    items = fetch_summons(p)
    if not items:
        return "No summons found for this plate.", 404

    # ---- Build a simple PDF
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"Summons for {p}", styles["Title"]),
        Spacer(1, 8),
    ]

    # Table header
    data = [["#", "Reference", "Date", "Amount", "Status"]]
    for i, s in enumerate(items, 1):
        ref = s.get("reference") or s.get("receiptNo") or "-"
        date = s.get("date") or s.get("issueDate") or "-"
        amt  = s.get("amount") or s.get("total") or "-"
        st   = s.get("status") or "-"
        data.append([i, ref, date, str(amt), st])

    table = Table(data, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("ALIGN", (0,0), (-1,0), "CENTER"),
    ]))
    story.append(table)

    doc.build(story)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"summons_{p}.pdf"
    )

@app.route("/api/generate-summons-pdf")
def generate_summons_pdf_row():
    """
    Professional per-plate PDF used by the button in 'N. Outstanding' column.
    Accepts:
      - plate (required)
      - time  (optional; used to pick the closest row)
    """
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    plate = normalize_plate(request.args.get("plate", ""))
    time_str = request.args.get("time", "").strip()
    if not plate:
        return ("Missing plate", 400)

    # Parse reference time if given
    ref_dt = None
    if time_str:
        try:
            ref_dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            ref_dt = None

    # 1) Get latest/closest dashboard row for summary + snapshot
    last_status = "-"
    last_time = "-"
    snapshot_value = ""

    conn = get_db()
    try:
        with conn.cursor() as cur:
            if ref_dt:
                cur.execute("""
                    SELECT status, time, snapshot
                    FROM dashboard_plates
                    WHERE plate=%s
                    ORDER BY ABS(TIMESTAMPDIFF(SECOND, time, %s)) ASC
                    LIMIT 1
                """, (plate, ref_dt))
            else:
                cur.execute("""
                    SELECT status, time, snapshot
                    FROM dashboard_plates
                    WHERE plate=%s
                    ORDER BY time DESC
                    LIMIT 1
                """, (plate,))
            r = cur.fetchone()
            if r:
                last_status = r.get("status") or "-"
                last_time = fmt_ts(r.get("time"))
                snapshot_value = r.get("snapshot") or ""
    finally:
        conn.close()

    # 2) Fetch summons and compute outstanding (Not Paid / Scofflaw / Unpaid / Outstanding)
    items_all = fetch_summons(plate)
    def is_outstanding(x):
        s = (x.get("status") or "").strip().lower()
        return s in ("not paid", "scofflaw", "unpaid", "outstanding")
    outstanding_items = [x for x in items_all if is_outstanding(x)]
    n_outstanding = len(outstanding_items)

    # Sum outstanding amount if present
    total_amt = 0.0
    for x in outstanding_items:
        val = x.get("amount") or x.get("total")
        try:
            total_amt += float(str(val).replace(",", ""))
        except Exception:
            pass

    # 3) Build professional PDF
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=16*mm, rightMargin=16*mm, topMargin=16*mm, bottomMargin=16*mm
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ttl", parent=styles["Title"], fontSize=16, leading=20, alignment=1)
    h_style = ParagraphStyle("h", parent=styles["Heading2"], spaceAfter=6)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9)

    story = []
    # optional logo if you have static/logo.png
    logo_path = os.path.join(app.root_path, "static", "logo.png")
    if os.path.exists(logo_path):
        story.append(RLImage(logo_path, width=30*mm, height=30*mm))
        story.append(Spacer(1, 6))

    story.append(Paragraph("Summons Report", title_style))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", small))
    story.append(Spacer(1, 10))

    # Summary block
    summary_data = [
        ["License Plate", plate],
        ["Last Status", last_status],
        ["Last Seen Time", last_time],
        ["N. Outstanding", str(n_outstanding)],
        ["Total Outstanding Amount (RM)", f"{total_amt:.2f}"],
    ]
    t = Table(summary_data, colWidths=[50*mm, None], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f2f4f7")),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#fafafa")]),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))

    # Snapshot if available (works for local or http URL)
    snapshot_abs = abs_snapshot_url(request.host, snapshot_value)
    img_flow = _image_flowable_from_any(snapshot_abs)
    if img_flow:
        story.append(Paragraph("Latest Snapshot", h_style))
        story.append(img_flow)
        story.append(Spacer(1, 10))

    # Detailed outstanding list
    story.append(Paragraph("Open Summons (Not Paid / Scofflaw)", h_style))
    if outstanding_items:
        header = ["Ref No", "Offence", "Amount (RM)", "Issued At", "Status"]
        body = []
        for s in outstanding_items:
            ref = s.get("reference") or s.get("receiptNo") or s.get("ref_no") or "-"
            off = s.get("offence") or s.get("offence_desc") or "-"
            amt = s.get("amount") or s.get("total") or ""
            try:
                amt = f"{float(str(amt).replace(',', '')):.2f}"
            except Exception:
                amt = str(amt)
            issued = s.get("date") or s.get("issueDate") or s.get("issued_at") or "-"
            status = s.get("status") or "-"
            body.append([ref, off, amt, issued, status])

        tbl = Table([header] + body, colWidths=[28*mm, 65*mm, 28*mm, 40*mm, 25*mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1f6feb")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
            ("ALIGN", (2,1), (2,-1), "RIGHT"),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f8fbff")]),
        ]))
        story.append(tbl)
    else:
        story.append(Paragraph("No outstanding summons found.", styles["Italic"]))

    doc.build(story)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{plate}_summons.pdf", mimetype="application/pdf")


@app.route("/api/plates", methods=["GET"])
def api_get_detected_plates():
    start = request.args.get("start")
    end = request.args.get("end")

    try:
        conn = get_db()
        with conn.cursor() as cursor:
            query = """
                SELECT plate, status, snapshot, time, latitude, longitude, officer_id
                FROM dashboard_plates
                WHERE 1=1
            """
            params = []

            if start:
                # If date only, convert to start of day
                try:
                    sd = datetime.strptime(start, "%Y-%m-%d")
                    start_str = sd.strftime("%Y-%m-%d 00:00:00")
                except Exception:
                    start_str = start
                query += " AND time >= %s"
                params.append(start_str)

            if end:
                # If date only, make it exclusive next day
                try:
                    ed = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
                    end_str = ed.strftime("%Y-%m-%d 00:00:00")
                except Exception:
                    end_str = end
                query += " AND time < %s"
                params.append(end_str)

            query += " ORDER BY id DESC LIMIT 500"
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()

        data = []
        for row in rows:
            formatted_time = fmt_ts(row.get("time"))
            snapshot_url = abs_snapshot_url(request.host, row.get("snapshot"))

            data.append({
                "plate": row.get("plate"),
                "status": row.get("status"),
                "timestamp": formatted_time,
                "snapshot_url": snapshot_url,
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "officer_id": row.get("officer_id")
            })

        conn.close()
        return jsonify(data)

    except Exception as e:
        print("❌ Error fetching plates:", e)
        return jsonify({"error": str(e)}), 500

# ========================
# 🔹 Run Server
# ========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False)
