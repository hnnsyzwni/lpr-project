from flask import Flask, render_template, Response, jsonify, request, redirect, url_for, session, send_file
import platform

if platform.system() == "Linux":
    from picamera2 import Picamera2

import cv2
import time
import threading
import os
import requests
from queue import Queue
import pandas as pd
from io import BytesIO
import gps
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle, SimpleDocTemplate, Paragraph, Image
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from datetime import datetime
import numpy as np  # Added for motion detection
import pymysql
from threading import Thread
import shutil
import socket
from pathlib import Path
import json
import subprocess
from threading import Thread

def _normalize_datetime_text(s: str) -> str | None:
    if not s:
        return None
    s = s.strip().replace("  ", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None

def extract_paid_until_from_status(status: str) -> str | None:
    """
    Pull 'YYYY-MM-DD HH:MM[:SS]' from strings like
    'Paid until 2025-09-28 22:30[:45]'.
    """
    if not isinstance(status, str):
        return None
    if "paid until" not in status.lower():
        return None
    raw = status.lower().split("paid until", 1)[-1].strip()
    return _normalize_datetime_text(raw)

OFFLINE_FILE = "offline_queue.json"

def is_connected(host="8.8.8.8", port=53, timeout=3):
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error:
        return False

def save_offline(data):
    path = Path(OFFLINE_FILE)
    if path.exists():
        with open(path, "r") as f:
            try:
                offline_data = json.load(f)
            except json.JSONDecodeError:
                offline_data = []
    else:
        offline_data = []

    offline_data.append(data)
    with open(path, "w") as f:
        json.dump(offline_data, f, indent=2)

# ✅ MariaDB connection
def get_db():
    return pymysql.connect(
        host="127.0.0.1",           # force TCP (avoid unix_socket quirks)
        port=3306,                  # your MariaDB listens on 3306
        user="lpr_user",
        password="vistasummerose",
        database="lpr_system",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5, read_timeout=10, write_timeout=10
    )

def get_latest_summons_for_plate(plate: str):
    """Return a list of summons dicts for the plate from detected_plates.summons_json"""
    if not plate:
        return []

    plate = plate.strip().upper()
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("""
                SELECT summons_json
                FROM detected_plates
                WHERE plate=%s
                ORDER BY id DESC
                LIMIT 1
            """, (plate,))
            row = cur.fetchone()
        db.close()

        if row and row.get("summons_json"):
            try:
                return json.loads(row["summons_json"]) or []
            except Exception:
                return []
        return []
    except Exception as e:
        print("❌ get_latest_summons_for_plate error:", e)
        return []

app = Flask(__name__)
app.secret_key = "supersecretkey"  # Change this to a secure key

app.config["SNAPSHOT_FOLDER"] = "static/snapshots"
if not os.path.exists(app.config["SNAPSHOT_FOLDER"]):
    os.makedirs(app.config["SNAPSHOT_FOLDER"])

# API Details
PLATE_RECOGNIZER_API_URL = "https://api.platerecognizer.com/v1/plate-reader/"
PARKING_API_URL = "https://mycouncil.citycarpark.my/parking/ctcp/services-listerner_mbk.php"
NODE_API_URL = "http://localhost:5000/api/summons"
API_TOKEN = "7a5650fef8c594f93549eb9dea557d1bcbf1b42e"
PARKING_API_ACTION = "GetParkingRightByPlateVerify"
 
detected_plates = []
summons_data = []  # Store fetched summons data globally
lock = threading.Lock()
frame_queue0 = Queue(maxsize=1)
frame_queue1 = Queue(maxsize=1) # Increased queue size
gps_logs = []  # ✅ Store latest GPS readings
stored_officer_id = "Unknown"  # ✅ Store officer ID globally

latest_gps = {"latitude": None, "longitude": None, "last_update": None}  # ✅ Global GPS storage

# --- behavior flags ---
ALLOW_NO_GPS = True            # ✅ Benarkan detect walaupun GPS belum lock
WRITE_NULL_GPS_AS_ZERO = False # ✅ Jika True, simpan 0.0/0.0 bila GPS tiada; kalau False, simpan NULL
DUP_COOLDOWN_S = 10
# -----------------------


# API Logging Stats
api_stats = {
    "success_count": 0,
    "failure_count": 0,
    "total_time": 0.0
}

# API Throttler
class Throttler:
    def __init__(self, rate_limit, interval=1):
        self.rate_limit = rate_limit
        self.interval = interval
        self.timestamps = []

    def wait(self):
        while True:
            now = time.time()
            self.timestamps = [t for t in self.timestamps if now - t < self.interval]
            if len(self.timestamps) < self.rate_limit:
                break
            time.sleep(self.interval - (now - self.timestamps[0]))
        self.timestamps.append(now)

throttler = Throttler(rate_limit=8, interval=1)  # 8 API calls per second

def crop_plate_region(frame):
    h, w, _ = frame.shape
    return frame[int(h * 0.1):int(h * 0.99), int(w * 0.01):int(w * 0.99)]

recent_plates = {}

def is_duplicate_plate(plate, cooldown=10):
    now = time.time()
    if plate in recent_plates and now - recent_plates[plate] < cooldown:
        return True
    recent_plates[plate] = now
    return False

def gps_updater():
    global latest_gps
    try:
        session = gps.gps(mode=gps.WATCH_ENABLE)
        for report in session:
            if report['class'] == 'TPV':
                lat = getattr(report, 'lat', None)
                lon = getattr(report, 'lon', None)
                if lat and lon:
                    latest_gps["latitude"] = round(lat, 6)
                    latest_gps["longitude"] = round(lon, 6)
                    latest_gps["last_update"] = time.time()
                    print(f"✅ GPS Updated: {latest_gps}")
    except Exception as e:
        print(f"❌ GPS updater error: {e}")

# Start GPS thread
threading.Thread(target=gps_updater, daemon=True).start()

# ===== Initialize Dual Cameras (Pi 5: CAM0 near USB-C, CAM1 near Ethernet) =====
cam0 = cam1 = None

def init_camera(index):
    from picamera2 import Picamera2
    cam = Picamera2(index)
    config = cam.create_preview_configuration(main={"size": (640, 480), "format": "RGB888"})
    cam.configure(config)
    cam.start()
    print(f"✅ Camera {index} started (640x480 RGB888)")
    return cam

try:
    cam0 = init_camera(0)  # usually CAM0 = right/front
except Exception as e:
    print(f"⚠️ CAM0 failed: {e}")

try:
    cam1 = init_camera(1)  # usually CAM1 = left/rear
except Exception as e:
    print(f"⚠️ CAM1 failed: {e}")

if not cam0 and not cam1:
    print("❌ No cameras initialized.")
else:
    print("✅ Camera(s) initialized successfully.")

def send_gps_to_dashboard(data):
    if not is_connected():
        print("📴 No internet, saving GPS to offline queue")
        save_offline({"type": "gps", "data": data})
        return

    urls = [
        "http://52.163.74.67:5002/api/gps",
        "http://192.168.8.108:5002/api/gps"
    ]
    for url in urls:
        try:
            response = requests.post(url, json=data, timeout=5)
            if response.status_code == 200:
                print("📡 GPS forwarded successfully:", url)
                return
        except Exception as e:
            print("❌ Failed GPS upload:", e)

    print("📦 Saving GPS to offline queue (all URLs failed)")
    save_offline({"type": "gps", "data": data})

# Authentication Routes
@app.route("/login", methods=["GET", "POST"])
def login():
    global stored_officer_id

    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
                user = cursor.fetchone()

                # ✅ DEBUG PRINTS (inside the try block)
                print(f"🧪 Trying login: username={username}, password={password}")
                print(f"🧪 DB user found: {user}")

        finally:
            conn.close()

        if user and password == user["password"]:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["officer_id"] = user["officer_id"]
            stored_officer_id = user["officer_id"]  # Store globally for detection
            return redirect(url_for("dashboard"))
        else:
            return render_template("login.html", error="Invalid login credentials.")

    return render_template("login.html")

@app.route("/logout")
def logout():
    global stored_officer_id
    session.clear()
    stored_officer_id = "Unknown"
    return redirect(url_for("login"))

@app.route("/")
def dashboard():
    if "user_id" not in session:  # ✅ If user is not logged in, redirect to login page
        return redirect(url_for("login"))
    return render_template("index.html")  # ✅ If logged in, show the dashboard

# License Plate Recognition
def recognize_plate(frame):
    throttler.wait()
    try:
        start_time = time.time()
        roi = crop_plate_region(frame)
        _, img_encoded = cv2.imencode(".jpg", roi, [int(cv2.IMWRITE_JPEG_QUALITY), 25])
        img_bytes = img_encoded.tobytes()

        print("📤 Sending image to Plate Recognizer API...")

        response = requests.post(
            PLATE_RECOGNIZER_API_URL,
            files={"upload": ("image.jpg", img_bytes, "image/jpeg")},
            headers={"Authorization": f"Token {API_TOKEN}"},
            timeout=30
        )

        elapsed = time.time() - start_time
        api_stats["total_time"] += elapsed

        if response.status_code == 201:
            api_stats["success_count"] += 1
            print(f"✅ Plate Recognizer Success in {elapsed:.2f}s | Total Success: {api_stats['success_count']}")
            return response.json().get("results", [])
        else:
            api_stats["failure_count"] += 1
            print(f"❌ API Error {response.status_code} in {elapsed:.2f}s | Total Failures: {api_stats['failure_count']}")
            return []

    except requests.exceptions.RequestException as e:
        api_stats["failure_count"] += 1
        print(f"❌ Request Exception: {e} | Total Failures: {api_stats['failure_count']}")
        return []

def check_parking_status(plate_number):
    try:
        response = requests.get(
            PARKING_API_URL,
            params={"prpid": "", "action": PARKING_API_ACTION, "filterid": plate_number},
            verify=False, timeout=5
        )
        if response.status_code == 200:
            result = response.json()
            if isinstance(result, list) and result:
                return f"Paid until {result[0].get('enddate', 'Unknown')} {result[0].get('endtime', '')}"
            return "Not Paid"
        return "Error"
    except requests.exceptions.RequestException as e:
        print(f"Parking API failed: {e}")
        return "Error"

def check_summons_status(plate_number):
    try:
        response = requests.post(
            NODE_API_URL,
            json={"vehicleNumber": plate_number},
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        data = response.json()
        if isinstance(data, list):  # ✅ If API returns a list, return it directly
            return data
        elif isinstance(data, dict) and "summonsQueue" in data:  # ✅ Handle dictionary response
            return data["summonsQueue"]
        return []  # ✅ Default return if format is unexpected
    except requests.exceptions.RequestException as e:
        print(f"Summons API failed: {e}")
        return []

# Frame processing

# ✅ Insert this updated section inside your `process_frames()` function

def process_frames():
    """
    Process frames -> Plate Recognizer -> save & forward.
    - Skips recent duplicates via is_duplicate_plate(...)
    - Allows detection even if GPS is missing (flags control NULL vs 0.0)
    """
    global stored_officer_id
    while True:
        if frame_queue.empty():
            time.sleep(0.01)
            continue

        frame = frame_queue.get()
        try:
            plates = recognize_plate(frame)
        except Exception as e:
            print(f"❌ recognize_plate error: {e}")
            plates = []

        for plate_data in plates:
            plate_number = (plate_data.get("plate") or "").upper().strip()
            if not plate_number:
                print("⚠️ No plate detected, skipping...")
                continue

            # ✅ Skip duplicates within cooldown
            if is_duplicate_plate(plate_number, cooldown=10):  # change 10 to your desired seconds
                print(f"⚠️ Recently detected {plate_number}, skipping duplicate.")
                continue

            # --- Snapshot ---
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            snapshot_name = f"{plate_number}_{int(time.time())}.jpg"
            snapshot_path = os.path.join(app.config["SNAPSHOT_FOLDER"], snapshot_name)
            try:
                cv2.imwrite(snapshot_path, frame)
            except Exception as e:
                print(f"❌ Failed to save snapshot: {e}")
                snapshot_path = None

            # --- GPS (allow missing) ---
            cur_lat = latest_gps.get("latitude")
            cur_lon = latest_gps.get("longitude")
            if (cur_lat is None or cur_lon is None) and ALLOW_NO_GPS:
                if WRITE_NULL_GPS_AS_ZERO:
                    lat_to_write = 0.0
                    lon_to_write = 0.0
                else:
                    lat_to_write = None
                    lon_to_write = None
            else:
                lat_to_write = cur_lat
                lon_to_write = cur_lon

            officer_id = stored_officer_id

            # --- Parking & Summons ---
            try:
                parking_status = check_parking_status(plate_number)
            except Exception as e:
                print(f"Parking status error: {e}")
                parking_status = "Error"

            try:
                summons_status = check_summons_status(plate_number)
            except Exception as e:
                print(f"Summons status error: {e}")
                summons_status = []

            # --- Final status logic ---
            if summons_status and isinstance(summons_status, list) and len(summons_status) > 0:
                final_status = summons_status[0].get("status", "Not Paid")
            elif isinstance(parking_status, str) and "Paid until" in parking_status:
                final_status = parking_status
            else:
                final_status = "Not Paid"

            # ✅ extract normalized paid-until timestamp if present
            paid_until_val = extract_paid_until_from_status(final_status)

            

            snapshot_url = (
                f"http://{request.host}/{snapshot_path}"
                if snapshot_path and request
                else f"http://192.168.8.102:5001/static/snapshots/{snapshot_name}"
            )

            plate_info = {
                "plate": plate_number,
                "status": final_status,
                "summons": summons_status if isinstance(summons_status, list) else [],
                "time": timestamp,
                "snapshot": snapshot_url,
                "latitude": lat_to_write,
                "longitude": lon_to_write,
                "officer_id": officer_id,
                "paid_until": paid_until_val,  # ✅ new
            }

            with lock:
                detected_plates.append(plate_info)
                send_plate_to_dashboard(plate_info)

            print(f"✅ Added Detected Plate: {plate_info}")

            # --- Save to DB ---
            try:
                db = get_db()
                with db.cursor() as cursor:
                    summons_total = len(plate_info["summons"])
                    cursor.execute("""
                        INSERT INTO detected_plates
                            (plate, timestamp, image_path, latitude, longitude, officer_id, status, summons_total, summons_json)
                        VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        plate_number,
                        timestamp,
                        snapshot_path,
                        lat_to_write,
                        lon_to_write,
                        officer_id,
                        final_status,
                        summons_total,
                        json.dumps(plate_info["summons"])
                    ))
                    cursor.execute("""
                        INSERT INTO plate_history
                            (plate, timestamp, image_path, latitude, longitude, officer_id)
                        VALUES
                            (%s, %s, %s, %s, %s, %s)
                    """, (
                        plate_number,
                        timestamp,
                        snapshot_path,
                        lat_to_write,
                        lon_to_write,
                        officer_id
                    ))
                db.commit()
                db.close()
                print("✅ Plate saved to DB")
            except Exception as e:
                print("❌ Failed to insert plate into DB:", e)

        frame_queue.task_done()

# ✅ Helper to send data to dashboard (Async/threaded version)
def send_plate_to_dashboard(plate_info):
    def forward():
        if not is_connected():
            print("📴 No internet, saving plate to offline queue")
            save_offline({"type": "plate", "data": plate_info})
            return

        dashboard_urls = [
            "http://192.168.8.108:5001/api/receive-plate",
            "http://192.168.8.108:5002/api/receive-plate",
            "http://52.163.74.67:5002/api/receive-plate",
            "http://52.163.74.67:5001/api/receive-plate"
        ]

        for url in dashboard_urls:
            try:
                print(f"🔁 Sending plate {plate_info['plate']} to {url}")
                response = requests.post(url, json=plate_info, timeout=5)
                print(f"📤 Response from {url}: {response.status_code}")
            except Exception as e:
                print(f"❌ Failed to send to {url}: {e}")

    Thread(target=forward).start()

def process_frames_cam0():
    while True:
        if frame_queue0.empty():
            time.sleep(0.01)
            continue
        frame = frame_queue0.get()
        process_frame_logic(frame, "cam0")  # helper call (see below)
        frame_queue0.task_done()

def process_frames_cam1():
    while True:
        if frame_queue1.empty():
            time.sleep(0.01)
            continue
        frame = frame_queue1.get()
        process_frame_logic(frame, "cam1")
        frame_queue1.task_done()

def process_frame_logic(frame, label):
    global stored_officer_id
    try:
        plates = recognize_plate(frame)
        print(f"[{label}] Plates detected:", plates)
    except Exception as e:
        print(f"[{label}] ❌ Detection error:", e)
        plates = []

    for plate_data in plates:
        plate_number = (plate_data.get("plate") or "").upper().strip()
        if not plate_number:
            continue

        # Skip duplicates within cooldown
        if is_duplicate_plate(plate_number, cooldown=DUP_COOLDOWN_S):
            print(f"[{label}] ⚠️ Recently detected {plate_number}, skipping duplicate.")
            continue

        # --- Snapshot ---
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        snapshot_name = f"{plate_number}_{int(time.time())}.jpg"
        snapshot_path = os.path.join(app.config["SNAPSHOT_FOLDER"], snapshot_name)
        try:
            cv2.imwrite(snapshot_path, frame)
        except Exception as e:
            print(f"[{label}] ❌ Failed to save snapshot: {e}")
            snapshot_path = None

        # --- GPS (allow missing) ---
        cur_lat = latest_gps.get("latitude")
        cur_lon = latest_gps.get("longitude")
        if (cur_lat is None or cur_lon is None) and ALLOW_NO_GPS:
            lat_to_write = 0.0 if WRITE_NULL_GPS_AS_ZERO else None
            lon_to_write = 0.0 if WRITE_NULL_GPS_AS_ZERO else None
        else:
            lat_to_write = cur_lat
            lon_to_write = cur_lon

        officer_id = stored_officer_id

        # --- Parking & Summons ---
        try:
            parking_status = check_parking_status(plate_number)
        except Exception as e:
            print(f"[{label}] Parking status error:", e)
            parking_status = "Error"

        try:
            summons_status = check_summons_status(plate_number)
        except Exception as e:
            print(f"[{label}] Summons status error:", e)
            summons_status = []

        # Final status
        if summons_status and isinstance(summons_status, list) and len(summons_status) > 0:
            final_status = summons_status[0].get("status", "Not Paid")
        elif isinstance(parking_status, str) and "Paid until" in parking_status:
            final_status = parking_status
        else:
            final_status = "Not Paid"

        paid_until_val = extract_paid_until_from_status(final_status)

        # Build a safe snapshot URL (thread-safe fallback)
        try:
            base = f"http://{request.host}"
        except Exception:
            base = "http://192.168.8.102:5001"  # <-- change if your Pi IP/port differs
        snapshot_url = f"{base}/{snapshot_path}" if snapshot_path else None

        plate_info = {
            "plate": plate_number,
            "status": final_status,
            "summons": summons_status if isinstance(summons_status, list) else [],
            "time": timestamp,
            "snapshot": snapshot_url,       # keep legacy key
            "snapshot_url": snapshot_url,   # and new key for UI
            "latitude": lat_to_write,
            "longitude": lon_to_write,
            "officer_id": officer_id,
            "cam": label,
            "paid_until": paid_until_val,
        }

        # In-memory + forward to dashboards
        with lock:
            detected_plates.append(plate_info)
            send_plate_to_dashboard(plate_info)

        print(f"[{label}] ✅ Added Detected Plate: {plate_info}")

        # --- Save to DB ---
        try:
            db = get_db()
            with db.cursor() as cursor:
                summons_total = len(plate_info["summons"])
                cursor.execute("""
                    INSERT INTO detected_plates
                        (plate, timestamp, image_path, latitude, longitude, officer_id, status, summons_total, summons_json)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    plate_number,
                    timestamp,
                    snapshot_path,
                    lat_to_write,
                    lon_to_write,
                    officer_id,
                    final_status,
                    summons_total,
                    json.dumps(plate_info["summons"])
                ))
                cursor.execute("""
                    INSERT INTO plate_history
                        (plate, timestamp, image_path, latitude, longitude, officer_id)
                    VALUES
                        (%s, %s, %s, %s, %s, %s)
                """, (
                    plate_number,
                    timestamp,
                    snapshot_path,
                    lat_to_write,
                    lon_to_write,
                    officer_id
                ))
            db.commit()
            db.close()
            print(f"[{label}] ✅ Plate saved to DB")
        except Exception as e:
            print(f"[{label}] ❌ Failed to insert plate into DB:", e)

# Start threads
threading.Thread(target=process_frames_cam0, daemon=True).start()
threading.Thread(target=process_frames_cam1, daemon=True).start()


def generate_frames(cam, queue, label="cam"):
    if not cam:
        yield b"Camera not initialized."
        return

    count = 0
    frame_skip = 1  # process every frame

    while True:
        try:
            frame = cam.capture_array()
            frame = cv2.resize(frame, (640, 480))
            count += 1
            if count % frame_skip == 0 and not queue.full():
                queue.put(frame.copy())
            _, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" +
                   buffer.tobytes() + b"\r\n")
        except Exception as e:
            print(f"[{label}] ❌ Error capturing frame: {e}")
            break

