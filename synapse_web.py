#!/usr/bin/env python3
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import serial
import serial.tools.list_ports
import time
import os
import json
import threading
import logging
import cv2
import math
import numpy as np
from flask import Response
import datetime

# Try to import mediapipe, but make it optional
try:
    import mediapipe as mp
    MEDIAPIPE_AVAILABLE = True
except ImportError:
    MEDIAPIPE_AVAILABLE = False
    print("MediaPipe not available. Gesture detection will be disabled.")

# Try to import joblib for ML models
try:
    import joblib
    JOBLIB_AVAILABLE = True
    
    # Try to load the ML models
    try:
        hypertension_model = joblib.load('sensor_rf_model.pkl')
        print("Hypertension model loaded successfully")
    except Exception as e:
        print(f"Error loading hypertension model: {e}")
        hypertension_model = None
        
    try:
        cardiac_model = joblib.load('cardiac_arrest_model.pkl')
        print("Cardiac arrest model loaded successfully")
    except Exception as e:
        print(f"Error loading cardiac arrest model: {e}")
        cardiac_model = None
        
    try:
        anxiety_model = joblib.load('anxiety_model.pkl')
        anxiety_imputer = joblib.load('anxiety_imputer.pkl')
        print("Anxiety model loaded successfully")
    except Exception as e:
        print(f"Error loading anxiety model: {e}")
        anxiety_model = None
        anxiety_imputer = None
except ImportError:
    JOBLIB_AVAILABLE = False
    hypertension_model = None
    cardiac_model = None
    anxiety_model = None
    anxiety_imputer = None
    print("Joblib not available. Prediction models will be disabled.")

app = Flask(__name__)
app.secret_key = "synapse_ar_secret_key"  # Required for flash messages

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global variables
ser = None
connected = False
available_ports = []
current_port = None
connection_lock = threading.Lock()

# Dedicated Heart Rate Serial Details
hr_ser = None
hr_connected = False
hr_port = None
hr_lock = threading.Lock()
hr_monitoring_thread = None

# Medicine and schedule data
medicines = []
schedule = []
emergency_contact = {"name": "Rishik", "number": "+91 1234567890"}

# Heart rate data
heart_rate_data = {
    "bpm": 0,
    "quality": 0,
    "valid": False,
    "last_updated": None
}

# GPS data
gps_data = {
    "latitude": 13.0827,  # Default (Chennai)
    "longitude": 80.2707, # Default (Chennai)
    "altitude": 0.0,
    "satellites": 0,
    "valid": False,
    "last_updated": None
}

# Add new global variables for sensor data
sensor_data = {
    "heartRate": 0,
    "heartRateAvg": 0,
    "spo2": 0,
    "spo2Avg": 0,
    "temperature": 0,
    "fallDetected": False,
    "validReadings": False,
    "last_updated": None
}

# Add heart rate buffer for 10-second averaging
heart_rate_buffer = []
BUFFER_DURATION = 10  # seconds

# Add gesture detection globals
gesture_enabled = False
gesture_thread = None
gesture_stop_event = threading.Event()
current_page = 0
MAX_PAGES = 6

def scan_ports():
    """Scan for available serial ports"""
    ports = []
    for port in serial.tools.list_ports.comports():
        ports.append({
            "device": port.device,
            "description": port.description,
            "hwid": port.hwid,
            "likely_esp32": "CP210" in port.description or "CH340" in port.description or "FTDI" in port.description
        })
    return ports

def connect_to_device(port_name):
    """Connect to the specified port"""
    global ser, connected, current_port
    
    if connected and ser and ser.is_open:
        try:
            ser.close()
        except Exception as e:
            logger.warning(f"Error closing previous connection: {e}")
        finally:
            connected = False
    
    try:
        logger.info(f"Attempting to connect to {port_name} at 115200 baud")
        # Increase timeout to 3 seconds and set parameters explicitly
        ser = serial.Serial(
            port=port_name,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=3,
            write_timeout=3
        )
        
        # Check if the port is actually open
        if not ser.is_open:
            logger.error(f"Port {port_name} failed to open")
            return False, "Failed to open serial port"
            
        connected = True
        current_port = port_name
        logger.info(f"Connected to {port_name}")
        
        # Give time for device reset - increased to 3 seconds
        time.sleep(3)
        
        # Clear input buffer
        ser.reset_input_buffer()
        
        # Send a carriage return to wake up the device
        ser.write(b"\r\n")
        time.sleep(0.5)
        
        # Try multiple commands for initialization
        success = False
        for attempt in range(3):
            logger.info(f"Initialization attempt {attempt+1}/3")
            
            # Send an empty line first to ensure proper state
            ser.write(b"\r\n")
            time.sleep(0.5)
            ser.reset_input_buffer()
            
            # Try different commands to get a response
            commands = ["6", "ping", "status"]
            cmd = commands[attempt % len(commands)]
            
            # Send command
            response = send_command(cmd)
            
            # Check for any of these patterns that would indicate successful communication
            success_patterns = [
                "CMD_RECEIVED", "GPS", "pong", "Device Status", "Heart Rate", 
                "SYNAPSE AR", "Current Status", "Terminal Interface", 
                "====", "MENU", "Help", "Schedule", "Medicine", 
                "Type a command", "command:"
            ]
            
            if any(pattern in response for pattern in success_patterns):
                logger.info(f"Device responded with valid data to '{cmd}' command")
                # Start monitoring threads
                monitor_thread = threading.Thread(target=monitor_serial, daemon=True)
                monitor_thread.start()
                success = True
                break
            else:
                logger.warning(f"Unexpected or no response for '{cmd}', attempt {attempt+1}")
                # Log the first few characters of the response for debugging
                if response:
                    logger.info(f"Response preview: {response[:50]}...")
                time.sleep(1)
        
        # Even if we didn't get a recognized response, consider it connected if we got any response at all
        if not success and ser and ser.is_open:
            if len(response) > 0:
                logger.warning("Device responded but with unrecognized format. Assuming connected anyway.")
                connected = True
                # Start monitoring thread
                monitor_thread = threading.Thread(target=monitor_serial, daemon=True)
                monitor_thread.start()
                return True, "Connected but device response format is unexpected"
        
        if success:
            return True, "Connected successfully"
        else:
            logger.warning("Connected to port but device not responding properly")
            # Still return True since we did connect to the port
            connected = True
            return True, "Connected to port but device communication may be unreliable"
        
    except serial.SerialException as e:
        logger.error(f"Error connecting to {port_name}: {e}")
        connected = False
        current_port = None
        return False, str(e)
    except Exception as e:
        logger.error(f"Unexpected error during connection: {e}")
        connected = False
        current_port = None
        return False, str(e)

def disconnect_device():
    """Disconnect from the current device"""
    global ser, connected, current_port
    
    # Stop gesture detection if running
    if gesture_enabled:
        stop_gesture_detection()
    
    with connection_lock:
        if connected and ser and ser.is_open:
            ser.close()
            logger.info(f"Disconnected from {current_port}")
        
        connected = False
        current_port = None
        ser = None

def send_command(command):
    """Send a command to the device and get response"""
    global ser, connected
    
    with connection_lock:
        if not connected or not ser or not ser.is_open:
            logger.warning("Attempted to send command while not connected")
            return "Error: Not connected to any device"
        
        try:
            # Special handling for GPS command
            if command == "6":
                # GPS commands may require multiple attempts
                return send_gps_command()
            
            # Clear input buffer first
            ser.reset_input_buffer()
            
            # Send command with newline
            logger.info(f"Sending command: {command}")
            ser.write((command + "\n").encode('utf-8'))
            
            # Wait for response - increased to 1.5 seconds
            time.sleep(1.5)
            
            # Read response
            response = ""
            start_time = time.time()
            timeout = 4.0  # Increased timeout to 4 seconds
            
            while time.time() - start_time < timeout:
                if ser.in_waiting:
                    byte_data = ser.read(ser.in_waiting)
                    try:
                        line = byte_data.decode('utf-8', errors='replace')  # Use 'replace' for better error handling
                        response += line
                    except UnicodeDecodeError:
                        # Handle decode errors by skipping problematic bytes
                        logger.warning(f"Unicode decode error, skipping some bytes")
                        response += "[decode error]"
                else:
                    # No more data, break out if we have some response or wait a bit
                    if response and len(response) > 10:
                        break
                    time.sleep(0.2)  # Increased delay before checking again
            
            if not response:
                logger.warning(f"No response received for command: {command}")
                return "No response from device"
                
            logger.info(f"Received response of {len(response)} bytes")
            if len(response) < 100:  # Only log small responses to avoid cluttering logs
                logger.info(f"Response content: {response}")
            else:
                logger.info(f"Response preview: {response[:50]}...")
            
            return response
        except Exception as e:
            logger.error(f"Error sending command: {e}")
            return f"Error: {str(e)}"

def send_gps_command():
    """Special function to handle GPS command with retries and more patience"""
    global ser
    
    # Clear input buffer first
    ser.reset_input_buffer()
    
    # Send GPS command
    logger.info("Sending GPS command with extended timeout")
    ser.write(b"6\n")
    
    # Give more time for GPS response
    time.sleep(2.0)
    
    # Read response with extended timeout
    response = ""
    start_time = time.time()
    timeout = 6.0  # Longer timeout for GPS
    
    while time.time() - start_time < timeout:
        if ser.in_waiting:
            byte_data = ser.read(ser.in_waiting)
            try:
                line = byte_data.decode('utf-8', errors='replace')
                response += line
            except UnicodeDecodeError:
                logger.warning("Unicode decode error in GPS response")
                response += "[decode error]"
        else:
            # Check if we have useful GPS data already
            if response and (
                "GPS signal not acquired yet" in response or 
                "Satellites:" in response or 
                "Latitude:" in response or 
                "Longitude:" in response or
                "Lat:" in response or
                "Lng:" in response
            ):
                break  # We have useful data, no need to wait more
            time.sleep(0.3)  # Longer delay for GPS
    
    # Even if we didn't get a normal response, check for these special markers
    if not response:
        logger.warning("No response received for GPS command")
        # Return a default fallback response that will be recognized
        return "GPS signal not acquired yet"
    elif len(response.strip()) < 5:
        logger.warning(f"Very short GPS response: '{response}'")
        # Return a default fallback response that will be recognized
        return "GPS signal not acquired yet"
    
    logger.info(f"GPS response received, length: {len(response)}")
    if len(response) < 100:
        logger.info(f"GPS response content: {response}")
    else:
        logger.info(f"GPS response preview: {response[:50]}...")
    
    return response

def fetch_medicine_list():
    """Get the current medicine list from the device"""
    # First check if we're connected
    global connected
    if not connected:
        # Return default values if not connected
        default_medicines = [
            {"index": 1, "name": "DICLOWIN 650 9 PM"},
            {"index": 2, "name": "IMEGLYN 1000 8 AM"},
            {"index": 3, "name": "Crocin 2 PM"},
            {"index": 4, "name": "Dolo 6 PM"}
        ]
        return default_medicines
    
    # Clear buffer and get fresh data
    try:
        response = send_command("menu")  # First refresh to main menu
        time.sleep(0.5)
        response = send_command("1")    # Then request medicines
        return parse_medicine_response(response)
    except Exception as e:
        logger.error(f"Error fetching medicines: {e}")
        # Return default values on error
        default_medicines = [
            {"index": 1, "name": "DICLOWIN 650 9 PM"},
            {"index": 2, "name": "IMEGLYN 1000 8 AM"},
            {"index": 3, "name": "Crocin 2 PM"},
            {"index": 4, "name": "Dolo 6 PM"}
        ]
        return default_medicines

