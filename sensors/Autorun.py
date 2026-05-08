import serial
import pynmea2
import time
import board
import adafruit_dht
from RPLCD.i2c import CharLCD
import firebase_admin
from firebase_admin import credentials, firestore
import os
import sys

# ================================================================
#  CONFIGURATION — edit only these values if anything changes
# ================================================================
FIREBASE_KEY     = "serviceAccountKey.json"
GPS_PORT         = "/dev/serial0"
GPS_BAUD         = 9600
DHT_PIN          = board.D4
LCD_ADDRESS      = 0x3F
UPLOAD_INTERVAL  = 10   # seconds between Firebase uploads
# ================================================================

# ---------------- FIREBASE INIT ----------------
firebase_connected = False
db = None

try:
    if os.path.exists(FIREBASE_KEY):
        cred = credentials.Certificate(FIREBASE_KEY)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        firebase_connected = True
        print("[Firebase] Connected ✅")
    else:
        print("[Firebase] Key file not found — running without Firebase ⏭")
except Exception as e:
    print(f"[Firebase] Init failed — running without Firebase: {e}")

# ---------------- UPLOAD FUNCTION ----------------
def upload_to_firebase(temp, hum, lat, lon, speed, satellites):
    global firebase_connected
    if not firebase_connected or db is None:
        print("[Firebase] Skipped — not connected ⏭")
        return
    try:
        data = {
            "temperature" : temp,
            "humidity"    : hum,
            "latitude"    : lat,
            "longitude"   : lon,
            "speed_kmh"   : round(speed, 2),
            "satellites"  : satellites,
            "timestamp"   : firestore.SERVER_TIMESTAMP
        }
        db.collection("vehicle_data").add(data)
        print("[Firebase] Uploaded ✅")
    except Exception as e:
        print(f"[Firebase] Upload failed — no internet? : {e}")
        firebase_connected = False  # stop retrying until next restart

# ---------------- LCD INIT ----------------
lcd = None
try:
    lcd = CharLCD('PCF8574', LCD_ADDRESS, cols=16, rows=2, backlight_enabled=True)
    lcd.clear()
    lcd.write_string("IoT Tracker")
    lcd.cursor_pos = (1, 0)
    lcd.write_string("Starting...")
    print("[LCD] Connected ✅")
except Exception as e:
    print(f"[LCD] Failed to connect: {e}")

def lcd_write(line1="", line2=""):
    if lcd is None:
        return
    try:
        lcd.clear()
        lcd.write_string(str(line1)[:16])
        lcd.cursor_pos = (1, 0)
        lcd.write_string(str(line2)[:16])
    except Exception as e:
        print(f"[LCD] Write error: {e}")

# ---------------- DHT11 INIT ----------------
dht = None
try:
    dht = adafruit_dht.DHT11(DHT_PIN, use_pulseio=False)
    print("[DHT11] Connected ✅")
except Exception as e:
    print(f"[DHT11] Failed to connect: {e}")

# ---------------- GPS INIT ----------------
gps_serial = None
try:
    gps_serial = serial.Serial(GPS_PORT, GPS_BAUD, timeout=1)
    print("[GPS] Connected ✅")
except Exception as e:
    print(f"[GPS] Failed to connect: {e}")
    lcd_write("GPS Error", str(e)[:16])

# ---------------- STARTUP SUMMARY ----------------
print("=" * 50)
print("  IoT Tracker — GPS + DHT11 + LCD + Firebase")
print("=" * 50)
print(f"  Firebase : {'✅ Connected'   if firebase_connected else '❌ Offline'}")
print(f"  LCD      : {'✅ Connected'   if lcd        is not None else '❌ Failed'}")
print(f"  DHT11    : {'✅ Connected'   if dht        is not None else '❌ Failed'}")
print(f"  GPS      : {'✅ Connected'   if gps_serial is not None else '❌ Failed'}")
print("=" * 50)

if gps_serial is None:
    print("[FATAL] GPS not available — exiting")
    sys.exit(1)

# ---------------- MAIN VARIABLES ----------------
satellites_visible = 0
fix_count          = 0
last_upload_time   = 0
temp               = None
hum                = None

# ---------------- MAIN LOOP ----------------
try:
    while True:

        # -------- DHT READ --------
        if dht is not None:
            try:
                temp = dht.temperature
                hum  = dht.humidity
            except RuntimeError:
                pass  # DHT11 occasional read fail is normal

        # -------- GPS READ --------
        try:
            raw  = gps_serial.readline()
            line = raw.decode('utf-8', errors='ignore').strip()
        except Exception as e:
            print(f"[GPS] Read error: {e}")
            time.sleep(1)
            continue

        if not line:
            continue

        # -------- SATELLITE COUNT --------
        if line.startswith('$GPGSV') or line.startswith('$GNGSV'):
            try:
                parts = line.split(',')
                satellites_visible = int(parts[3]) if parts[3] else 0
            except:
                pass

        # -------- LOCATION + SPEED --------
        if line.startswith('$GPRMC') or line.startswith('$GNRMC'):
            try:
                msg = pynmea2.parse(line)

                if msg.status == 'A':  # valid GPS fix
                    fix_count += 1
                    lat       = msg.latitude
                    lon       = msg.longitude
                    speed_kmh = float(msg.spd_over_grnd) * 1.852 if msg.spd_over_grnd else 0.0

                    # -------- TERMINAL --------
                    print("\n" + "=" * 40)
                    print(f"  GPS FIX #{fix_count}")
                    print("=" * 40)
                    print(f"  Lat        : {lat:.6f}")
                    print(f"  Lon        : {lon:.6f}")
                    print(f"  Speed      : {speed_kmh:.2f} km/h")
                    print(f"  Temp       : {temp} C")
                    print(f"  Humidity   : {hum} %")
                    print(f"  Satellites : {satellites_visible}")
                    print("=" * 40)

                    # -------- FIREBASE --------
                    now = time.time()
                    if now - last_upload_time >= UPLOAD_INTERVAL:
                        if temp is not None and hum is not None:
                            upload_to_firebase(temp, hum, lat, lon, speed_kmh, satellites_visible)
                            last_upload_time = now
                        else:
                            print("[Firebase] Skipped — DHT not ready ⏭")

                    # -------- LCD PAGE 1 — Temp & Humidity --------
                    lcd_write(f"Temp:{temp}C", f"Hum:{hum}%")
                    time.sleep(3)

                    # -------- LCD PAGE 2 — GPS Coordinates --------
                    lcd_write(f"Lat:{round(lat,4)}", f"Lon:{round(lon,4)}")
                    time.sleep(3)

                    # -------- LCD PAGE 3 — Speed & Satellites --------
                    lcd_write(f"Spd:{speed_kmh:.1f}kmh", f"Sat:{satellites_visible}")
                    time.sleep(3)

                else:
                    # No GPS fix yet
                    print(f"[GPS] Searching... Sats:{satellites_visible}")
                    lcd_write("GPS Searching...", f"Sats:{satellites_visible}")

            except pynmea2.ParseError:
                pass

except KeyboardInterrupt:
    print("\n[STOPPED] Script stopped by user")

finally:
    # -------- CLEAN SHUTDOWN --------
    print("[CLEANUP] Shutting down cleanly...")
    if lcd is not None:
        try:
            lcd.clear()
            lcd.write_string("System Stopped")
        except:
            pass
    if gps_serial is not None:
        gps_serial.close()
    if dht is not None:
        dht.exit()
    print("[CLEANUP] Done ✅")