# ---- MJPEG routes for each camera ----
@app.route("/video_right")  # CAM0
def video_right():
    return Response(
        generate_frames(cam0, frame_queue0, "cam0"),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )

@app.route("/video_left")   # CAM1
def video_left():
    return Response(
        generate_frames(cam1, frame_queue1, "cam1"),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )

@app.route("/plates", methods=["GET"])
def plates():
    try:
        db = get_db()
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT id, plate, timestamp, image_path, latitude, longitude, officer_id, status, summons_total, summons_json
                FROM detected_plates ORDER BY id DESC LIMIT 100
            """)
            rows = cursor.fetchall()
        db.close()

        plates_from_db = []
        for row in rows:
            # Only build URLs if we actually have an image_path
            snapshot_url = None
            if row.get("image_path"):
                snapshot_url = f"http://{request.host}/{row['image_path']}"

            plates_from_db.append({
                "id": row["id"],
                "plate": row["plate"],
                "status": row.get("status", "Not Paid"),
                "time": row["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
                "snapshot_url": snapshot_url,   # <-- use this
                "snapshot": snapshot_url,
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "officer_id": row["officer_id"],
                "summons": json.loads(row.get("summons_json") or "[]"),
                "total_summons": row.get("summons_total", 0),
                "paid_until": extract_paid_until_from_status(row.get("status", "")),  # ✅ expose paid_until
            })

        return jsonify(plates_from_db)
    except Exception as e:
        print("❌ Error loading plates from DB:", e)
        return jsonify([]), 500

@app.route('/api/delete-plate', methods=['POST'])
def delete_plate():
    try:
        data = request.get_json(force=True)
        plate_id = data.get('id')
        plate = (data.get('plate') or '').strip().upper()

        if not plate_id and not plate:
            return jsonify({"status": "error", "message": "Missing id or plate"}), 400

        db = get_db()
        with db.cursor() as cur:
            # fetch row for file removal
            if plate_id:
                cur.execute("SELECT image_path FROM detected_plates WHERE id=%s LIMIT 1", (plate_id,))
                row = cur.fetchone()
                cur.execute("DELETE FROM detected_plates WHERE id=%s LIMIT 1", (plate_id,))
            else:
                cur.execute("""
                    SELECT id, image_path FROM detected_plates
                    WHERE plate=%s ORDER BY id DESC LIMIT 1
                """, (plate,))
                row = cur.fetchone()
                cur.execute("""
                    DELETE FROM detected_plates
                    WHERE plate=%s ORDER BY id DESC LIMIT 1
                """, (plate,))
        db.commit()
        db.close()

        # remove snapshot file
        if row and row.get('image_path'):
            try:
                if os.path.exists(row['image_path']):
                    os.remove(row['image_path'])
            except Exception as e:
                print("⚠️ could not delete snapshot:", e)

        # keep in-memory cache consistent
        with lock:
            global detected_plates
            if plate_id:
                detected_plates = [p for p in detected_plates if str(p.get('id')) != str(plate_id)]
            elif plate:
                detected_plates = [p for p in detected_plates if (p.get('plate') or '').upper() != plate]

        return jsonify({"status": "success"})
    except Exception as e:
        print("❌ delete_plate error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/user", methods=["GET"])
def get_user():
    global stored_officer_id
    if "user_id" in session:
        stored_officer_id = session.get("officer_id", "Unknown")
        return jsonify({"username": session.get("username"), "officer_id": stored_officer_id})
    return jsonify({"error": "Not logged in"}), 401

@app.route("/summons", methods=["GET"])
def get_summons():
    """
    Returns summons data for all detected plates,
    or only for a specific plate if ?plate=XYZ is provided.
    """
    global summons_data
    plate_filter = request.args.get("plate")
    unique_summons = {}

    with lock:
        for plate in detected_plates:
            # ✅ If filter is applied, skip other plates
            if plate_filter and plate["plate"] != plate_filter:
                continue

            summons_status = check_summons_status(plate["plate"])
            if summons_status and summons_status != "Error":
                for summon in summons_status:
                    if summon["noticeNo"] not in unique_summons:
                        summon["plate"] = plate["plate"]
                        summon["latitude"] = plate["latitude"]
                        summon["longitude"] = plate["longitude"]
                        summon["snapshot"] = plate["snapshot"]
                        summon["officer_id"] = plate.get("officer_id", stored_officer_id)
                        unique_summons[summon["noticeNo"]] = summon

    summons_data = list(unique_summons.values())  # Store summons globally

    print(f"📌 API Returning Summons Data (filter={plate_filter}):", summons_data)
    return jsonify(summons_data)

@app.route("/api/received-plates", methods=["GET"])
def get_received_plates():
    with lock:
        return jsonify(list(reversed(detected_plates)))

@app.route("/download/excel/detected_plates", methods=["GET"])
def download_detected_plates_excel():
    with lock:
        if not detected_plates:
            return "No data available", 400

        df = pd.DataFrame(detected_plates)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name="Detected Plates")
        output.seek(0)

        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name="detected_plates.xlsx")

@app.route("/download/pdf/detected_plates", methods=["GET"])
def download_detected_plates_pdf():
    with lock:
        if not detected_plates:
            return "No data available", 400

        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=10, rightMargin=10, topMargin=20, bottomMargin=20)
        elements = []

        styles = getSampleStyleSheet()
        title = Paragraph("<b>Detected Plates Report</b>", styles["Title"])
        elements.append(title)

        # Table Headers
        data = [["License Plate", "Status", "Time", "Snapshot"]]

        # Table Rows
        for plate in detected_plates:
            snapshot_path = plate["snapshot"]
            img = Image(snapshot_path, width=100, height=70)  # Adjusted image size
            
            # **Enable word wrapping for Status using Paragraph**
            status_text = Paragraph(plate["status"], styles["Normal"])
            
            data.append([
                plate["plate"],
                status_text,  # Apply word wrapping to the status column
                plate["time"],
                img
            ])

        # Increase column widths for better fit
        col_widths = [100, 120, 120, 120]

        # Create Table
        table = Table(data, colWidths=col_widths)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.blue),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('WORDWRAP', (0, 0), (-1, -1)),  # Enable word wrapping
        ]))

        elements.append(table)
        doc.build(elements)

        buffer.seek(0)

        return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="detected_plates.pdf")
        
@app.route("/download/excel/summons_queue", methods=["GET"])
def download_summons_queue_excel():
    with lock:
        if not summons_data:
            return "No data available", 400

        df = pd.DataFrame(summons_data)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name="Summons Queue")
        output.seek(0)

        return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name="summons_queue.xlsx")

@app.route("/download/pdf/summons_queue", methods=["GET"])
def download_summons_queue_pdf():
    with lock:
        if not summons_data:
            return "No data available", 400

        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=20, rightMargin=20, topMargin=30, bottomMargin=20)
        elements = []
        
        # Title with Centered Alignment
        styles = getSampleStyleSheet()
        title = Paragraph("<b>Summons Queue Report</b>", styles["Title"])
        elements.append(title)

        # Table Headers
        data = [["License Plate", "Notice No", "Offence", "Location", "Date", "Status", "Fine Amount", "Due Date"]]

        # Table Rows
        for summon in summons_data:
            data.append([
                summon["plate"],
                summon["noticeNo"],
                Paragraph(summon["offence"], styles["Normal"]),  # Wrap text properly
                Paragraph(summon["location"], styles["Normal"]), # Wrap text properly
                summon["date"],
                summon["status"],
                summon["amount"],
                summon["due_date"]
            ])

        # **Updated Column Widths**
        col_widths = [60, 90, 180, 150, 70, 70, 70, 70]  # Balanced layout for better fit

        # Create Table
        table = Table(data, colWidths=col_widths)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.blue),  # Header background color
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),  # Header text color
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),  # Center align all text
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),  # Alternate row colors
            ('GRID', (0, 0), (-1, -1), 1, colors.black),  # Borders for all cells
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),  # Align vertically center
            ('FONTSIZE', (0, 0), (-1, -1), 9),  # Reduce font size for better fit
            ('WORDWRAP', (0, 0), (-1, -1)),  # Enable text wrapping for long content
        ]))

        elements.append(table)
        doc.build(elements)

        buffer.seek(0)

        return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="summons_queue.pdf")
        
gps_data_log = []  # Store GPS data temporarily

@app.route("/api/gps", methods=["POST"])
def receive_gps():
    global gps_logs
    data = request.json
    if data:
        # ✅ Inject fixed scan car plate and timestamp
        data["plate"] = "VP1728"
        data["time"] = data.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        gps_logs.append(data)

        try:
            db = get_db()
            cursor = db.cursor()
            cursor.execute("""
                INSERT INTO gps_history (plate, timestamp, latitude, longitude, speed)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                data.get("plate"),
                data.get("time"),
                data.get("latitude"),
                data.get("longitude"),
                data.get("speed", 0)
            ))
            db.commit()
            cursor.close()
            print("✅ GPS saved to DB")
        except Exception as e:
            print("❌ Failed to insert GPS into DB:", e)

        # ✅ This must be OUTSIDE the try block, no extra indent
        if len(gps_logs) > 1000:
            gps_logs.pop(0)

        send_gps_to_dashboard(data)
        print(f"📡 GPS Data Received: {data}")
        return jsonify({"status": "success"}), 200

    return jsonify({"error": "No data received"}), 400