def parse_medicine_response(response):
    """Parse medicine list from device response"""
    medicines = []
    # Extract only the medicine lines with numbers
    if "Current Medicines" in response:
        lines = response.strip().split('\n')
        reading_medicines = False
        for line in lines:
            if "Current Medicines" in line:
                reading_medicines = True
                continue
            if reading_medicines and line.strip() and not line.startswith("---"):
                # Match number format like "1. Medicine"
                if ". " in line and any(line.startswith(f"{n}.") for n in range(1, 5)):
                    parts = line.split(". ", 1)
                    if len(parts) > 1:
                        index = int(parts[0])
                        name = parts[1].strip()
                        medicines.append({"index": index, "name": name})
    
    # If we still don't have medicines, use default values
    if not medicines or len(medicines) < 4:
        default_medicines = ["DICLOWIN 650 9 PM", "IMEGLYN 1000 8 AM", "Crocin 2 PM", "Dolo 6 PM"]
        existing_indices = [m["index"] for m in medicines]
        
        for i in range(1, 5):
            if i not in existing_indices:
                medicines.append({"index": i, "name": default_medicines[i-1]})
    
    # Sort by index to ensure order
    medicines.sort(key=lambda x: x["index"])
    
    return medicines

def fetch_schedule_list():
    """Get the current schedule from the device"""
    # First check if we're connected
    global connected
    if not connected:
        # Return default values if not connected
        default_schedule = [
            {"index": 1, "details": "7 AM - Breakfast"},
            {"index": 2, "details": "1.10 PM - Lunch"},
            {"index": 3, "details": "8 PM - Dinner"},
            {"index": 4, "details": "9 PM - Medicine"}
        ]
        return default_schedule
    
    # Clear buffer and get fresh data
    try:
        response = send_command("menu")  # First refresh to main menu
        time.sleep(0.5)
        response = send_command("2")     # Then request schedule
        return parse_schedule_response(response)
    except Exception as e:
        logger.error(f"Error fetching schedule: {e}")
        # Return default values on error
        default_schedule = [
            {"index": 1, "details": "7 AM - Breakfast"},
            {"index": 2, "details": "1.10 PM - Lunch"},
            {"index": 3, "details": "8 PM - Dinner"},
            {"index": 4, "details": "9 PM - Medicine"}
        ]
        return default_schedule

def parse_schedule_response(response):
    """Parse schedule from device response"""
    schedule = []
    if "Current Schedule" in response:
        lines = response.strip().split('\n')
        reading_schedule = False
        for line in lines:
            if "Current Schedule" in line:
                reading_schedule = True
                continue
            if reading_schedule and line.strip() and not line.startswith("---"):
                # Match number format like "1. Schedule Item"
                if ". " in line and any(line.startswith(f"{n}.") for n in range(1, 5)):
                    parts = line.split(". ", 1)
                    if len(parts) > 1:
                        index = int(parts[0])
                        details = parts[1].strip()
                        schedule.append({"index": index, "details": details})
    
    # If we still don't have schedule items, use default values
    if not schedule or len(schedule) < 4:
        default_schedule = [
            "7 AM - Breakfast",
            "1.10 PM - Lunch",
            "8 PM - Dinner",
            "9 PM - Medicine"
        ]
        existing_indices = [s["index"] for s in schedule]
        
        for i in range(1, 5):
            if i not in existing_indices:
                schedule.append({"index": i, "details": default_schedule[i-1]})
    
    # Sort by index to ensure order
    schedule.sort(key=lambda x: x["index"])
    
    return schedule

def fetch_gps_data():
    """Get current GPS data from device"""
    global connected, ser, gps_data
    
    if not connected or not ser or not ser.is_open:
        logger.warning("Cannot fetch GPS data - device not connected")
        return gps_data
    
    try:
        # Clear any pending data
        ser.reset_input_buffer()
        
        # Request GPS data
        logger.info("Requesting GPS data...")
        response = send_command("6")  # Now uses special GPS handler
        logger.info(f"Raw GPS response: {response}")
        
        # Parse and return GPS data
        updated_data = parse_gps_response(response)
        
        # Keep existing valid coordinates if new data is invalid but we had good data before
        if not updated_data["valid"] and gps_data["valid"]:
            updated_data["latitude"] = gps_data["latitude"]
            updated_data["longitude"] = gps_data["longitude"]
            updated_data["altitude"] = gps_data["altitude"]
            logger.info("Keeping previous valid coordinates")
            
            # Mark as valid if we have non-zero coordinates
            if updated_data["latitude"] != 0 and updated_data["longitude"] != 0:
                updated_data["valid"] = True
        
        # Update global variable
        gps_data = updated_data
        return updated_data
        
    except Exception as e:
        logger.error(f"Error fetching GPS data: {e}")
        return gps_data

def parse_gps_response(response):
    """Parse GPS data from device response"""
    global gps_data
    
    # Initialize with existing data
    updated_data = gps_data.copy()
    
    try:
        # Check if GPS signal is not acquired yet
        if "GPS signal not acquired yet" in response:
            logger.info("Device reports GPS signal not acquired yet")
            updated_data["valid"] = False
            updated_data["last_updated"] = time.time()
            
            # For better user experience, provide default coordinates
            if updated_data["latitude"] == 0 and updated_data["longitude"] == 0:
                updated_data["latitude"] = 13.0827  # Chennai default
                updated_data["longitude"] = 80.2707
                logger.info("Using default coordinates for Chennai")
            
            return updated_data
        
        # Check for satellites-only response (partial data)
        if "Satellites:" in response and len(response) < 50:
            logger.info("Received satellites-only data")
            try:
                # Try to extract the satellite count
                sat_parts = response.split("Satellites:")
                if len(sat_parts) > 1:
                    sat_str = sat_parts[1].strip()
                    # Extract just the first digit if there's extra text
                    import re
                    sat_match = re.search(r'\d+', sat_str)
                    if sat_match:
                        satellites = int(sat_match.group())
                        updated_data["satellites"] = satellites
                        logger.info(f"Updated satellites count to {satellites}")
                        
                # Update timestamp but don't mark as valid without coordinates
                updated_data["last_updated"] = time.time()
                
                # For better user experience, provide default coordinates if needed
                if updated_data["latitude"] == 0 and updated_data["longitude"] == 0:
                    updated_data["latitude"] = 13.0827  # Chennai default
                    updated_data["longitude"] = 80.2707
                    logger.info("Using default coordinates for Chennai")
                
                return updated_data
            except Exception as e:
                logger.error(f"Error parsing satellites data: {e}")
        
        # Look for the specific format we're getting from ESP32
        if "GPS Location Updated!" in response:
            lines = response.split('\n')
            for line in lines:
                if "Lat:" in line:
                    lat_str = line.split("Lat:")[1].strip()
                    updated_data["latitude"] = float(lat_str)
                elif "Lng:" in line:
                    lng_str = line.split("Lng:")[1].strip()
                    updated_data["longitude"] = float(lng_str)
                elif "Satellites:" in line and not "$" in line:  # Avoid NMEA sentences
                    sat_str = line.split("Satellites:")[1].strip()
                    updated_data["satellites"] = int(sat_str)
                    
                # Look for altitude in GPGGA sentence
                elif "$GPGGA" in line:
                    parts = line.split(',')
                    if len(parts) >= 10:
                        try:
                            alt = float(parts[9])
                            updated_data["altitude"] = alt
                        except ValueError:
                            pass
            
            # If we have valid coordinates, mark as valid
            if updated_data["latitude"] != 0 and updated_data["longitude"] != 0:
                updated_data["valid"] = True
                updated_data["last_updated"] = time.time()
                logger.info(f"Successfully parsed GPS data: {updated_data}")
            else:
                logger.warning("Got GPS data but coordinates are 0,0")
        else:
            # Check for newer format where GPS data comes after "Latitude:" and "Longitude:"
            lines = response.split('\n')
            found_coords = False
            
            for line in lines:
                if "Latitude:" in line:
                    try:
                        lat_str = line.split("Latitude:")[1].strip()
                        updated_data["latitude"] = float(lat_str)
                        found_coords = True
                    except (ValueError, IndexError):
                        logger.warning(f"Failed to parse latitude from: {line}")
                        
                elif "Longitude:" in line:
                    try:
                        lng_str = line.split("Longitude:")[1].strip()
                        updated_data["longitude"] = float(lng_str)
                        found_coords = True
                    except (ValueError, IndexError):
                        logger.warning(f"Failed to parse longitude from: {line}")
                        
                elif "Altitude:" in line:
                    try:
                        # Extract just the number from "X.X meters"
                        alt_str = line.split("Altitude:")[1].strip().split(" ")[0]
                        updated_data["altitude"] = float(alt_str)
                    except (ValueError, IndexError):
                        logger.warning(f"Failed to parse altitude from: {line}")
                        
                elif "Satellites:" in line:
                    try:
                        sat_str = line.split("Satellites:")[1].strip()
                        updated_data["satellites"] = int(sat_str)
                    except (ValueError, IndexError):
                        logger.warning(f"Failed to parse satellites from: {line}")
            
            if found_coords and updated_data["latitude"] != 0 and updated_data["longitude"] != 0:
                updated_data["valid"] = True
                updated_data["last_updated"] = time.time()
                logger.info(f"Successfully parsed GPS data (new format): {updated_data}")
            elif not "GPS signal not acquired yet" in response:
                logger.warning("GPS response didn't contain recognizable coordinate format")
    except Exception as e:
        logger.error(f"Error parsing GPS data: {e}")
        # Keep existing data on parsing error
    
    # Update global GPS data
    gps_data = updated_data    
    return updated_data

# --- Dedicated Heart Rate Page Routes ---

@app.route('/heart_rate')
def heart_rate_page():
    """Render the dedicated heart rate monitor page"""
    return render_template('heart_rate.html')

@app.route('/api/ports')
def list_ports_api():
    """List available serial ports"""
    return jsonify(scan_ports())

@app.route('/api/heart_rate/connect', methods=['POST'])
def connect_heart_rate_api():
    """Connect to a separate serial port for heart rate monitoring"""
    data = request.json
    port = data.get('port')
    
    if not port:
        return jsonify({"success": False, "message": "Port is required"})
        
    global hr_ser, hr_connected, hr_port, hr_monitoring_thread
    
    with hr_lock:
        if hr_connected and hr_ser and hr_ser.is_open:
            try:
                hr_ser.close()
            except:
                pass
            hr_connected = False
            
        try:
            hr_ser = serial.Serial(port, 9600, timeout=1) # Pulse Sensor usually 9600
            hr_connected = True
            hr_port = port
            
            # Start monitoring thread
            if hr_monitoring_thread is None or not hr_monitoring_thread.is_alive():
                hr_monitoring_thread = threading.Thread(target=monitor_hr_dedicated, daemon=True)
                hr_monitoring_thread.start()
                
            return jsonify({"success": True, "message": "Connected successfully"})
        except Exception as e:
            logger.error(f"Error connecting to HR port: {e}")
            hr_connected = False
            return jsonify({"success": False, "message": str(e)})

