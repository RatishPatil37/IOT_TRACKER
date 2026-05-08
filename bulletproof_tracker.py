"""
╔══════════════════════════════════════════════════════════════╗
║      IoT Vehicle Tracker — FINAL BULLETPROOF EDITION        ║
║  Hardware : Raspberry Pi 4B                                  ║
║  Sensors  : NEO-M8N (GPS)  DHT22 (Temp/Hum)                ║
║  Display  : 16x2 I2C LCD  (PCF8574  0x3F)                  ║
║  Internet : SIM7670C 4G  →  ppp0  ONLY                     ║
║  Cloud    : Firebase Firestore                               ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, re, sys, time, socket, threading, subprocess, collections
import serial, pynmea2, board, adafruit_dht
from RPLCD.i2c import CharLCD
import firebase_admin
from firebase_admin import credentials, firestore

# ══════════════════════════════════════════════════════════════
# 0.  CONFIGURATION
# ══════════════════════════════════════════════════════════════
sys.stdout.reconfigure(encoding='utf-8')

# SECURITY: Use a generic filename. Add this file to your .gitignore!
FIREBASE_CRED    = "serviceAccountKey.json" 

GSM_IFACE        = "ppp0"
GSM_SERVICE      = "gprs.service"
BLOCK_IFACES     = ["wlan0", "eth0"]
NTP_SERVER       = "pool.ntp.org"
MAX_QUEUE        = 100
MAX_FAIL_COUNT   = 5

GPS_PORT         = "/dev/serial0"
GPS_BAUD         = 9600

UPLOAD_INTERVAL  = 10
DHT_INTERVAL     = 2
LCD_INTERVAL     = 3
LCD_PAGES        = 4

# ══════════════════════════════════════════════════════════════
# 1.  HELPERS
# ══════════════════════════════════════════════════════════════
def _run(cmd: str) -> bool:
    return os.system(cmd + " >/dev/null 2>&1") == 0

def _sh(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip()

# ══════════════════════════════════════════════════════════════
# 2.  HARDWARE WATCHDOG
# ══════════════════════════════════════════════════════════════
def kick_watchdog():
    try:
        with open("/dev/watchdog", "w") as wd:
            wd.write("\n")
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════
# 3.  NETWORK — FORCE GSM ONLY
# ══════════════════════════════════════════════════════════════
def block_non_gsm():
    for iface in BLOCK_IFACES:
        chk = subprocess.run(f"sudo iptables -C OUTPUT -o {iface} -j DROP", shell=True, capture_output=True)
        if chk.returncode != 0:
            _run(f"sudo iptables -I OUTPUT -o {iface} -j DROP")

def force_gsm_route():
    for iface in BLOCK_IFACES:
        _run(f"sudo ip route del default dev {iface}")
    _run(f"sudo ip route add default dev {GSM_IFACE}")
    _run("echo 'nameserver 8.8.8.8\nnameserver 8.8.4.4' | sudo tee /etc/resolv.conf")

def get_ppp0_ip() -> str:
    out = _sh("ip -4 addr show ppp0")
    m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out)
    return m.group(1) if m else ""

def ppp0_is_up() -> bool:
    out = _sh("ip link show ppp0")
    return "ppp0" in out and "UP" in out

def gsm_internet_ok() -> bool:
    if not ppp0_is_up(): return False
    ip = get_ppp0_ip()
    if not ip: return False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, GSM_IFACE.encode())
        sock.connect(("8.8.8.8", 53))
        sock.close()
        return True
    except:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.bind((ip, 0))
            sock.connect(("8.8.8.8", 53))
            sock.close()
            return True
        except:
            return False

def restart_gsm_service():
    _run(f"sudo systemctl restart {GSM_SERVICE}")
    for i in range(30): time.sleep(2)

# ══════════════════════════════════════════════════════════════
# 4.  NTP CLOCK SYNC
# ══════════════════════════════════════════════════════════════
_ntp_synced = False
def sync_ntp() -> bool:
    global _ntp_synced
    if _run(f"sudo ntpdate -u {NTP_SERVER}") or _run("sudo chronyc makestep"):
        _ntp_synced = True
        return True
    return False

def auto_ntp_sync():
    global _ntp_synced
    while not _ntp_synced:
        if gsm_internet_ok() and sync_ntp():
            init_firebase()
            break
        time.sleep(10)

# ══════════════════════════════════════════════════════════════
# 5.  FIREBASE
# ══════════════════════════════════════════════════════════════
db = None
db_lock = threading.Lock()

def init_firebase() -> bool:
    global db
    with db_lock:
        try:
            if not os.path.exists(FIREBASE_CRED): return False
            if firebase_admin._apps: firebase_admin.delete_app(firebase_admin.get_app())
            cred = credentials.Certificate(FIREBASE_CRED)
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            db.collection("_ping").document("boot").set({"booted_at": firestore.SERVER_TIMESTAMP})
            return True
        except:
            db = None
            return False

# ── Offline upload queue ──────────────────────────────────────
_upload_queue = collections.deque(maxlen=MAX_QUEUE)
_queue_lock = threading.Lock()

def _do_upload(payload: dict) -> bool:
    global db
    if db is None: return False
    try:
        db.collection("vehicle_data").add(payload)
        return True
    except:
        sync_ntp()
        time.sleep(1)
        init_firebase()
        return False

def upload_worker(payload: dict):
    if not gsm_internet_ok():
        with _queue_lock: _upload_queue.append(payload)
        return
    if db is None:
        init_firebase()
        if db is None:
            with _queue_lock: _upload_queue.append(payload)
            return
    if _do_upload(payload):
        while True:
            with _queue_lock:
                if not _upload_queue: break
                queued = _upload_queue[0]
            if gsm_internet_ok() and _do_upload(queued):
                with _queue_lock: _upload_queue.popleft()
            else: break
    else:
        with _queue_lock: _upload_queue.append(payload)

def trigger_upload(payload: dict):
    threading.Thread(target=upload_worker, args=(payload,), daemon=True).start()

# ══════════════════════════════════════════════════════════════
# 6.  WATCHDOGS & HARDWARE
# ══════════════════════════════════════════════════════════════
def gsm_watchdog():
    while True:
        time.sleep(20)
        if not ppp0_is_up(): restart_gsm_service()
        else: force_gsm_route()

threading.Thread(target=gsm_watchdog, daemon=True).start()
threading.Thread(target=auto_ntp_sync, daemon=True).start()

# ── LCD ──────────────────────────────────────────────────────
lcd = None
def init_lcd():
    global lcd
    try:
        lcd = CharLCD('PCF8574', 0x3F, cols=16, rows=2, backlight_enabled=True)
        lcd.clear()
        lcd.write_string("IoT Tracker")
    except: lcd = None

def lcd_write(line1: str = "", line2: str = ""):
    global lcd
    for attempt in range(3):
        try:
            if lcd is None: init_lcd()
            if lcd is None: return
            lcd.clear()
            lcd.write_string(str(line1)[:16])
            lcd.cursor_pos = (1, 0)
            lcd.write_string(str(line2)[:16])
            return
        except: lcd = None

init_lcd()

# ── DHT22 & GPS ──────────────────────────────────────────────
dht = None
try: dht = adafruit_dht.DHT22(board.D4, use_pulseio=False)
except: pass

def open_gps():
    try: return serial.Serial(GPS_PORT, GPS_BAUD, timeout=0.5)
    except: return None

gps_serial = open_gps()

# ══════════════════════════════════════════════════════════════
# 7.  MAIN LOOP
# ══════════════════════════════════════════════════════════════
last_temp, last_hum, lat, lon, speed_kmh, satellites, has_fix = 0.0, 0.0, 0.0, 0.0, 0.0, 0, False
last_upload_time = last_dht_time = last_lcd_time = time.time()
lcd_page, fail_count = 0, 0

while True:
    now = time.time()
    kick_watchdog()

    if dht and (now - last_dht_time >= DHT_INTERVAL):
        try:
            t, h = dht.temperature, dht.humidity
            if t is not None: last_temp, last_hum = t, h
        except: pass
        last_dht_time = now

    try:
        if gps_serial:
            line = gps_serial.readline().decode('utf-8', errors='ignore').strip()
            if line.startswith(('$GPGSV', '$GNGSV')):
                parts = line.split(',')
                if len(parts) > 3 and parts[3]: satellites = int(parts[3])
            if line.startswith(('$GPRMC', '$GNRMC')):
                msg = pynmea2.parse(line)
                if msg.status == 'A':
                    has_fix, lat, lon = True, msg.latitude, msg.longitude
                    speed_kmh = float(msg.spd_over_grnd) * 1.852 if msg.spd_over_grnd else 0.0
                else: has_fix = False
    except:
        if gps_serial: gps_serial.close()
        gps_serial = open_gps()

    if now - last_lcd_time >= LCD_INTERVAL:
        if not has_fix: lcd_write("GPS Searching...", f"Sats:{satellites}")
        else:
            if lcd_page == 0: lcd_write(f"T:{last_temp:.1f}C H:{last_hum:.0f}%", f"Sats:{satellites} Fix:Y")
            elif lcd_page == 1: lcd_write(f"Lat:{lat:.5f}", f"Lon:{lon:.5f}")
        lcd_page = (lcd_page + 1) % LCD_PAGES
        last_lcd_time = now

    if now - last_upload_time >= UPLOAD_INTERVAL:
        payload = {
            "temperature": round(last_temp, 1), "humidity": round(last_hum, 1),
            "latitude": lat, "longitude": lon, "speed_kmh": round(speed_kmh, 2),
            "satellites": satellites, "has_fix": has_fix, "timestamp": firestore.SERVER_TIMESTAMP
        }
        trigger_upload(payload)
        last_upload_time = now

    if not (ppp0_is_up() and gsm_internet_ok()):
        fail_count += 1
        if fail_count >= MAX_FAIL_COUNT: os.system("sudo reboot")
    else: fail_count = 0
    time.sleep(0.1)