@app.route("/api/gps/logs", methods=["GET"])
def get_gps_logs():
    return jsonify(gps_logs)  # Return logged GPS data

@app.route("/api/gps-latest", methods=["GET"])
def gps_latest():
    """
    Returns the most recent GPS fix.
    Prefers a record received via /api/gps (gps_logs), falls back to the live
    GNSS reading kept in latest_gps (from gps_updater thread).
    """
    # Prefer last pushed GPS from /api/gps
    if gps_logs:
        last = gps_logs[-1]
        return jsonify({
            "latitude": last.get("latitude"),
            "longitude": last.get("longitude"),
            "speed": last.get("speed", 0),
            "timestamp": last.get("time"),
            "detected": True
        }), 200

    # Fall back to background GPS thread state
    lat = latest_gps.get("latitude")
    lon = latest_gps.get("longitude")
    ts  = latest_gps.get("last_update")
    if lat is not None and lon is not None:
        ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None
        return jsonify({
            "latitude": lat,
            "longitude": lon,
            "speed": None,
            "timestamp": ts_str,
            "detected": True
        }), 200

    # No fix yet
    return jsonify({"detected": False}), 200

@app.route("/api/gps-status", methods=["GET"])
def gps_status():
    """
    Returns detected=True/False and how many seconds since the last fix.
    """
    now = time.time()
    last = latest_gps.get("last_update")
    detected = last is not None and (now - last) < 30   # consider fresh if < 30s old
    age = (now - last) if last else None
    return jsonify({"detected": detected, "age_seconds": round(age, 1) if age else None}), 200

