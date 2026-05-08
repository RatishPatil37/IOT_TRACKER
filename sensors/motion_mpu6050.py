import smbus

# I2C bus and MPU6050 address
bus = smbus.SMBus(1)
MPU_ADDR = 0x68

def init_mpu6050():
    """Wakes up the MPU6050."""
    try:
        bus.write_byte_data(MPU_ADDR, 0x6B, 0)
        return True
    except Exception:
        return False

def read_raw_data(addr):
    """Helper function to read and format I2C data."""
    high = bus.read_byte_data(MPU_ADDR, addr)
    low = bus.read_byte_data(MPU_ADDR, addr+1)
    value = ((high << 8) | low)
    if value > 32768:
        value = value - 65536
    return value

def check_for_crash(threshold=2.5):
    """Reads acceleration and compares it to a crash threshold."""
    try:
        # Read Y-axis acceleration (0x3D)
        acc_y = read_raw_data(0x3D)
        
        # Convert to G-force (assuming default +/- 2g scale)
        g_force = acc_y / 16384.0 
        
        if abs(g_force) > threshold:
            return {"crash_detected": True, "g_force": round(g_force, 2)}
        return {"crash_detected": False, "g_force": round(g_force, 2)}
        
    except Exception as e:
         return {"crash_detected": False, "g_force": None, "error": str(e)}