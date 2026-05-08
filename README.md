# 📡 IoT Live Vehicle Tracker (Bulletproof Edition)

![Status](https://img.shields.io/badge/Status-Operational-00ff88?style=for-the-badge&logo=raspberrypi)
![Platform](https://img.shields.io/badge/Hardware-Raspberry%20Pi%204B-C51A4A?style=for-the-badge&logo=raspberrypi)
![Network](https://img.shields.io/badge/Connectivity-4G%20LTE%20(ppp0)-00e5ff?style=for-the-badge)

A mission-critical, real-time IoT tracking system built for maximum uptime and environmental resilience. This "Bulletproof Edition" features hardware-level watchdogs, strict network isolation, and self-healing connectivity.

---

## 🏗️ System Architecture

```mermaid
graph TD
    subgraph "Hardware (Vehicle)"
        GPS[NEO-M8N GPS] --> |Serial| RPi[Raspberry Pi 4B]
        DHT[DHT22 Temp/Hum] --> |GPIO| RPi
        RPi --> |I2C| LCD[16x2 LCD Display]
        RPi --> |USB/Serial| GSM[SIM7670C 4G LTE]
    end

    subgraph "Cloud (Firebase)"
        GSM --> |ppp0 Interface| FS[(Firestore)]
    end

    subgraph "Monitoring (Web Dashboard)"
        FS --> |Real-time Listener| Web[JS Dashboard]
        Web --> |Google Maps API| Map[Live Map Visualization]
    end
```

---

## ⚡ Bulletproof Features

- **🔒 Strict Network Isolation**: Outbound traffic is locked to the `ppp0` interface via `iptables`. Prevents data leakage via WiFi/Ethernet.
- **🛡️ Self-Healing Logic**:
  - **Hardware Watchdog**: Kernel-level `/dev/watchdog` support. If the script freezes, the Pi reboots automatically.
  - **GSM Watchdog**: A background thread monitors `ppp0`. If the connection drops, it automatically restarts the `gprs.service`.
  - **Auto NTP Sync**: Synchronizes the system clock on startup to ensure accurate Firestore timestamps.
- **📦 Offline Persistence**: Failed uploads are queued in a local buffer (up to 100 records) and retried once connectivity is restored.
- **📟 Diagnostic Display**: 4-page flipping LCD interface showing live Coordinates, Network Status, Sensors, and System Health.

---

## 🔄 Logic Flow

```mermaid
flowchart TD
    Start([System Boot]) --> NTP{NTP Sync?}
    NTP -->|No| Wait[Wait for GSM]
    NTP -->|Yes| Main[Start Main Loop]
    
    subgraph "Main Cycle"
        Main --> Sensors[Read GPS & DHT22]
        Sensors --> LCD[Update 16x2 LCD]
        LCD --> NetCheck{Internet OK?}
        
        NetCheck -->|Yes| Push[Push to Firebase]
        NetCheck -->|No| Queue[Add to Offline Queue]
        
        Push --> WD[Kick Hardware Watchdog]
        Queue --> WD
    end
    
    WD -->|Sleep 10s| Main
```

---

## 🛠️ Hardware Requirements

| Component | Description |
| :--- | :--- |
| **Microcontroller** | Raspberry Pi 4B (Recommended) |
| **GPS Module** | NEO-M8N (via `/dev/serial0`) |
| **Sensors** | DHT22 (Temperature & Humidity) |
| **Display** | 16x2 I2C LCD (PCF8574 at 0x3F) |
| **Modem** | SIM7670C 4G LTE (ppp0 connection) |

---

## 🚀 Deployment

### 1. Hardware Setup
- Connect GPS to UART pins.
- Connect DHT22 to GPIO.
- Connect LCD to I2C pins.
- Ensure the SIM7670C is configured as a `ppp0` interface using `wvdial` or `nmcli`.

### 2. Software Installation
```bash
# Clone the repository
git clone https://github.com/RatishPatil37/IOT_TRACKER.git

# Install dependencies
pip install firebase-admin adafruit-circuitpython-dht RPLCD pynmea2
```

### 3. Execution
Run the "Bulletproof" script:
```bash
python3 "Final_code (1).py"
```

---

## 📜 Credits
Developed by **Ratish Patil** for advanced IoT vehicle monitoring and logistics.