@app.route('/api/heart_rate/disconnect', methods=['POST'])
def disconnect_heart_rate_api():
    """Disconnect from heart rate port"""
    global hr_ser, hr_connected
    
    with hr_lock:
        if hr_connected and hr_ser:
            try:
                hr_ser.close()
            except:
                pass
        hr_connected = False
        return jsonify({"success": True})

@app.route('/api/heart_rate/data')
def get_heart_rate_data_api():
    """Get the current heart rate data"""
    # Return the global sensor_data (which is updated by the thread)
    return jsonify({
        "bpm": sensor_data["heartRate"],
        "valid": sensor_data["validReadings"],
        "last_updated": sensor_data["last_updated"]
    })

def monitor_hr_dedicated():
    """Monitoring thread for dedicated HR port"""
    global hr_ser, hr_connected
    
    logger.info("Starting dedicated HR monitoring thread")
    
    while True:
        with hr_lock:
            if not hr_connected or not hr_ser or not hr_ser.is_open:
                time.sleep(1)
                continue
                
        try:
            if hr_ser.in_waiting:
                line = hr_ser.readline().decode('utf-8', errors='ignore').strip()
                
                # Format expected from external_pulse_sensor.ino is "HR:75"
                if line.startswith("HR:"):
                    try:
                        bpm = float(line.split(":")[1])
                        
                        # Update global data
                        sensor_data["heartRate"] = bpm
                        sensor_data["validReadings"] = True
                        sensor_data["last_updated"] = time.time()
                        
                        # Also update the legacy data structure
                        heart_rate_data.update({
                            "bpm": bpm,
                            "valid": True,
                            "last_updated": time.time()
                        })
                        
                    except ValueError:
                        pass
            else:
                time.sleep(0.1)
                
        except Exception as e:
            logger.error(f"Error in HR thread: {e}")
            time.sleep(1)

def monitor_heart_rate():
    """Background thread to monitor heart rate data from device"""
    global ser, connected, heart_rate_data
    
    logger.info("Starting heart rate monitoring thread")
    
    while connected and ser and ser.is_open:
        try:
            # Check for heart rate data in serial buffer
            if ser.in_waiting > 0:
                try:
                    line = ser.readline().decode('utf-8', errors='replace').strip()
                    
                    # Check for sensor data
                    if line.startswith("SENSOR_DATA:"):
                        try:
                            # Parse SENSOR_DATA:heartRate,heartRateAvg,spo2,spo2Avg,temp,fall,valid format
                            parts = line[12:].split(',')
                            if len(parts) >= 7:
                                sensor_data.update({
                                    "heartRate": float(parts[0]),
                                    "heartRateAvg": int(parts[1]),
                                    "spo2": int(parts[2]),
                                    "spo2Avg": int(parts[3]),
                                    "temperature": float(parts[4]),
                                    "fallDetected": parts[5] == "1",
                                    "validReadings": parts[6] == "1",
                                    "last_updated": time.time()
                                })
                                logger.debug(f"Updated sensor data: {sensor_data}")
                                
                                # Update heart rate data for compatibility
                                heart_rate_data.update({
                                    "bpm": sensor_data["heartRate"],
                                    "quality": 100 if sensor_data["validReadings"] else 0,
                                    "valid": sensor_data["validReadings"],
                                    "last_updated": time.time()
                                })
                        except (ValueError, IndexError) as e:
                            logger.warning(f"Error parsing sensor data: {e}, raw data: {line}")
                    
                    # Handle other existing data formats...
                    elif line.startswith("HR_DATA:"):
                        # Keep existing HR_DATA handling for backward compatibility
                        try:
                            parts = line[8:].split(',')
                            if len(parts) >= 2 and parts[0].strip() and parts[0].strip().replace('.', '', 1).isdigit():
                                bpm = float(parts[0])
                                quality = int(parts[1]) if parts[1].strip().isdigit() else 0
                                
                                heart_rate_data.update({
                                    "bpm": bpm,
                                    "quality": quality,
                                    "valid": True,
                                    "last_updated": time.time()
                                })
                        except (ValueError, IndexError) as e:
                            logger.warning(f"Error parsing heart rate data: {e}, raw data: {line}")
                except Exception as e:
                    logger.warning(f"Error reading line: {e}")
            
            # Sleep briefly to avoid high CPU usage
            time.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Error in heart rate monitoring thread: {e}")
            time.sleep(1)  # Wait before retrying
    
    logger.info("Heart rate monitoring thread stopped")

def monitor_gps():
    """Background thread to monitor GPS data from device"""
    global ser, connected, gps_data
    
    logger.info("Starting GPS monitoring thread")
    
    while connected and ser and ser.is_open:
        try:
            # Request GPS data every 5 seconds
            logger.info("Requesting GPS data...")
            response = send_command("6")  # GPS data command
            logger.info(f"Raw GPS response: {response}")
            
            # Parse GPS data from response
            if "GPS signal not acquired yet" in response:
                logger.info("GPS signal not acquired yet")
                gps_data["valid"] = False
                gps_data["last_updated"] = time.time()
            elif "Satellites:" in response and len(response) < 50:
                # Handle satellites-only response
                logger.info("Received satellites-only data")
                try:
                    # Extract satellites count
                    sat_parts = response.split("Satellites:")
                    if len(sat_parts) > 1:
                        sat_str = sat_parts[1].strip()
                        # Use regex to extract just the number
                        import re
                        sat_match = re.search(r'\d+', sat_str)
                        if sat_match:
                            gps_data["satellites"] = int(sat_match.group())
                            logger.info(f"Updated satellites count to {gps_data['satellites']}")
                    
                    # Update timestamp
                    gps_data["last_updated"] = time.time()
                    
                    # If we don't have coordinates yet, use defaults for better UX
                    if gps_data["latitude"] == 0 and gps_data["longitude"] == 0:
                        gps_data["latitude"] = 13.0827  # Chennai default
                        gps_data["longitude"] = 80.2707
                        logger.info("Using default coordinates for Chennai")
                except Exception as e:
                    logger.error(f"Error parsing satellites data: {e}")
            elif "Latitude:" in response and "Longitude:" in response:
                try:
                    # Extract GPS coordinates
                    lines = response.strip().split('\n')
                    for line in lines:
                        logger.debug(f"Processing GPS line: {line}")
                        if "Latitude:" in line:
                            lat_str = line.split("Latitude:")[1].strip()
                            gps_data["latitude"] = float(lat_str)
                            logger.info(f"Parsed latitude: {lat_str}")
                        elif "Longitude:" in line:
                            lng_str = line.split("Longitude:")[1].strip()
                            gps_data["longitude"] = float(lng_str)
                            logger.info(f"Parsed longitude: {lng_str}")
                        elif "Altitude:" in line:
                            # Extract just the number from "X.X meters"
                            alt_str = line.split("Altitude:")[1].strip().split(" ")[0]
                            gps_data["altitude"] = float(alt_str)
                            logger.info(f"Parsed altitude: {alt_str}")
                        elif "Satellites:" in line:
                            sat_str = line.split("Satellites:")[1].strip()
                            gps_data["satellites"] = int(sat_str)
                            logger.info(f"Parsed satellites: {sat_str}")
                    
                    # If we have valid coordinates, mark as valid
                    if gps_data["latitude"] != 0 and gps_data["longitude"] != 0:
                        gps_data["valid"] = True
                        gps_data["last_updated"] = time.time()
                        logger.info(f"Updated GPS data: {gps_data}")
                    else:
                        logger.warning("Got GPS data but coordinates are 0,0")
                except Exception as e:
                    logger.error(f"Error parsing GPS data: {e}")
                    logger.error(f"Problematic response: {response}")
            elif "Lat:" in response and "Lng:" in response:
                # Handle the alternative format
                try:
                    # Extract GPS coordinates
                    lines = response.strip().split('\n')
                    for line in lines:
                        logger.debug(f"Processing GPS line: {line}")
                        if "Lat:" in line:
                            lat_str = line.split("Lat:")[1].strip()
                            gps_data["latitude"] = float(lat_str)
                            logger.info(f"Parsed latitude: {lat_str}")
                        elif "Lng:" in line:
                            lng_str = line.split("Lng:")[1].strip()
                            gps_data["longitude"] = float(lng_str)
                            logger.info(f"Parsed longitude: {lng_str}")
                        elif "Satellites:" in line and not "$" in line:
                            sat_str = line.split("Satellites:")[1].strip()
                            gps_data["satellites"] = int(sat_str)
                            logger.info(f"Parsed satellites: {sat_str}")
                    
                    # If we have valid coordinates, mark as valid
                    if gps_data["latitude"] != 0 and gps_data["longitude"] != 0:
                        gps_data["valid"] = True
                        gps_data["last_updated"] = time.time()
                        logger.info(f"Updated GPS data: {gps_data}")
                    else:
                        logger.warning("Got GPS data but coordinates are 0,0")
                except Exception as e:
                    logger.error(f"Error parsing GPS data: {e}")
                    logger.error(f"Problematic response: {response}")
            else:
                logger.warning(f"Unrecognized GPS response format. Response length: {len(response)}")
            
            # Sleep for 5 seconds before next update
            time.sleep(5)
            
        except Exception as e:
            logger.error(f"Error in GPS monitoring thread: {e}")
            time.sleep(1)  # Wait before retrying
    
    logger.info("GPS monitoring thread stopped")

def fetch_heart_rate():
    """Get the current heart rate data from the device"""
    global connected, heart_rate_data
    
    # If not connected or data is stale (>30 seconds old), try to get fresh data
    if connected and (not heart_rate_data["valid"] or 
                      heart_rate_data["last_updated"] is None or 
                      time.time() - heart_rate_data["last_updated"] > 30):
        try:
            # Request heart rate data directly
            response = send_command("7")  # Heart rate command
            
            # More flexible parsing
            for pattern in ["Current BPM:", "BPM:", "Heart Rate:"]:
                if pattern in response:
                    try:
                        # Find the line with the BPM value
                        for line in response.split('\n'):
                            if pattern in line:
                                # Extract the number after the pattern
                                value_part = line.split(pattern)[1].strip()
                                # Find the first number in the string
                                import re
                                bpm_match = re.search(r'\d+(\.\d+)?', value_part)
                                if bpm_match:
                                    bpm = float(bpm_match.group())
                                    heart_rate_data["bpm"] = bpm
                                    heart_rate_data["valid"] = True
                                    heart_rate_data["last_updated"] = time.time()
                                    break
                    except Exception as e:
                        logger.error(f"Error parsing heart rate value: {e}")
                    
                    # Try to get quality info if available
                    try:
                        for line in response.split('\n'):
                            if "Quality:" in line or "Signal:" in line:
                                value_part = line.split(':')[1].strip()
                                quality_match = re.search(r'\d+', value_part)
                                if quality_match:
                                    heart_rate_data["quality"] = int(quality_match.group())
                    except Exception:
                        # Quality info is optional
                        pass
                    
                    break
        except Exception as e:
            logger.error(f"Error fetching heart rate: {e}")
    
    return heart_rate_data

