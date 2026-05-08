import serial

def get_air_quality(port='/dev/ttyS0', baudrate=9600):
    """Reads PM2.5 and PM10 data from the PMS5003."""
    try:
        pms_serial = serial.Serial(port, baudrate, timeout=2)
        
        # The sensor sends data in 32-byte frames
        data = pms_serial.read(32)
        
        # Check if the frame starts with the correct signature (0x42 0x4d)
        if len(data) == 32 and data[0] == 0x42 and data[1] == 0x4d:
            pm1_0 = (data[10] << 8) + data[11]
            pm2_5 = (data[12] << 8) + data[13]
            pm10  = (data[14] << 8) + data[15]
            
            return {"pm1_0": pm1_0, "pm2_5": pm2_5, "pm10": pm10, "status": "OK"}
            
        return {"pm1_0": None, "pm2_5": None, "pm10": None, "status": "Reading Frame..."}
        
    except Exception as e:
         return {"pm1_0": None, "pm2_5": None, "pm10": None, "status": f"Error: {e}"}