@app.route("/api/payment/generate-qr", methods=["POST"])
def generate_qr():
    data = request.json
    if not data or "totalAmount" not in data or "summons" not in data:
        return jsonify({"error": "Missing required data"}), 400

    total_amount = data["totalAmount"]
    summons = data["summons"]

    try:
        response = requests.post(
            "http://localhost:5000/api/payment/generate-qr",  # ✅ Node.js API endpoint
            json={
                "totalAmount": total_amount,
                "summons": summons
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": "2c76ee72a2e68a54e6e73ba360c6f1f41de42cb8c2235f645705ce1f834d7122"  # ✅ Replace with your actual token if needed
            },
            timeout=10
        )

        print("📥 Payment API Response:", response.text)
        return jsonify(response.json()), response.status_code

    except requests.exceptions.RequestException as e:
        print("❌ Payment request failed:", e)
        return jsonify({"error": "Failed to generate payment QR"}), 500


@app.route("/gps-tracking", methods=["GET"])
def get_gps_tracking():
    if gps_logs:
        latest_gps = gps_logs[-1]  # ✅ Get last received GPS log
        return jsonify(latest_gps)
    return jsonify({"error": "No GPS data available"}), 404  # ✅ Return proper error message

@app.route("/gps-tracking-history", methods=["GET"])
def gps_tracking_history():
    plate = request.args.get("plate")
    start = request.args.get("start")
    end = request.args.get("end")

    filtered = gps_logs
    if plate:
        filtered = [g for g in filtered if g.get("plate") == plate]
    if start and end:
        filtered = [g for g in filtered if start <= g.get("time", "") <= end]

    formatted = [{
        "latitude": g["latitude"],
        "longitude": g["longitude"],
        "time": g["time"],
        "speed": g.get("speed", 0)
    } for g in filtered if "latitude" in g and "longitude" in g]

    return jsonify(formatted)  # ✅ Correctly indented now