def parse_sensor_data(response):
    """Parse sensor data from the device response"""
    try:
        # Initialize default values
        data = {
            "heartRate": 0,
            "heartRateAvg": 0,
            "spo2": 0,
            "spo2Avg": 0,
            "temperature": 0,
            "fallDetected": False,
            "validReadings": False,
            "last_updated": time.time()
        }
        
        # Split response into lines and process each line
        lines = response.split('\n')
        for line in lines:
            line = line.strip()
            
            # Parse heart rate
            if "Heart Rate:" in line:
                try:
                    # Extract heart rate value
                    hr_part = line.split("Heart Rate:")[1].split("BPM")[0].strip()
                    data["heartRate"] = float(hr_part)
                    
                    # Extract average if available
                    if "(Avg:" in line:
                        avg_part = line.split("(Avg:")[1].split("BPM")[0].strip()
                        data["heartRateAvg"] = int(float(avg_part))
                except Exception as e:
                    logger.error(f"Error parsing heart rate: {e}, line: {line}")
            
            # Parse SPO2
            elif "SPO2:" in line:
                try:
                    # Extract SPO2 value
                    spo2_part = line.split("SPO2:")[1].split("%")[0].strip()
                    data["spo2"] = int(float(spo2_part))
                    
                    # Extract average if available
                    if "(Avg:" in line:
                        avg_part = line.split("(Avg:")[1].split("%")[0].strip()
                        data["spo2Avg"] = int(float(avg_part))
                except Exception as e:
                    logger.error(f"Error parsing SPO2: {e}, line: {line}")
            
            # Parse temperature
            elif "Temperature:" in line:
                try:
                    temp_part = line.split("Temperature:")[1].strip()
                    if "°C" in temp_part:
                        data["temperature"] = float(temp_part.replace("°C", ""))
                except Exception as e:
                    logger.error(f"Error parsing temperature: {e}, line: {line}")
            
            # Parse fall detection with improved detection logic
            elif "Fall Detected:" in line:
                fall_text = line.split("Fall Detected:")[1].strip().upper()
                # Check for YES, YES!, Y, 1, TRUE as positive indicators
                is_fall_detected = any(keyword in fall_text for keyword in ["YES", "Y", "1", "TRUE"])
                data["fallDetected"] = is_fall_detected
                logger.info(f"Fall detection parsed from '{fall_text}' -> {is_fall_detected}")
            
            # Parse readings validity
            elif "Readings Valid:" in line:
                data["validReadings"] = "Yes" in line
        
        # Additional validation
        if data["spo2"] > 0 or data["heartRate"] > 0:
            data["validReadings"] = True
        
        logger.debug(f"Parsed sensor data: {data}")
        return data
        
    except Exception as e:
        logger.error(f"Error parsing sensor data: {e}")
        logger.error(f"Raw response: {response}")
        return None

def monitor_serial():
    """Background thread to monitor serial data from device"""
    global ser, connected, sensor_data, heart_rate_buffer
    
    logger.info("Starting serial monitoring thread")
    reading_lines = []
    last_cleanup = time.time()
    last_fall_detection_time = 0
    fall_detection_timeout = 30  # Seconds to maintain fall detection state
    
    while connected and ser and ser.is_open:
        try:
            # Check for data in serial buffer
            if ser.in_waiting > 0:
                try:
                    line = ser.readline().decode('utf-8', errors='replace').strip()
                    
                    # Handle both data formats
                    if "SENSOR_DATA:" in line:
                        # Direct sensor data format
                        try:
                            parts = line.split("SENSOR_DATA:")[1].strip().split(',')
                            if len(parts) >= 7:
                                current_time = time.time()
                                
                                # Add heart rate to buffer with timestamp
                                heart_rate_buffer.append({
                                    "value": float(parts[0]),
                                    "timestamp": current_time
                                })
                                
                                # Clean up old readings (older than BUFFER_DURATION seconds)
                                heart_rate_buffer = [
                                    hr for hr in heart_rate_buffer 
                                    if (current_time - hr["timestamp"]) <= BUFFER_DURATION
                                ]
                                
                                # Calculate 10-second average
                                if heart_rate_buffer:
                                    avg_heart_rate = sum(hr["value"] for hr in heart_rate_buffer) / len(heart_rate_buffer)
                                else:
                                    avg_heart_rate = 0
                                
                                # Prepare update data
                                update_data = {
                                    "heartRate": float(parts[0]),
                                    "heartRateAvg": int(avg_heart_rate),  # Use calculated 10-second average
                                    "spo2": int(parts[2]),
                                    "spo2Avg": int(parts[3]),
                                    "temperature": float(parts[4]),
                                    "validReadings": parts[6] == "1",
                                    "last_updated": current_time
                                }
                                
                                # Only update fall detection if no fall was recently detected in text format
                                # or if the binary format is indicating a fall (parts[5] == "1")
                                fall_in_binary = parts[5] == "1"
                                if fall_in_binary or (current_time - last_fall_detection_time > fall_detection_timeout):
                                    update_data["fallDetected"] = fall_in_binary
                                    # If binary format detected a fall, update the timestamp
                                    if fall_in_binary:
                                        last_fall_detection_time = current_time
                                        logger.info(f"Fall detected in binary format")
                                        
                                # Update sensor data with our prepared data
                                sensor_data.update(update_data)
                                logger.debug(f"Updated sensor data (direct format): {sensor_data}")
                        except Exception as e:
                            logger.error(f"Error parsing direct sensor data: {e}")
                            
                    elif line.startswith("--- Received Sensor Data ---"):
                        # Start of a new accumulated reading
                        reading_lines = []
                    elif line.startswith("-------------------------"):
                        # End of reading, parse accumulated lines
                        if reading_lines:
                            response = "\n".join(reading_lines)
                            logger.info(f"Received response of {len(response)} bytes")
                            logger.info(f"Response content: {response}")
                            
                            # Parse the sensor data
                            parsed_data = parse_sensor_data(response)
                            if parsed_data:
                                # If a fall was detected in the text format, remember the time
                                if parsed_data.get("fallDetected", False):
                                    last_fall_detection_time = time.time()
                                    logger.info("Fall detected in text format")
                                
                                sensor_data.update(parsed_data)
                                sensor_data["last_updated"] = time.time()
                                logger.debug(f"Updated sensor data (accumulated format): {sensor_data}")
                    else:
                        # Add line to current reading
                        reading_lines.append(line)
                        
                except UnicodeDecodeError as e:
                    logger.error(f"Error decoding serial data: {e}")
                except Exception as e:
                    logger.error(f"Error processing serial data: {e}")
            
            # Small delay to prevent CPU overload
            time.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Error in serial monitoring thread: {e}")
            time.sleep(1)  # Wait before retrying
    
    logger.info("Serial monitoring thread stopped")

@app.route('/')
def index():
    """Home page with device connection status"""
    global connected, current_port, available_ports
    
    # Refresh available ports
    available_ports = scan_ports()
    
    return render_template('index.html', 
                          connected=connected, 
                          current_port=current_port,
                          available_ports=available_ports)

def initialize_device():
    """Initialize the device and verify communication"""
    global ser, connected
    
    if not ser or not ser.is_open:
        logger.error("Serial port not open")
        return False
        
    try:
        # Clear any pending data
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        
        # Wait for device to stabilize
        time.sleep(2)
        
        # Try to get a response from the device
        for attempt in range(3):
            logger.info(f"Initialization attempt {attempt + 1}/3")
            
            # Send a command and wait for response
            ser.write(b'6\n')  # Request GPS data
            time.sleep(1)
            
            if ser.in_waiting > 0:
                response = ser.read(ser.in_waiting).decode('utf-8', errors='replace')
                # Check for several valid response patterns
                if "GPS signal not acquired yet" in response or "GPS Location Updated" in response or "CMD_RECEIVED" in response:
                    logger.info("Device responding correctly with valid GPS command response")
                    return True
                else:
                    logger.warning(f"Unexpected response format: {response[:100]}...")
            else:
                logger.warning("No response from device")
            
            time.sleep(1)
        
        # Even if we didn't get the exact response we expected, still mark as connected
        # if we got any response that seems like our device
        logger.warning("Connected to port but device not responding with expected pattern")
        return True
        
    except Exception as e:
        logger.error(f"Error during device initialization: {e}")
        return False

@app.route('/connect', methods=['POST'])
def connect():
    """Connect to the selected serial port"""
    global ser, connected, current_port
    
    port = request.form.get('port')
    if not port:
        flash("No port selected", "error")
        return redirect(url_for('index'))
    
    try:
        # Close existing connection if any
        if ser and ser.is_open:
            ser.close()
            connected = False
        
        # Open new connection
        ser = serial.Serial(port, 115200, timeout=1)
        logger.info(f"Attempting to connect to {port} at 115200 baud")
        
        # Initialize device
        if initialize_device():
            connected = True
            current_port = port
            
            # Start monitoring thread
            monitor_thread = threading.Thread(target=monitor_serial, daemon=True)
            monitor_thread.start()
            
            flash(f"Connected to {port}", "success")
        else:
            ser.close()
            connected = False
            flash("Device not responding properly", "error")
            
    except Exception as e:
        logger.error(f"Error connecting to {port}: {e}")
        flash(f"Error connecting to {port}: {str(e)}", "error")
        connected = False
        
    return redirect(url_for('index'))

@app.route('/disconnect', methods=['POST'])
def disconnect():
    """Disconnect from the current device"""
    disconnect_device()
    flash("Disconnected from device", "info")
    return redirect(url_for('index'))

@app.route('/scan', methods=['POST'])
def scan():
    """Scan for available ports"""
    global available_ports
    available_ports = scan_ports()
    return redirect(url_for('index'))

def check_medicine_time(medicine_str):
    """Parse medicine string to check if it's time to take it"""
    try:
        parts = medicine_str.split()
        # Find the time part (assuming the last two parts are the time and AM/PM)
        if len(parts) >= 2:
            time_parts = parts[-2:]
            hour = int(time_parts[0])
            am_pm = time_parts[1].upper()
            
            # Get current hour
            current_hour = datetime.datetime.now().hour
            
            # Convert medicine time to 24-hour format
            medicine_hour = hour
            if am_pm == 'PM' and hour < 12:
                medicine_hour += 12
            elif am_pm == 'AM' and hour == 12:
                medicine_hour = 0
                
            # Check if it's time to take (within 2 hours before or 3 hours after)
            time_diff = current_hour - medicine_hour
            if -2 <= time_diff <= 3:
                return True, f"Take now ({hour} {am_pm})"
            else:
                return False, f"Next dose: {hour} {am_pm}"
    except Exception as e:
        logging.error(f"Error parsing medicine time: {e}")
    
    # Default return if parsing fails
    return False, "Schedule unknown"

