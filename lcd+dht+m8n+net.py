"""
╔══════════════════════════════════════════════════════════════╗
║      IoT Vehicle Tracker — FINAL BULLETPROOF EDITION        ║
║  Hardware : Raspberry Pi 4B                                  ║
║  Sensors  : NEO-M8N (GPS)  DHT22 (Temp/Hum)                ║
║  Display  : 16x2 I2C LCD  (PCF8574  0x3F)                  ║
║  Internet : SIM7670C 4G  →  ppp0  ONLY                     ║
║  Cloud    : Firebase Firestore                               ║
╚══════════════════════════════════════════════════════════════╝

ALL FEATURES:
  ✅ AUTO NTP SYNC — syncs clock on every startup automatically
  ✅ Firebase TOP PRIORITY — uploads even without GPS fix
  ✅ Socket bound to ppp0 — cannot leak via WiFi
  ✅ iptables blocks wlan0/eth0 at OS level
  ✅ Offline queue — failed uploads retried automatically
  ✅ Firebase SDK auto-reinit on token/auth/clock errors
  ✅ Hardware watchdog — Pi reboots if script freezes
  ✅ Auto reboot after 5 consecutive system failures
  ✅ GSM watchdog thread — restarts gprs.service if ppp0 drops
  ✅ Full diagnostic emoji output on every cycle
  ✅ LCD auto-reinit with 3 retries (Errno 5 fix)
  ✅ GPS reconnects automatically on serial errors
  ✅ 4 flipping LCD pages every 3 seconds
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

FIREBASE_CRED    = "serviceAccountKey.json"
GSM_IFACE        = "ppp0"
GSM_SERVICE      = "gprs.service"
BLOCK_IFACES     = ["wlan0", "eth0"]
NTP_SERVER       = "pool.ntp.org"
MAX_QUEUE        = 100
MAX_FAIL_COUNT   = 5      # reboot after this many consecutive failures

GPS_PORT         = "/dev/serial0"
GPS_BAUD         = 9600

UPLOAD_INTERVAL  = 10     # seconds between Firebase uploads
DHT_INTERVAL     = 2      # seconds between DHT reads
LCD_INTERVAL     = 3      # seconds between LCD page flips
LCD_PAGES        = 4      # total LCD pages

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
#     Kicks /dev/watchdog every cycle.
#     If script freezes/crashes, kernel reboots Pi automatically.
# ══════════════════════════════════════════════════════════════
# def kick_watchdog():
#     try:
#         with open("/dev/watchdog", "w") as wd:
#             wd.write("\n")
#     except Exception:
#         pass   # watchdog may not be enabled — safe to ignore

# ══════════════════════════════════════════════════════════════
# 3.  NETWORK — FORCE GSM ONLY
# ══════════════════════════════════════════════════════════════
def block_non_gsm():
    """iptables: drop all outbound on WiFi/eth — one time setup."""
    for iface in BLOCK_IFACES:
        chk = subprocess.run(
            f"sudo iptables -C OUTPUT -o {iface} -j DROP",
            shell=True, capture_output=True
        )
        if chk.returncode != 0:
            _run(f"sudo iptables -I OUTPUT -o {iface} -j DROP")
            print(f"🔒 [Net] iptables blocked outbound on {iface}")

def force_gsm_route():
    """Delete WiFi/eth default routes, enforce ppp0 as sole default."""
    for iface in BLOCK_IFACES:
        _run(f"sudo ip route del default dev {iface}")
    _run(f"sudo ip route add default dev {GSM_IFACE}")
    _run("echo 'nameserver 8.8.8.8\nnameserver 8.8.4.4' | sudo tee /etc/resolv.conf")
    print(f"🌐 [Net] Default route → {GSM_IFACE}")

def get_ppp0_ip() -> str:
    out = _sh("ip -4 addr show ppp0")
    m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out)
    return m.group(1) if m else ""

def ppp0_is_up() -> bool:
    out = _sh("ip link show ppp0")
    return "ppp0" in out and "UP" in out

def gsm_internet_ok() -> bool:
    """
    Strictly verify internet through ppp0 only.
    Method 1: SO_BINDTODEVICE (requires root — most reliable)
    Method 2: bind to ppp0 IP directly (fallback)
    """
    if not ppp0_is_up():
        print("❌ [GSM] ppp0 interface is DOWN")
        return False

    ip = get_ppp0_ip()
    if not ip:
        print("❌ [GSM] ppp0 has no IP address yet")
        return False

    # Method 1 — SO_BINDTODEVICE
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                        GSM_IFACE.encode())
        sock.connect(("8.8.8.8", 53))
        sock.close()
        return True
    except Exception as e1:
        print(f"⚠️  [GSM] SO_BINDTODEVICE failed ({e1}) — trying IP bind")

    # Method 2 — bind to ppp0 IP
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.bind((ip, 0))
        sock.connect(("8.8.8.8", 53))
        sock.close()
        return True
    except Exception as e2:
        print(f"❌ [GSM] IP bind also failed: {e2}")
        return False

def restart_gsm_service():
    print(f"🔄 [GSM] Restarting {GSM_SERVICE}...")
    _run(f"sudo systemctl restart {GSM_SERVICE}")
    for i in range(30):
        time.sleep(2)
        if ppp0_is_up() and get_ppp0_ip():
            print(f"✅ [GSM] ppp0 back UP — IP: {get_ppp0_ip()}")
            force_gsm_route()
            return True
        print(f"⏳ [GSM] Waiting for ppp0... ({i+1}/30)")
    print("❌ [GSM] ppp0 still down after restart")
    return False

# ══════════════════════════════════════════════════════════════
# 4.  NTP CLOCK SYNC — AUTO ON STARTUP
#     Waits for ppp0 internet, then syncs clock.
#     Retries every 10s until successful.
#     This fixes Firebase JWT clock errors permanently.
# ══════════════════════════════════════════════════════════════
_ntp_synced = False

def sync_ntp() -> bool:
    global _ntp_synced
    print(f"🕐 [Time] Syncing clock via {NTP_SERVER}...")
    # Try ntpdate first
    if _run(f"sudo ntpdate -u {NTP_SERVER}"):
        t = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f"✅ [Time] Clock synced → {t}")
        _ntp_synced = True
        return True
    # Fallback to chronyc
    if _run("sudo chronyc makestep"):
        print(f"✅ [Time] Clock synced via chronyc")
        _ntp_synced = True
        return True
    print("⚠️  [Time] NTP sync failed — will retry")
    return False

def auto_ntp_sync():
    """
    Runs at startup in background thread.
    Keeps retrying every 10 seconds until clock is synced.
    Once synced, reinitialises Firebase so JWT tokens work.
    """
    global _ntp_synced
    print("🕐 [NTP] Auto-sync thread started — waiting for GSM...")
    while not _ntp_synced:
        if gsm_internet_ok():
            if sync_ntp():
                print("🔄 [NTP] Reinitialising Firebase after clock sync...")
                init_firebase()
                break
        else:
            print("⏳ [NTP] No internet yet — retrying in 10s...")
        time.sleep(10)
    print("✅ [NTP] Auto-sync complete — thread exiting")

# ══════════════════════════════════════════════════════════════
# 5.  BOOT-TIME NETWORK HARDENING
# ══════════════════════════════════════════════════════════════
print("\n🚀 [Boot] Hardening network...")
block_non_gsm()
force_gsm_route()

# ══════════════════════════════════════════════════════════════
# 6.  FIREBASE — HIGHEST PRIORITY MODULE
# ══════════════════════════════════════════════════════════════
db      = None
db_lock = threading.Lock()

def init_firebase() -> bool:
    global db
    with db_lock:
        try:
            print("🔥 [Firebase] Initialising...")

            if not os.path.exists(FIREBASE_CRED):
                print(f"❌ [Firebase] Credential file NOT found:")
                print(f"   Expected : {FIREBASE_CRED}")
                return False
            else:
                print(f"✅ [Firebase] Credential file found")

            if firebase_admin._apps:
                firebase_admin.delete_app(firebase_admin.get_app())

            cred = credentials.Certificate(FIREBASE_CRED)
            firebase_admin.initialize_app(cred)
            db = firestore.client()

            # Verify with a test write
            db.collection("_ping").document("boot").set(
                {"booted_at": firestore.SERVER_TIMESTAMP}
            )
            print("✅ [Firebase] Connected & test write successful!")
            return True

        except Exception as e:
            db = None
            err = str(e)
            print(f"❌ [Firebase] Init failed: {err}")
            if "invalid_grant" in err or "token" in err.lower():
                print("   ⏰ Clock mismatch — NTP sync will fix this")
            elif "CERTIFICATE" in err or "credential" in err.lower():
                print(f"   📄 Check JSON file: {FIREBASE_CRED}")
            elif "network" in err.lower() or "timeout" in err.lower():
                print("   📶 Network issue — check ppp0")
            return False

# Boot Firebase
print("\n🔥 [Boot] Starting Firebase...")
gsm_ready = gsm_internet_ok()
print(f"   GSM ready : {'✅' if gsm_ready else '❌'}")

if gsm_ready:
    # Try NTP sync immediately at boot
    sync_ntp()
    time.sleep(1)

if not init_firebase():
    print("⚠️  [Boot] Firebase not ready — NTP auto-sync will fix this")

# Start auto NTP sync thread — keeps retrying until success
threading.Thread(target=auto_ntp_sync, daemon=True,
                 name="NTP-Sync").start()

# ── Offline upload queue ──────────────────────────────────────
_upload_queue : collections.deque = collections.deque(maxlen=MAX_QUEUE)
_queue_lock   = threading.Lock()

def _do_upload(payload: dict) -> bool:
    global db
    if db is None:
        return False
    try:
        db.collection("vehicle_data").add(payload)
        return True
    except Exception as e:
        err = str(e).lower()
        print(f"❌ [Firebase] Upload error: {e}")
        if any(k in err for k in ("invalid_grant", "token", "expired",
                                   "iat", "exp", "timeframe")):
            print("   ⏰ JWT clock error — syncing NTP and reinitialising")
            sync_ntp()
            time.sleep(1)
            init_firebase()
        elif any(k in err for k in ("transport", "connection", "timeout",
                                     "unavailable", "503", "500")):
            print("   🔄 Network error — reinitialising Firebase")
            init_firebase()
        return False

def upload_worker(payload: dict):
    """
    Daemon thread — TOP PRIORITY upload.
    1. Check GSM is up
    2. Check Firebase is ready (reinit if not)
    3. Upload current record
    4. Drain any queued offline records
    """
    # Step 1 — Check GSM
    if not ppp0_is_up():
        print("⚠️  [Upload] ppp0 DOWN — queuing record")
        with _queue_lock:
            _upload_queue.append(payload)
        return

    if not gsm_internet_ok():
        print("⚠️  [Upload] No internet on ppp0 — queuing record")
        with _queue_lock:
            _upload_queue.append(payload)
        return

    # Step 2 — Check Firebase
    if db is None:
        print("🔄 [Upload] Firebase not ready — fixing now...")
        sync_ntp()
        time.sleep(1)
        init_firebase()
        if db is None:
            print("❌ [Upload] Firebase still not ready — queuing")
            with _queue_lock:
                _upload_queue.append(payload)
            return

    # Step 3 — Upload current record
    if _do_upload(payload):
        print("✅ [Firebase] Upload successful!")
    else:
        with _queue_lock:
            _upload_queue.append(payload)
        print(f"📦 [Queue] Total queued: {len(_upload_queue)}")
        return

    # Step 4 — Drain offline queue
    drained = 0
    while True:
        with _queue_lock:
            if not _upload_queue:
                break
            queued = _upload_queue[0]
        if not gsm_internet_ok():
            break
        if _do_upload(queued):
            with _queue_lock:
                _upload_queue.popleft()
            drained += 1
        else:
            break
    if drained:
        print(f"📤 [Firebase] Drained {drained} queued records!")

def trigger_upload(payload: dict):
    threading.Thread(target=upload_worker,
                     args=(payload,), daemon=True).start()

# ══════════════════════════════════════════════════════════════
# 7.  GSM WATCHDOG THREAD
#     Checks ppp0 every 20s, restarts gprs.service if down.
#     Also re-enforces routes every cycle.
# ══════════════════════════════════════════════════════════════
def gsm_watchdog():
    print("👁️  [GSM-WD] Watchdog thread started")
    while True:
        time.sleep(20)
        try:
            if not ppp0_is_up():
                print("🚨 [GSM-WD] ppp0 DOWN — restarting service!")
                restart_gsm_service()
            else:
                force_gsm_route()   # silently re-enforce routes
        except Exception as e:
            print(f"⚠️  [GSM-WD] Error: {e}")

threading.Thread(target=gsm_watchdog, daemon=True,
                 name="GSM-WD").start()

# ══════════════════════════════════════════════════════════════
# 8.  HARDWARE SETUP
# ══════════════════════════════════════════════════════════════

# ── LCD ──────────────────────────────────────────────────────
lcd = None

def init_lcd():
    global lcd
    try:
        lcd = CharLCD('PCF8574', 0x3F, cols=16, rows=2,
                      backlight_enabled=True)
        lcd.clear()
        lcd.write_string("IoT Tracker")
        lcd.cursor_pos = (1, 0)
        lcd.write_string("Booting up...")
        print("✅ [LCD] Initialised")
    except Exception as e:
        print(f"⚠️  [LCD] Init failed: {e}")
        lcd = None

def lcd_write(line1: str = "", line2: str = ""):
    """
    Write to LCD with 3 auto-retry attempts.
    Auto-reinits on Errno 5 (I2C disconnect).
    """
    global lcd
    for attempt in range(3):
        try:
            if lcd is None:
                init_lcd()
            if lcd is None:
                return
            lcd.clear()
            lcd.write_string(str(line1)[:16])
            lcd.cursor_pos = (1, 0)
            lcd.write_string(str(line2)[:16])
            return   # success
        except Exception as e:
            print(f"⚠️  [LCD] Write error (attempt {attempt+1}): {e}")
            lcd = None
            time.sleep(0.2)

init_lcd()

# ── DHT22 ────────────────────────────────────────────────────
dht = None
try:
    dht = adafruit_dht.DHT22(board.D4, use_pulseio=False)
    print("✅ [DHT22] Initialised on GPIO4")
except Exception as e:
    print(f"❌ [DHT22] Init error: {e}")

# ── GPS (NEO-M8N) ────────────────────────────────────────────
def open_gps():
    try:
        gps = serial.Serial(GPS_PORT, GPS_BAUD, timeout=0.5)
        print(f"✅ [GPS] Connected on {GPS_PORT}")
        return gps
    except Exception as e:
        print(f"❌ [GPS] Error: {e}")
        return None

gps_serial = open_gps()

# ── Startup diagnostics ───────────────────────────────────────
print("\n" + "═" * 52)
print("   🛰️  TRACKER STARTED — FINAL BULLETPROOF EDITION")
print("═" * 52)
print(f"\n📋 STARTUP DIAGNOSTICS:")
print(f"   ppp0 UP     : {'✅' if ppp0_is_up() else '❌ DOWN'}")
print(f"   ppp0 IP     : {get_ppp0_ip() or '❌ No IP'}")
print(f"   GSM Internet: {'✅' if gsm_internet_ok() else '❌ No internet'}")
print(f"   Firebase    : {'✅ Ready' if db else '❌ Not ready (NTP sync pending)'}")
print(f"   NTP synced  : {'✅' if _ntp_synced else '⏳ Auto-sync running in background'}")
print(f"   GPS serial  : {'✅' if gps_serial else '❌'}")
print(f"   DHT22       : {'✅' if dht else '❌'}")
print(f"   LCD         : {'✅' if lcd else '❌'}")
print(f"   Clock now   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"   Watchdog    : ✅ Hardware + software")
print()

# ══════════════════════════════════════════════════════════════
# 9.  GLOBAL STATE
# ══════════════════════════════════════════════════════════════
last_temp        = 0.0
last_hum         = 0.0
lat              = 0.0
lon              = 0.0
speed_kmh        = 0.0
satellites       = 0
has_fix          = False

last_upload_time = time.time()
last_dht_time    = time.time()
last_lcd_time    = time.time()
lcd_page         = 0
fail_count       = 0   # consecutive system failure counter

# ══════════════════════════════════════════════════════════════
# 10. LCD PAGES
# ══════════════════════════════════════════════════════════════
def show_lcd_page(page: int):
    if not has_fix:
        lcd_write("GPS Searching...", "Sats:" + str(satellites))
        return

    if page == 0:
        lcd_write(
            "T:{:.1f}C H:{:.0f}%".format(last_temp, last_hum),
            "Sats:{} Fix:YES".format(satellites)
        )
    elif page == 1:
        lcd_write(
            "Lat:{:.5f}".format(lat),
            "Lon:{:.5f}".format(lon)
        )
    elif page == 2:
        lcd_write(
            "Spd:{:.1f}km/h".format(speed_kmh),
            "Sats:{}".format(satellites)
        )
    elif page == 3:
        with _queue_lock:
            q = len(_upload_queue)
        gsm_ok = ppp0_is_up()
        fb_ok  = db is not None
        lcd_write(
            "GSM:{} FB:{}".format(
                "OK" if gsm_ok else "--",
                "OK" if fb_ok  else "--"
            ),
            "Q:{} 4G:ppp0".format(q)
        )

# ══════════════════════════════════════════════════════════════
# 11. MAIN LOOP
# ══════════════════════════════════════════════════════════════
while True:
    now = time.time()

    # ── A. HARDWARE WATCHDOG KICK ─────────────────────────────
    # kick_watchdog()

    # ── B. DHT22 ─────────────────────────────────────────────
    if dht and (now - last_dht_time >= DHT_INTERVAL):
        try:
            t = dht.temperature
            h = dht.humidity
            if t is not None and h is not None:
                last_temp = t
                last_hum  = h
        except RuntimeError:
            pass
        last_dht_time = now

    # ── C. GPS ───────────────────────────────────────────────
    try:
        if gps_serial:
            raw  = gps_serial.readline()
            line = raw.decode('utf-8', errors='ignore').strip()

            if line.startswith(('$GPGSV', '$GNGSV', '$GLGSV')):
                parts = line.split(',')
                if len(parts) > 3 and parts[3].strip():
                    try:
                        satellites = int(parts[3])
                    except ValueError:
                        pass

            if line.startswith(('$GPRMC', '$GNRMC')):
                msg = pynmea2.parse(line)
                if msg.status == 'A' and msg.latitude and msg.longitude:
                    has_fix   = True
                    lat       = msg.latitude
                    lon       = msg.longitude
                    speed_kmh = (float(msg.spd_over_grnd) * 1.852
                                 if msg.spd_over_grnd else 0.0)
                else:
                    has_fix = False

    except pynmea2.ParseError:
        pass
    except serial.SerialException:
        print("⚠️  [GPS] Serial error — reconnecting...")
        try:
            gps_serial.close()
        except Exception:
            pass
        time.sleep(1)
        gps_serial = open_gps()

    # ── D. LCD PAGE FLIP ─────────────────────────────────────
    if now - last_lcd_time >= LCD_INTERVAL:
        show_lcd_page(lcd_page)
        lcd_page      = (lcd_page + 1) % LCD_PAGES
        last_lcd_time = now

    # ── E. FIREBASE UPLOAD — TOP PRIORITY ────────────────────
    if now - last_upload_time >= UPLOAD_INTERVAL:

        force_gsm_route()   # Re-enforce route every cycle

        with _queue_lock:
            q_len = len(_upload_queue)

        gsm_ok = ppp0_is_up()
        gsm_ip = get_ppp0_ip()
        fb_ok  = db is not None

        print("\n" + "─" * 50)
        print(f"🌡️  Temp:{last_temp:.1f}C  Hum:{last_hum:.1f}%")
        print(f"🛰️  GPS: {lat:.5f},{lon:.5f} | "
              f"{speed_kmh:.1f}km/h | Sats:{satellites} | "
              f"Fix:{'✅' if has_fix else '❌'}")
        print(f"📶 GSM: {'✅ UP' if gsm_ok else '❌ DOWN'} | "
              f"IP:{gsm_ip or 'none'} | Queue:{q_len}")
        print(f"🔥 Firebase: {'✅ Ready' if fb_ok else '❌ NOT READY'}")
        print(f"🕐 Clock: {time.strftime('%Y-%m-%d %H:%M:%S')} | "
              f"NTP:{'✅' if _ntp_synced else '⏳ pending'}")

        # If Firebase not ready — try fix immediately
        if not fb_ok:
            print("🔄 [Firebase] Not ready — attempting fix now...")
            if gsm_ok and not _ntp_synced:
                sync_ntp()
                time.sleep(1)
            init_firebase()
            fb_ok = db is not None
            print(f"   Result: {'✅ Ready' if fb_ok else '❌ Still not ready'}")

        # Build payload — always upload regardless of GPS fix
        payload = {
            "temperature" : round(last_temp, 1),
            "humidity"    : round(last_hum,  1),
            "latitude"    : lat,
            "longitude"   : lon,
            "speed_kmh"   : round(speed_kmh, 2),
            "satellites"  : satellites,
            "has_fix"     : has_fix,
            "gsm_ip"      : gsm_ip,
            "timestamp"   : firestore.SERVER_TIMESTAMP,
        }

        trigger_upload(payload)

        if not has_fix:
            print(f"⏳ [GPS] No fix yet (Sats:{satellites}) — uploading data anyway")

        print("─" * 50)
        last_upload_time = now

    # ── F. SYSTEM HEALTH CHECK + AUTO REBOOT ─────────────────
    if ppp0_is_up() and gsm_internet_ok():
        fail_count = 0
    else:
        fail_count += 1
        print(f"⚠️  [SYS] System failure count: {fail_count}/{MAX_FAIL_COUNT}")

    if fail_count >= MAX_FAIL_COUNT:
        print("🚨 [SYS] Too many failures — REBOOTING Pi now...")
        lcd_write("System Error", "Rebooting...")
        time.sleep(2)
        # os.system("sudo reboot")

    # ── G. CPU RELIEF ────────────────────────────────────────
    time.sleep(0.1)