@app.route("/queue-summons")
def redirect_to_dashboard_summons():
    plate = request.args.get("plate")
    if not plate:
        return "Missing plate number", 400
    return redirect(f"/?plate={plate}&view=summons-payment")

@app.route("/qr-payment")
def qr_payment_view():
    url = request.args.get("url")
    return render_template("qr_payment.html", qr_url=url)

@app.route("/summons-payment")
def summons_payment_page():
    plate = (request.args.get("plate") or "").strip().upper()
    summons = get_latest_summons_for_plate(plate)

    # Optional fallback: if DB has nothing, hit the Node API live once, then cache minimal row
    if not summons and plate:
        print(f"ℹ️ No DB summons for {plate}; calling Node API fallback...")
        summons = check_summons_status(plate)
        try:
            db = get_db()
            with db.cursor() as cur:
                cur.execute("""
                    INSERT INTO detected_plates
                        (plate, timestamp, image_path, latitude, longitude, officer_id, status, summons_total, summons_json)
                    VALUES
                        (%s, NOW(), %s, %s, %s, %s, %s, %s, %s)
                """, (
                    plate,
                    None,         # no snapshot in this fallback insert
                    None, None,   # no GPS in fallback
                    stored_officer_id,
                    ("Unpaid" if summons else "Not Paid"),
                    len(summons) if isinstance(summons, list) else 0,
                    json.dumps(summons or [])
                ))
            db.commit()
            db.close()
        except Exception as e:
            print("❌ Fallback insert failed:", e)

    return render_template("summons_payment.html", plate=plate, summons=summons)