@app.route('/medicines')
def medicine_page():
    """Medicine management page"""
    # Always show the page, but with a warning if not connected
    if not connected:
        flash("Not connected to a device. Showing default values. Changes won't be saved until connected.", "warning")
    
    medicines = fetch_medicine_list()
    
    # Check if each medicine should be taken now
    for medicine in medicines:
        should_take, message = check_medicine_time(medicine["name"])
        medicine["should_take"] = should_take
        medicine["message"] = message
    
    return render_template('medicines.html', medicines=medicines, connected=connected)

@app.route('/update_medicine', methods=['POST'])
def update_medicine():
    """Update a medicine entry"""
    index = request.form.get('index')
    name = request.form.get('name')
    
    if not index or not name:
        flash("Index and name are required", "error")
        return redirect(url_for('medicines'))
    
    if not connected:
        flash("Not connected to device. Connect first to save changes.", "warning")
        return redirect(url_for('medicines'))
    
    try:
        # Try a menu command first to ensure we're in the right state
        send_command("menu")
        time.sleep(0.5)
        
        # Now send the medicine update command
        command = f"med {index} {name}"
        logger.info(f"Updating medicine: {command}")
        response = send_command(command)
        
        if not response:
            flash("No response from device. Please try again.", "error")
        elif "updated" in response.lower() or "medicine" in response.lower():
            flash(f"Medicine {index} updated successfully", "success")
            logger.info(f"Successfully updated medicine {index} to {name}")
        else:
            logger.warning(f"Unexpected response: {response}")
            flash(f"Update may not have succeeded. Please check the device.", "warning")
        
        # Refresh the medicines list
        time.sleep(0.5)
        send_command("1")
    except Exception as e:
        logger.error(f"Error during medicine update: {e}")
        flash(f"Error: {str(e)}", "error")
    
    return redirect(url_for('medicines'))

@app.route('/schedule')
def schedule_page():
    """Schedule management page"""
    # Always show the page, but with a warning if not connected
    if not connected:
        flash("Not connected to a device. Showing default values. Changes won't be saved until connected.", "warning")
    
    schedule = fetch_schedule_list()
    return render_template('schedule.html', schedule=schedule, connected=connected)

@app.route('/update_schedule', methods=['POST'])
def update_schedule():
    """Update a schedule entry"""
    index = request.form.get('index')
    details = request.form.get('details')
    
    if not index or not details:
        flash("Index and details are required", "error")
        return redirect(url_for('schedule_page'))
    
    if not connected:
        flash("Not connected to device. Connect first to save changes.", "warning")
        return redirect(url_for('schedule_page'))
    
    try:
        # Try a menu command first to ensure we're in the right state
        send_command("menu")
        time.sleep(0.5)
        
        # Now send the schedule update command
        command = f"sch {index} {details}"
        logger.info(f"Updating schedule: {command}")
        response = send_command(command)
        
        if not response:
            flash("No response from device. Please try again.", "error")
        elif "updated" in response.lower() or "schedule" in response.lower():
            flash(f"Schedule {index} updated successfully", "success")
            logger.info(f"Successfully updated schedule {index} to {details}")
        else:
            logger.warning(f"Unexpected response: {response}")
            flash(f"Update may not have succeeded. Please check the device.", "warning")
        
        # Refresh the schedule list
        time.sleep(0.5)
        send_command("2")
    except Exception as e:
        logger.error(f"Error during schedule update: {e}")
        flash(f"Error: {str(e)}", "error")
    
    return redirect(url_for('schedule_page'))

@app.route('/emergency')
def emergency_page():
    """Emergency contact management page"""
    # Always show the page, but with a warning if not connected
    if not connected:
        flash("Not connected to a device. Showing default values. Changes won't be saved until connected.", "warning")
    
    return render_template('emergency.html', connected=connected)

@app.route('/update_emergency', methods=['POST'])
def update_emergency():
    """Update emergency contact"""
    name = request.form.get('name')
    number = request.form.get('number')
    
    if not name or not number:
        flash("Name and number are required", "error")
        return redirect(url_for('emergency_page'))
    
    if not connected:
        flash("Not connected to device. Connect first to save changes.", "warning")
        return redirect(url_for('emergency_page'))
    
    try:
        # Try a menu command first to ensure we're in the right state
        send_command("menu")
        time.sleep(0.5)
        
        # Now send the emergency contact update command
        command = f"emergency {name} {number}"
        logger.info(f"Updating emergency contact: {command}")
        response = send_command(command)
        
        if not response:
            flash("No response from device. Please try again.", "error")
        elif "updated" in response.lower() or "emergency" in response.lower() or "contact" in response.lower():
            flash("Emergency contact updated successfully", "success")
            logger.info(f"Successfully updated emergency contact to {name}, {number}")
            
            # No longer inject JavaScript into flash messages
            logger.info(f"Updated contact information: {name}, {number}")
        else:
            logger.warning(f"Unexpected response: {response}")
            flash(f"Update may not have succeeded. Please check the device.", "warning")
    except Exception as e:
        logger.error(f"Error during emergency contact update: {e}")
        flash(f"Error: {str(e)}", "error")
    
    return redirect(url_for('emergency_page'))

@app.route('/custom_command')
def custom_command_page():
    """Redirect custom command page to home with informative message"""
    flash("The custom command feature is currently disabled for improved security.", "info")
    return redirect(url_for('index'))

@app.route('/send_command', methods=['POST'])
def execute_command():
    """Send a custom command to the device"""
    if not connected:
        flash("Not connected to device", "error")
        return redirect(url_for('custom_command_page'))
    
    command = request.form.get('command')
    
    if not command:
        flash("Command cannot be empty", "error")
        return redirect(url_for('custom_command_page'))
    
    response = send_command(command)
    return render_template('custom_command.html', command=command, response=response)

@app.route('/api/status')
def api_status():
    """API endpoint for connection status"""
    return jsonify({
        "connected": connected,
        "port": current_port,
        "available_ports": available_ports
    })

@app.route('/api/medicines')
def api_medicines():
    """API endpoint for medicines list"""
    if not connected:
        return jsonify({"error": "Not connected to device"}), 400
    
    medicines = fetch_medicine_list()
    return jsonify(medicines)

@app.route('/api/schedule')
def api_schedule():
    """API endpoint for schedule list"""
    if not connected:
        return jsonify({"error": "Not connected to device"}), 400
    
    schedule = fetch_schedule_list()
    return jsonify(schedule)

@app.route('/gps')
def gps_page():
    """Render the GPS location page"""
    global connected
    # Fetch latest GPS data
    if connected:
        fetch_gps_data()
    return render_template('gps.html', connected=connected, current_port=current_port)

@app.route('/api/gps')
def api_gps():
    """Return GPS data as JSON"""
    try:
        if connected:
            # Get fresh GPS data
            gps_info = fetch_gps_data()
            logger.info(f"Sending GPS data to frontend: {gps_info}")
            return jsonify(gps_info)
        else:
            # Return cached data
            logger.info(f"Device not connected, sending cached GPS data: {gps_data}")
            return jsonify(gps_data)
    except Exception as e:
        logger.error(f"Error in GPS API endpoint: {e}")
        return jsonify({
            "error": str(e),
            "valid": False,
            "latitude": 13.0827,
            "longitude": 80.2707,
            "altitude": 0.0,
            "satellites": 0,
            "last_updated": None
        })

@app.route('/spo2')
def spo2_page():
    """Render the SPO2 monitoring page"""
    global connected
    
    # Always show the page, but with a warning if not connected
    if not connected:
        flash("Not connected to a device. SPO2 monitoring not available.", "warning")
        
    # Get current sensor data
    current_data = sensor_data
    
    # Include time module for template
    return render_template('spo2.html', 
                         connected=connected, 
                         current_port=current_port,
                         sensor_data=current_data,
                         time=time)

@app.route('/api/spo2')
def api_spo2():
    """Return SPO2 data as JSON"""
    try:
        if connected:
            # Get fresh SPO2 data from sensor_data
            return jsonify({
                "spo2": sensor_data["spo2"],
                "spo2Avg": sensor_data["spo2Avg"],
                "valid": sensor_data["validReadings"],
                "last_updated": sensor_data["last_updated"]
            })
        else:
            # Return default data when not connected
            return jsonify({
                "spo2": 0,
                "spo2Avg": 0,
                "valid": False,
                "last_updated": None
            })
    except Exception as e:
        logger.error(f"Error in SPO2 API endpoint: {e}")
        return jsonify({
            "error": str(e),
            "spo2": 0,
            "spo2Avg": 0,
            "valid": False,
            "last_updated": None
        })

@app.route('/api/heart_rate')
def api_heart_rate():
    """Return heart rate data as JSON"""
    global sensor_data, heart_rate_buffer
    try:
        if connected and sensor_data:
            # Calculate number of readings in the buffer
            readings_count = len(heart_rate_buffer)
            
            return jsonify({
                "bpm": sensor_data["heartRateAvg"],  # 10-second average
                "valid": sensor_data["validReadings"],
                "quality": 100 if sensor_data["validReadings"] else 0,
                "last_updated": sensor_data["last_updated"],
                "readings_count": readings_count,
                "averaging_period": BUFFER_DURATION
            })
        else:
            return jsonify({
                "bpm": 0,
                "valid": False,
                "quality": 0,
                "last_updated": None,
                "readings_count": 0,
                "averaging_period": BUFFER_DURATION
            })
    except Exception as e:
        logger.error(f"Error in heart rate API endpoint: {e}")
        return jsonify({
            "error": str(e),
            "bpm": 0,
            "valid": False,
            "quality": 0,
            "last_updated": None,
            "readings_count": 0,
            "averaging_period": BUFFER_DURATION
        })



@app.route('/health')
def health_redirect():
    """Redirect from the old health page to heart rate page"""
    flash("The Health page has been moved to Heart Rate page", "info")
    return redirect(url_for('heart_rate_page'))

# Clean up on exit
def cleanup():
    disconnect_device()

# Templates directory
@app.route('/templates/<path:path>')
def serve_template(path):
    # This should be handled by Flask's template system, but added for completeness
    return render_template(path)