@app.route("/api/lpr-stats", methods=["GET"])
def get_lpr_stats():
    total = api_stats["success_count"] + api_stats["failure_count"]
    average_time = (
        api_stats["total_time"] / api_stats["success_count"]
        if api_stats["success_count"] > 0 else 0
    )
    return jsonify({
        "total_calls": total,
        "successful_calls": api_stats["success_count"],
        "failed_calls": api_stats["failure_count"],
        "average_response_time_sec": round(average_time, 2)
    })

@app.route('/reset-queue', methods=['POST'])
def reset_queue():
    def clear_all():
        global detected_plates
        try:
            # 1. Truncate DB table (faster than DELETE)
            connection = pymysql.connect(
                host='127.0.0.1',
                port=3306,
                user='lpr_user',
                password='vistasummerose',
                database='lpr_system',
                autocommit=True,
                cursorclass=pymysql.cursors.DictCursor
            )
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("TRUNCATE TABLE detected_plates")
                    connection.commit()

            # 2. Clear in-memory queue
            with lock:
                detected_plates.clear()

            # 3. Delete and recreate snapshots folder (faster cleanup)
            snapshot_folder = app.config["SNAPSHOT_FOLDER"]
            if os.path.exists(snapshot_folder):
                shutil.rmtree(snapshot_folder)
            os.makedirs(snapshot_folder)

            print("✅ Reset complete: DB + memory + snapshots cleared.")

        except Exception as e:
            print("❌ Reset failed:", e)

    # Run the clear operation in background so user gets instant response
    Thread(target=clear_all).start()

    return jsonify({"status": "success", "message": "Reset started. Data will clear shortly."})

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({"status": "online" if is_connected() else "offline"})