# Make sure all templates are created
def create_templates():
    """Create necessary template files if they don't exist"""
    templates_dir = "templates"
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)
    
    # Create a base template with the navigation
    base_path = os.path.join(templates_dir, "base.html")
    if not os.path.exists(base_path):
        with open(base_path, 'w') as f:
            f.write("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Synapse AR{% endblock %}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css">
    <style>
        .nav-link {
            padding: 12px 15px;
            font-weight: 500;
        }
        .nav-link.active {
            border-bottom: 3px solid #0d6efd;
        }
        .nav-tabs {
            border-bottom: 1px solid #dee2e6;
            margin-bottom: 20px;
        }
        .active-tab {
            background-color: #f8f9fa;
            border-radius: 0.25rem 0.25rem 0 0;
        }
        .status-icon {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 5px;
        }
        .status-icon.connected {
            background-color: #28a745;
        }
        .status-icon.disconnected {
            background-color: #dc3545;
        }
        .tab-btn {
            padding: 10px 15px;
            text-align: center;
            border: 1px solid #dee2e6;
            border-bottom: none;
            border-radius: 0.25rem 0.25rem 0 0;
            cursor: pointer;
            margin-right: 5px;
            background-color: #f8f9fa;
        }
        .tab-btn.active {
            background-color: #ffffff;
            border-bottom: 2px solid #fff;
            font-weight: bold;
        }
        .tab-content {
            border: 1px solid #dee2e6;
            border-radius: 0 0.25rem 0.25rem 0.25rem;
            padding: 20px;
            margin-top: -1px;
        }
    </style>
    {% block styles %}{% endblock %}
</head>
<body>
    <div class="container mt-4">
        <nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-4">
            <div class="container-fluid">
                <a class="navbar-brand" href="/">Synapse AR</a>
                <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                    <span class="navbar-toggler-icon"></span>
                </button>
                <div class="collapse navbar-collapse" id="navbarNav">
                    <ul class="navbar-nav">
                        <li class="nav-item">
                            <a class="nav-link {% if request.path == '/' %}active{% endif %}" href="/">
                                <i class="bi bi-house-fill"></i> Home
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link {% if request.path == '/medicines' %}active{% endif %}" href="/medicines">
                                <i class="bi bi-capsule"></i> Medicines
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link {% if request.path == '/schedule' %}active{% endif %}" href="/schedule">
                                <i class="bi bi-calendar"></i> Schedule
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link {% if request.path == '/emergency' %}active{% endif %}" href="/emergency">
                                <i class="bi bi-exclamation-octagon"></i> Emergency
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link {% if request.path == '/health' %}active{% endif %}" href="/health">
                                <i class="bi bi-heart-pulse-fill"></i> Health
                            </a>
                        </li>
                    </ul>
                    <div class="ms-auto d-flex align-items-center">
                        <span class="text-light me-2">
                            <span class="status-icon {% if connected %}connected{% else %}disconnected{% endif %}"></span>
                            {% if connected %}Connected{% else %}Disconnected{% endif %}
                        </span>
                    </div>
                </div>
            </div>
        </nav>

        {% block content %}{% endblock %}
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
    {% block scripts %}{% endblock %}
</body>
</html>
            """)
    
    # Create the heart rate template with tabs
    health_template_path = os.path.join(templates_dir, "health.html")
    if not os.path.exists(health_template_path):
        with open(health_template_path, 'w') as f:
            f.write("""
{% extends "base.html" %}

{% block title %}Health Monitoring - Synapse AR{% endblock %}

{% block styles %}
<style>
    .bpm-display {
        font-size: 6rem;
        font-weight: bold;
        color: #343a40;
        text-align: center;
        margin-top: 1rem;
        line-height: 1;
    }
    .bpm-unit {
        font-size: 1.5rem;
        color: #6c757d;
        position: relative;
        top: -2rem;
    }
    .bpm-label {
        font-size: 1.2rem;
        color: #6c757d;
        text-align: center;
    }
    .last-updated {
        font-size: 0.8rem;
        color: #6c757d;
        text-align: center;
    }
    .heart-icon {
        color: #dc3545;
        margin-right: 8px;
    }
    .health-card {
        border-radius: 15px;
        box-shadow: 0 4px 8px rgba(0,0,0,0.1);
        padding: 25px;
        height: 100%;
    }
    .health-card.primary {
        border-left: 4px solid #0d6efd;
    }
    .health-card.danger {
        border-left: 4px solid #dc3545;
    }
    .health-card.success {
        border-left: 4px solid #28a745;
    }
    .health-card h5 {
        font-weight: 600;
        margin-bottom: 20px;
        color: #343a40;
    }
    .btn-refresh {
        position: absolute;
        top: 10px;
        right: 10px;
        border-radius: 50%;
        width: 32px;
        height: 32px;
        padding: 0;
        display: flex;
        align-items: center;
        justify-content: center;
    }
    .heart-rate-container {
        margin-top: 20px;
        position: relative;
    }
    .progress-ring {
        transform: rotate(-90deg);
        transform-origin: 50% 50%;
    }
    .pulse-animation {
        animation: pulse 1.5s ease-in-out infinite;
    }
    @keyframes pulse {
        0% { transform: scale(1); }
        50% { transform: scale(1.05); }
        100% { transform: scale(1); }
    }
    .health-tab {
        cursor: pointer;
        padding: 15px;
        margin-right: 5px;
        border-radius: 5px 5px 0 0;
        border: 1px solid #dee2e6;
        background-color: #f8f9fa;
    }
    .health-tab.active {
        background-color: white;
        border-bottom: none;
        font-weight: bold;
    }
    .health-tab-content {
        display: none;
        padding: 20px;
        border: 1px solid #dee2e6;
        border-radius: 0 0 5px 5px;
    }
    .health-tab-content.active {
        display: block;
    }
    .circle-chart {
        width: 140px;
        height: 140px;
        margin: 0 auto;
        position: relative;
    }
    .circle-bg {
        fill: none;
        stroke: #eee;
        stroke-width: 3.8;
    }
    .circle-chart__circle {
        fill: none;
        stroke-width: 3.8;
        stroke-linecap: round;
        animation: circle-chart-fill 2s reverse;
        transform: rotate(-90deg);
        transform-origin: center;
    }
    .circle-chart__info {
        position: absolute;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        text-align: center;
    }
</style>
{% endblock %}

{% block content %}
<div class="row mb-4">
    <div class="col-12">
        <div class="d-flex">
            <h2 class="mb-4"><i class="bi bi-heart-pulse-fill text-danger"></i> Health Monitoring</h2>
            <div class="ms-auto">
                {% if not connected %}
                <a href="/" class="btn btn-primary"><i class="bi bi-plug-fill"></i> Connect Device</a>
                {% endif %}
            </div>
        </div>
    </div>
</div>

<div class="row mb-4">
    <div class="col-12">
        <div class="health-tabs">
            <div class="d-flex border-bottom mb-3">
                <div id="tab-heart-rate" class="health-tab active">
                    <i class="bi bi-heart-pulse-fill text-danger"></i> Heart Rate
                </div>
                <div id="tab-instructions" class="health-tab">
                    <i class="bi bi-info-circle-fill text-primary"></i> Instructions
                </div>
                <div id="tab-history" class="health-tab">
                    <i class="bi bi-graph-up text-success"></i> History
                </div>
            </div>
        </div>
        
        <!-- Heart Rate Tab Content -->
        <div id="content-heart-rate" class="health-tab-content active">
            <div class="row">
                <div class="col-md-6">
                    <div class="health-card primary position-relative">
                        <h5><i class="bi bi-heart-pulse-fill heart-icon"></i>Current Heart Rate</h5>
                        <button id="refresh-hr" class="btn btn-sm btn-outline-primary btn-refresh">
                            <i class="bi bi-arrow-clockwise"></i>
                        </button>
                        
                        {% if connected %}
                            <div class="heart-rate-container">
                                <div class="bpm-display {% if heart_rate.valid %}pulse-animation{% endif %}">
                                    <span id="bpmValue">
                                        {% if heart_rate.valid %}
                                            {{ heart_rate.bpm|int }}
                                        {% else %}
                                            --
                                        {% endif %}
                                    </span>
                                    <span class="bpm-unit">BPM</span>
                                </div>
                                
                                <div id="lastUpdated" class="last-updated mt-3">
                                    {% if heart_rate.last_updated %}
                                        Last updated: {{ ((time.time() - heart_rate.last_updated)|int) }} seconds ago
                                    {% else %}
                                        Waiting for data...
                                    {% endif %}
                                </div>
                                
                                <div class="text-center mt-4">
                                    <p id="heart-rate-status" class="mb-2">
                                        {% if heart_rate.valid %}
                                            {% set bpm = heart_rate.bpm|int %}
                                            {% if bpm < 60 %}
                                                <span class="badge bg-warning">Low Heart Rate</span>
                                            {% elif bpm > 100 %}
                                                <span class="badge bg-warning">Elevated Heart Rate</span>
                                            {% else %}
                                                <span class="badge bg-success">Normal Heart Rate</span>
                                            {% endif %}
                                        {% else %}
                                            <span class="badge bg-secondary">No Data</span>
                                        {% endif %}
                                    </p>
                                    <p id="measurement-note" class="text-muted small mb-0">
                                        Measured over 10 seconds
                                    </p>
                                </div>
                            </div>
                        {% else %}
                            <div class="alert alert-warning">
                                <p>Not connected to the device. Please connect from the home page.</p>
                                <a href="/" class="btn btn-primary">Go to Home</a>
                            </div>
                        {% endif %}
                    </div>
                </div>
                
                <div class="col-md-6">
                    <div class="health-card success">
                        <h5><i class="bi bi-info-circle-fill text-success me-2"></i>Heart Rate Info</h5>
                        <div class="row">
                            <div class="col-6">
                                <div class="circle-chart">
                                    <svg viewBox="0 0 36 36" class="circular-chart">
                                        <path class="circle-bg"
                                            d="M18 2.0845
                                                a 15.9155 15.9155 0 0 1 0 31.831
                                                a 15.9155 15.9155 0 0 1 0 -31.831"
                                        />
                                        <path class="circle-chart__circle" stroke="#28a745"
                                            stroke-dasharray="{% if heart_rate.valid %}{{ heart_rate.quality }}{% else %}0{% endif %}, 100"
                                            d="M18 2.0845
                                                a 15.9155 15.9155 0 0 1 0 31.831
                                                a 15.9155 15.9155 0 0 1 0 -31.831"
                                            id="quality-circle"
                                        />
                                        <div class="circle-chart__info">
                                            <text id="quality-percentage" x="18" y="20.35" class="circle-chart__percent">
                                                {% if heart_rate.valid %}{{ heart_rate.quality }}%{% else %}--{% endif %}
                                            </text>
                                        </div>
                                    </svg>
                                    <div class="text-center mt-2">Signal Quality</div>
                                </div>
                            </div>
                            <div class="col-6">
                                <h6>Healthy Range:</h6>
                                <p>60-100 BPM (Adult)</p>
                                
                                <h6 class="mt-3">Your Status:</h6>
                                <p id="status-text">
                                    {% if heart_rate.valid %}
                                        {% set bpm = heart_rate.bpm|int %}
                                        {% if bpm < 60 %}
                                            Below normal range
                                        {% elif bpm > 100 %}
                                            Above normal range
                                        {% else %}
                                            Within normal range
                                        {% endif %}
                                    {% else %}
                                        No data available
                                    {% endif %}
                                </p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Instructions Tab Content -->
        <div id="content-instructions" class="health-tab-content">
            <div class="health-card">
                <h5><i class="bi bi-info-circle-fill text-primary me-2"></i>How to Measure Your Heart Rate</h5>
                <div class="row">
                    <div class="col-md-7">
                        <ol class="mt-3">
                            <li class="mb-3">Make sure your device is connected to Synapse AR</li>
                            <li class="mb-3">Place your finger gently on the pulse sensor</li>
                            <li class="mb-3">Keep your finger still for at least 10 seconds</li>
                            <li class="mb-3">Wait for the measurement to complete</li>
                            <li class="mb-3">View your heart rate displayed in beats per minute (BPM)</li>
                        </ol>
                        
                        <div class="alert alert-info mt-4">
                            <i class="bi bi-lightbulb-fill me-2"></i>
                            <strong>Tip:</strong> For best results, sit in a relaxed position and avoid moving during measurement.
                        </div>
                    </div>
                    <div class="col-md-5">
                        <div class="card">
                            <div class="card-body text-center">
                                <h6 class="card-title">Normal Heart Rate Ranges</h6>
                                <table class="table table-sm">
                                    <thead>
                                        <tr>
                                            <th>Age</th>
                                            <th>BPM Range</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        <tr>
                                            <td>Adults</td>
                                            <td>60-100</td>
                                        </tr>
                                        <tr>
                                            <td>Children (7-15)</td>
                                            <td>70-110</td>
                                        </tr>
                                        <tr>
                                            <td>Athletes</td>
                                            <td>40-60</td>
                                        </tr>
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- History Tab Content -->
        <div id="content-history" class="health-tab-content">
            <div class="health-card">
                <h5><i class="bi bi-graph-up text-success me-2"></i>Heart Rate History</h5>
                <div class="text-center py-4">
                    <p>Heart rate history feature will be available in a future update.</p>
                    <p>This will allow you to track your heart rate measurements over time.</p>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
    // Tab switching functionality
    document.querySelectorAll('.health-tab').forEach(tab => {
        tab.addEventListener('click', function() {
            // Remove active class from all tabs
            document.querySelectorAll('.health-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.health-tab-content').forEach(c => c.classList.remove('active'));
            
            // Add active class to clicked tab
            this.classList.add('active');
            
            // Show corresponding content
            const contentId = 'content-' + this.id.split('-')[1];
            document.getElementById(contentId).classList.add('active');
        });
    });

    // Update heart rate data every 3 seconds
    function updateHeartRate() {
        if (!{{ connected|tojson }}) return;

        fetch('/api/heart_rate')
            .then(response => response.json())
            .then(data => {
                if (data.valid) {
                    // Update BPM display
                    const bpmValue = Math.round(data.bpm);
                    document.getElementById('bpmValue').innerText = bpmValue;
                    document.querySelector('.heart-rate-container').classList.add('pulse-animation');
                    
                    // Update signal quality circle
                    const qualityCircle = document.getElementById('quality-circle');
                    qualityCircle.setAttribute('stroke-dasharray', `${data.quality}, 100`);
                    document.getElementById('quality-percentage').textContent = `${data.quality}%`;
                    
                    // Update status text
                    let statusText = '';
                    let statusBadge = '';
                    if (bpmValue < 60) {
                        statusText = 'Below normal range';
                        statusBadge = '<span class="badge bg-warning">Low Heart Rate</span>';
                    } else if (bpmValue > 100) {
                        statusText = 'Above normal range';
                        statusBadge = '<span class="badge bg-warning">Elevated Heart Rate</span>';
                    } else {
                        statusText = 'Within normal range';
                        statusBadge = '<span class="badge bg-success">Normal Heart Rate</span>';
                    }
                    document.getElementById('status-text').textContent = statusText;
                    document.getElementById('heart-rate-status').innerHTML = statusBadge;
                    
                    // Calculate seconds since last update
                    const now = Math.floor(Date.now() / 1000);
                    const secondsAgo = now - data.last_updated;
                    document.getElementById('lastUpdated').innerText = 
                        'Last updated: ' + secondsAgo + ' seconds ago';
                } else {
                    document.getElementById('bpmValue').innerText = '--';
                    document.querySelector('.heart-rate-container').classList.remove('pulse-animation');
                    document.getElementById('quality-circle').setAttribute('stroke-dasharray', '0, 100');
                    document.getElementById('quality-percentage').textContent = '--';
                    document.getElementById('status-text').textContent = 'No data available';
                    document.getElementById('heart-rate-status').innerHTML = '<span class="badge bg-secondary">No Data</span>';
                    document.getElementById('lastUpdated').innerText = 'Waiting for data...';
                }
            })
            .catch(error => {
                console.error('Error fetching heart rate data:', error);
            });
    }

    // Update initially and then every 3 seconds
    updateHeartRate();
    setInterval(updateHeartRate, 3000);
    
    // Refresh button functionality
    document.getElementById('refresh-hr').addEventListener('click', function() {
        this.querySelector('i').classList.add('fa-spin');
        updateHeartRate();
        setTimeout(() => {
            this.querySelector('i').classList.remove('fa-spin');
        }, 1000);
    });
</script>
{% endblock %}
""")
    
    # Update index template to ensure health link is present
    index_path = os.path.join(templates_dir, "index.html")
    if not os.path.exists(index_path):
        with open(index_path, 'w') as f:
            f.write("""
{% extends "base.html" %}

{% block title %}Home - Synapse AR{% endblock %}

{% block content %}
<div class="row">
    <div class="col-md-12">
        <div class="card">
            <div class="card-header bg-primary text-white">
                <h5 class="mb-0">Synapse AR Device Connection</h5>
            </div>
            <div class="card-body">
                {% for message in get_flashed_messages() %}
                    <div class="alert alert-warning">{{ message }}</div>
                {% endfor %}

                <div class="mb-4">
                    <h4>Connection Status</h4>
                    <p>
                        {% if connected %}
                            <span class="badge bg-success">Connected to {{ current_port }}</span>
                        {% else %}
                            <span class="badge bg-danger">Not Connected</span>
                        {% endif %}
                    </p>
                </div>

                {% if not connected %}
                    <div class="mb-4">
                        <h4>Connect to Device</h4>
                        <form action="/scan" method="post" class="mb-3">
                            <button type="submit" class="btn btn-primary">Scan for Ports</button>
                        </form>

                        {% if available_ports %}
                            <form action="/connect" method="post">
                                <div class="mb-3">
                                    <label for="port" class="form-label">Select Port</label>
                                    <select name="port" id="port" class="form-select">
                                        {% for port in available_ports %}
                                            <option value="{{ port.device }}" {{ 'selected' if port.likely_esp32 else '' }}>
                                                {{ port.device }} - {{ port.description }} 
                                                {% if port.likely_esp32 %}(Likely ESP32){% endif %}
                                            </option>
                                        {% endfor %}
                                    </select>
                                </div>
                                <button type="submit" class="btn btn-success">Connect</button>
                            </form>
                        {% else %}
                            <div class="alert alert-info">
                                No ports found. Make sure your device is connected and click "Scan for Ports".
                            </div>
                        {% endif %}
                    </div>
                {% else %}
                    <form action="/disconnect" method="post">
                        <button type="submit" class="btn btn-danger">Disconnect</button>
                    </form>
                    
                    <div class="mt-4">
                        <h4>Device Functions</h4>
                        <div class="row">
                            <div class="col-md-3 mb-3">
                                <a href="/medicines" class="btn btn-primary w-100">
                                    <i class="bi bi-capsule"></i> Medicines
                                </a>
                            </div>
                            <div class="col-md-3 mb-3">
                                <a href="/schedule" class="btn btn-primary w-100">
                                    <i class="bi bi-calendar"></i> Schedule
                                </a>
                            </div>
                            <div class="col-md-3 mb-3">
                                <a href="/emergency" class="btn btn-primary w-100">
                                    <i class="bi bi-exclamation-octagon"></i> Emergency
                                </a>
                            </div>
                            <div class="col-md-3 mb-3">
                                <a href="/health" class="btn btn-primary w-100">
                                    <i class="bi bi-heart-pulse-fill"></i> Health
                                </a>
                            </div>
                        </div>
                    </div>
                {% endif %}
            </div>
        </div>
    </div>
</div>
{% endblock %}
""")

# Function to generate dotted line for gesture visualization
def drawline(img, pt1, pt2, color, thickness=1, style='dotted', gap=20):
    if not MEDIAPIPE_AVAILABLE:
        return
        
    dist = ((pt1[0] - pt2[0]) ** 2 + (pt1[1] - pt2[1]) ** 2) ** .5
    pts = []
    for i in np.arange(0, dist, gap):
        r = i / dist
        x = int((pt1[0] * (1 - r) + pt2[0] * r) + .5)
        y = int((pt1[1] * (1 - r) + pt2[1] * r) + .5)
        p = (x, y)
        pts.append(p)
    if style == 'dotted':
        for p in pts:
            cv2.circle(img, p, thickness, color, -1)
    else:
        s = pts[0]
        e = pts[0]
        i = 0
        for p in pts:
            s = e
            e = p
            if i % 2 == 1:
                cv2.line(img, s, e, color, thickness)
            i += 1

# Function to draw page indicators
def draw_page_indicators(img, current_page, max_pages):
    if not MEDIAPIPE_AVAILABLE:
        return
        
    start_x = 10
    y = 70
    circle_radius = 10
    spacing = 30
    
    for i in range(max_pages):
        center = (start_x + i * spacing, y)
        if i == current_page:
            cv2.circle(img, center, circle_radius, (0, 255, 0), -1)  # Filled circle for current page
        else:
            cv2.circle(img, center, circle_radius, (128, 128, 128), 2)  # Empty circle for other pages

# Function to switch pages via serial command
def switch_page(arduino_ser):
    global current_page
    try:
        # Use the existing send_command function instead of direct serial access
        response = send_command("button_press")
        logger.info(f"Page switch command response: {response}")
        
        # Check for successful page change
        if "Page changed successfully" in response:
            current_page = (current_page + 1) % MAX_PAGES
            logger.info(f"Updated page counter to: {current_page}")
        
        return True
    except Exception as e:
        logger.error(f"Error sending page switch command: {e}")
        return False

# Main gesture detection thread function
def gesture_detection_thread(stop_event):
    global ser, current_page
    
    if not MEDIAPIPE_AVAILABLE:
        logger.error("MediaPipe is not available. Cannot run gesture detection.")
        return
        
    if not connected or not ser:
        logger.error("Cannot start gesture detection - not connected to device")
        return
    
    logger.info("Starting gesture detection thread")
    
    # Mediapipe setup
    mp_drawing = mp.solutions.drawing_utils
    hand_mpDraw = mp.solutions.drawing_utils
    mp_hands = mp.solutions.hands
    
    # Initialize camera
    cap = cv2.VideoCapture(0)  # Try 0 first, can be changed to other camera indices
    if not cap.isOpened():
        logger.error("Failed to open camera with index 0, trying index 1")
        cap = cv2.VideoCapture(1)
        if not cap.isOpened():
            logger.error("Failed to open camera with index 1, trying index 2")
            cap = cv2.VideoCapture(2)
            if not cap.isOpened():
                logger.error("Failed to open any camera")
                return
    
    distance = -1
    last_switch_time = 0  # For debouncing
    
    # Start MediaPipe Hands
    with mp_hands.Hands(
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
            max_num_hands=2) as hands:
            
        while not stop_event.is_set() and cap.isOpened():
            # Read frame
            success, image = cap.read()
            if not success:
                logger.error("Failed to read frame from camera")
                time.sleep(0.1)
                continue
                
            # Process image
            image = cv2.flip(image, 1)
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            image.flags.writeable = False
            results = hands.process(image)
            image.flags.writeable = True
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            
            # Draw instructions and page indicators
            cv2.putText(image, "Join index finger and thumb to switch pages", (10, 30),
                      cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            draw_page_indicators(image, current_page, MAX_PAGES)
            cv2.putText(image, f"Current Page: {current_page}", (10, 100),
                      cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            
            # Process hand landmarks
            if results.multi_hand_landmarks:
                for hand_landmarks in results.multi_hand_landmarks:
                    lmList = []
                    for id, lm in enumerate(hand_landmarks.landmark):
                        h, w, c = image.shape
                        cx, cy = int(lm.x * w), int(lm.y * h)
                        lmList.append([id, cx, cy])
                        tips = [0, 4, 8, 12, 16, 20]
                        if id in tips:
                            cv2.circle(image, (cx, cy), 15, (0, 255, 0), cv2.FILLED)
                    
                    # Check if we have enough landmarks for index and thumb
                    if len(lmList) > 8:
                        # Calculate distance between index and thumb
                        distance = math.hypot(lmList[8][1] - lmList[4][1], lmList[8][2] - lmList[4][2])
                        
                        # Check if fingers are close enough to trigger page switch
                        current_time = time.time()
                        if distance < 50 and (current_time - last_switch_time) > 1.0:
                            logger.info(f"Fingers close! Distance: {distance}")
                            
                            # Use lock when accessing serial
                            with connection_lock:
                                switch_page(ser)
                                
                            last_switch_time = current_time
                        
                        # Draw line between index and thumb
                        drawline(image, (lmList[4][1], lmList[4][2]), (lmList[8][1], lmList[8][2]), (0, 0, 255),
                               thickness=1, style='dotted', gap=10)
                        
                        # Add distance text to screen
                        cv2.putText(image, f"Distance: {int(distance)}", (10, 150),
                                  cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    
                    # Draw landmarks
                    mp_drawing.draw_landmarks(
                        image, hand_landmarks, mp_hands.HAND_CONNECTIONS,
                        landmark_drawing_spec=hand_mpDraw.DrawingSpec(color=(0, 255, 0)),
                        connection_drawing_spec=hand_mpDraw.DrawingSpec(color=(255, 0, 0)))
            
            # Display the frame
            cv2.imshow('AR Gesture Control', image)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
                
            # Small delay to prevent high CPU usage
            time.sleep(0.01)
    
    # Release resources
    cap.release()
    cv2.destroyAllWindows()
    logger.info("Gesture detection thread stopped")

# Function to start gesture detection
def start_gesture_detection():
    global gesture_enabled, gesture_thread, gesture_stop_event
    
    if not MEDIAPIPE_AVAILABLE:
        logger.error("MediaPipe is not available. Cannot start gesture detection.")
        return False
    
    if gesture_enabled:
        logger.warning("Gesture detection already running")
        return False
        
    if not connected:
        logger.error("Cannot start gesture detection - not connected to device")
        return False
    
    # Create and start thread
    gesture_stop_event.clear()
    gesture_thread = threading.Thread(
        target=gesture_detection_thread, 
        args=(gesture_stop_event,),
        daemon=True
    )
    gesture_thread.start()
    gesture_enabled = True
    logger.info("Gesture detection started")
    return True

# Function to stop gesture detection
def stop_gesture_detection():
    global gesture_enabled, gesture_thread, gesture_stop_event
    
    if not gesture_enabled:
        logger.warning("Gesture detection not running")
        return False
    
    # Signal thread to stop
    gesture_stop_event.set()
    
    # Wait for thread to finish (with timeout)
    if gesture_thread and gesture_thread.is_alive():
        gesture_thread.join(timeout=2.0)
    
    gesture_enabled = False
    logger.info("Gesture detection stopped")
    return True

@app.route('/gesture')
def gesture_page():
    """Redirect gesture control page to home with a message"""
    flash("Gesture detection is only available via the standalone simple_gesture.py script.", "info")
    return redirect(url_for('index'))

@app.route('/predictions')
def predictions_page():
    """Render the predictions page with current sensor data"""
    return render_template('predictions.html', sensor_data=sensor_data)

@app.route('/api/predict/hypertension', methods=['POST'])
def predict_hypertension():
    """API endpoint for hypertension risk prediction"""
    if not JOBLIB_AVAILABLE or hypertension_model is None:
        return jsonify({"error": "Hypertension model not available"}), 503
    
    try:
        data = request.json
        # Extract features
        features = {
            'age': data.get('age', 45),
            'male': data.get('male', 0),
            'sysBP': data.get('sysBP', 120),
            'diaBP': data.get('diaBP', 80),
            'heartRate': data.get('heartRate', 75)
        }
        
        # Create feature array for prediction
        import pandas as pd
        features_df = pd.DataFrame([features])
        
        # Make prediction
        prediction = int(hypertension_model.predict(features_df)[0])
        probability = float(hypertension_model.predict_proba(features_df)[0][1])
        
        return jsonify({
            "risk": prediction,
            "probability": probability,
            "features": features
        })
    except Exception as e:
        logger.error(f"Error in hypertension prediction: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/predict/cardiac', methods=['POST'])
def predict_cardiac():
    """API endpoint for cardiac arrest risk prediction"""
    if not JOBLIB_AVAILABLE or cardiac_model is None:
        return jsonify({"error": "Cardiac arrest model not available"}), 503
    
    try:
        data = request.json
        # Extract features
        features = {
            'Age': data.get('Age', 45),
            'Gender': data.get('Gender', 'Male'),
            'Heart_Rate': data.get('Heart_Rate', 75),
            'Systolic_BP': data.get('Systolic_BP', 120),
            'Diastolic_BP': data.get('Diastolic_BP', 80),
            'BMI': data.get('BMI', 24.5),
            'Age_BMI': data.get('Age', 45) * data.get('BMI', 24.5)
        }
        
        # Add derived features
        features['Pulse_Pressure'] = features['Systolic_BP'] - features['Diastolic_BP']
        
        # Add categorical features
        age_value = features['Age']
        if age_value <= 35:
            features['Age_Bucket'] = 'young'
        elif age_value <= 50:
            features['Age_Bucket'] = 'mid'
        elif age_value <= 65:
            features['Age_Bucket'] = 'senior'
        else:
            features['Age_Bucket'] = 'elder'
            
        bmi_value = features['BMI']
        if bmi_value <= 18.5:
            features['BMI_Category'] = 'underweight'
        elif bmi_value <= 24.9:
            features['BMI_Category'] = 'normal'
        elif bmi_value <= 29.9:
            features['BMI_Category'] = 'overweight'
        else:
            features['BMI_Category'] = 'obese'
        
        # Create feature array for prediction
        import pandas as pd
        features_df = pd.DataFrame([features])
        
        # Make prediction
        prediction = int(cardiac_model.predict(features_df)[0])
        
        return jsonify({
            "risk": prediction,
            "features": features
        })
    except Exception as e:
        logger.error(f"Error in cardiac arrest prediction: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/predict/anxiety', methods=['POST'])
def predict_anxiety():
    """API endpoint for anxiety assessment"""
    if not JOBLIB_AVAILABLE or anxiety_model is None or anxiety_imputer is None:
        return jsonify({"error": "Anxiety model not available"}), 503
    
    try:
        data = request.json
        # Extract features
        features = {
            'Age': data.get('Age', 12),
            'Number of Siblings': data.get('Siblings', 1),
            'Number of Bio. Parents': data.get('BioParents', 2),
            'Poverty Status': data.get('Poverty', 0),
            'Number of Impairments': data.get('Impairments', 0),
            'Number of Type A Stressors': data.get('StressorsA', 0),
            'Number of Type B Stressors': data.get('StressorsB', 0),
            'Frequency Temper Tantrums': data.get('Tantrums', 0),
            'Frequency Irritable Mood': data.get('Irritable', 0),
            'Number of Sleep Disturbances': data.get('Sleep', 0),
            'Number of Physical Symptoms': data.get('Physical', 0),
            'Number of Sensory Sensitivities': data.get('Sensory', 0),
            'Family History - Substance Abuse': data.get('Substance', 0),
            'Family History - Psychiatric Diagnosis': data.get('Psychiatric', 0)
        }
        
        # Create feature array for prediction
        import pandas as pd
        import numpy as np
        features_df = pd.DataFrame([features])
        
        # Apply the same imputation as during training
        features_imputed = anxiety_imputer.transform(features_df)
        
        # Make prediction
        prediction = int(anxiety_model.predict(features_imputed)[0])
        probability = float(anxiety_model.predict_proba(features_imputed)[0][1])
        
        return jsonify({
            "risk": prediction,
            "probability": probability,
            "features": features
        })
    except Exception as e:
        logger.error(f"Error in anxiety prediction: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/gesture/start', methods=['POST'])
def api_start_gesture():
    """Start gesture detection"""
    if not MEDIAPIPE_AVAILABLE:
        return jsonify({"success": False, "error": "MediaPipe is not installed. Please use simple_gesture.py instead."}), 400
        
    if not connected:
        return jsonify({"success": False, "error": "Device not connected"}), 400
        
    result = start_gesture_detection()
    return jsonify({"success": result})

@app.route('/api/gesture/stop', methods=['POST'])
def api_stop_gesture():
    """Stop gesture detection"""
    result = stop_gesture_detection()
    return jsonify({"success": result})

@app.route('/api/gesture/status')
def api_gesture_status():
    """Get gesture detection status"""
    return jsonify({
        "enabled": gesture_enabled,
        "running": gesture_thread is not None and gesture_thread.is_alive(),
        "mediapipe_available": MEDIAPIPE_AVAILABLE
    })

@app.route('/api/sensor_data', methods=['GET'])
def api_sensor_data():
    """API endpoint to get current sensor data"""
    try:
        # Get current vital signs
        current_time = time.time()
        
        # Use the global sensor_data instead of non-existent vital_signs variable
        return jsonify({
            "status": "success",
            "sensor_data": sensor_data
        })
    except Exception as e:
        app.logger.error(f"Error fetching sensor data: {e}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

# Add the following route for updating vital signs programmatically

@app.route('/update_vital_signs', methods=['POST'])
def update_vital_signs():
    """API endpoint to update vital signs programmatically for testing"""
    try:
        global sensor_data
        # Get vital sign data from request
        data = request.json
        
        if not data:
            return jsonify({
                "status": "error",
                "message": "No data provided"
            }), 400
        
        # Update the global sensor_data dict with received data
        sensor_data.update(data)
        
        # Log the update
        app.logger.info(f"Vital signs updated: {data}")
        
        return jsonify({
            "status": "success",
            "message": "Vital signs updated successfully"
        })
    except Exception as e:
        app.logger.error(f"Error updating vital signs: {e}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

if __name__ == "__main__":
    # Create templates
    create_templates()
    
    # Start the Flask app
    try:
        logger.info("Starting Synapse AR Web Interface...")
        app.run(host='0.0.0.0', port=8081, debug=True)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        cleanup() 