@app.route("/api/receive-plate", methods=["POST"])
def receive_plate():
    data = request.json
    if not data or "plate" not in data:
        return jsonify({"error": "Invalid data"}), 400

    with lock:
        detected_plates.append(data)
    print(f"📥 Plate received via API: {data}")
    return jsonify({"message": "Plate received"}), 200

def sync_offline_data():
    if not is_connected():
        return

    path = Path(OFFLINE_FILE)
    if not path.exists():
        return

    with open(path, "r") as f:
        try:
            queue = json.load(f)
        except json.JSONDecodeError:
            queue = []

    successful = []
    for item in queue:
        try:
            if item["type"] == "plate":
                res = requests.post("http://52.163.74.67:5002/api/receive-plate", json=item["data"], timeout=5)
            elif item["type"] == "gps":
                res = requests.post("http://52.163.74.67:5002/api/gps", json=item["data"], timeout=5)
            else:
                continue

            if res.status_code == 200:
                successful.append(item)
        except:
            continue

    remaining = [q for q in queue if q not in successful]
    with open(OFFLINE_FILE, "w") as f:
        json.dump(remaining, f, indent=2)

def start_sync_loop():
    def loop():
        while True:
            sync_offline_data()
            time.sleep(30)
    threading.Thread(target=loop, daemon=True).start()

# Start offline sync loop
start_sync_loop()

@app.route('/start-all', methods=['POST'])
def start_all_services():
    try:
        # DO NOT start this same file again
        # subprocess.Popen(['python3', '/home/lpr/Desktop/lpr-project/live_detection_service/lpr.py'],
        #                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        subprocess.Popen(['python3', '/home/lpr/Desktop/lpr-project/live_detection_service/gps_tracker.py'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(['node', '/home/lpr/Desktop/lpr-project/live_detection_service/server.js'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(['python3', '/home/lpr/Desktop/lpr-project/dashboard_service/dashboard.py'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"message": "✅ All services started successfully!"})
    except Exception as e:
        return jsonify({"message": f"❌ Error starting services: {e}"}), 500


@app.route('/stop-all', methods=['POST'])
def stop_all_services():
    try:
        os.system("pkill -f /home/lpr/Desktop/lpr-project/live_detection_service/lpr.py")
        os.system("pkill -f /home/lpr/Desktop/lpr-project/live_detection_service/gps_tracker.py")
        os.system("pkill -f /home/lpr/Desktop/lpr-project/live_detection_service/server.js")
        os.system("pkill -f /home/lpr/Desktop/lpr-project/dashboard_service/dashboard.py")
        return jsonify({"message": "✅ All services stopped successfully!"})
    except Exception as e:
        return jsonify({"message": f"❌ Error stopping services: {e}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
