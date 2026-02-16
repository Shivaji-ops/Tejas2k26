import cv2
import numpy as np
import os
import google.generativeai as genai
from PIL import Image
import io
import absl.logging
import time
import threading
from queue import Queue
import mediapipe as mp
import math
import serial
import serial.tools.list_ports
import datetime

# Suppress TensorFlow warnings
absl.logging.set_verbosity(absl.logging.ERROR)

# Configure Google Generative AI
GOOGLE_API_KEY = "AIzaSyAc5ng7BCmJYxvwZyVH-EygQEe1FpjxmtE"
os.environ["GOOGLE_API_KEY"] = GOOGLE_API_KEY
genai.configure(api_key=GOOGLE_API_KEY)

# Medicine list and schedule
MEDICINES = {
    "DICLOWIN 650": {"schedule_time": "9 PM", "taken_today": False, "display": "DICLOWIN 650 - 9 PM"},
    "IMEGLYN 1000": {"schedule_time": "8 AM", "taken_today": False, "display": "IMEGLYN 1000 - 8 AM"},
    "Crocin": {"schedule_time": "2 PM", "taken_today": False, "display": "Crocin - 2 PM"},
    "Dolo": {"schedule_time": "6 PM", "taken_today": False, "display": "Dolo - 6 PM"}
}

def setup_arduino():
    # List all available ports
    ports = list(serial.tools.list_ports.comports())
    print("Available ports:")
    for port in ports:
        print(f"  {port.device}")

    # Try to find Arduino port
    arduino_port = None
    for port in ports:
        if 'usbserial' in port.device.lower():
            arduino_port = port.device
            print(f"Using Arduino port: {arduino_port}")
            break

    if arduino_port is None:
        print("No Arduino port found!")
        return None

    try:
        # Initialize Arduino serial connection with 115200 baud rate
        arduino = serial.Serial(arduino_port, 115200, timeout=1)
        time.sleep(2)  # Wait for Arduino to reset
        print("Arduino connected successfully!")
        
        # Flush any existing data
        arduino.reset_input_buffer()
        arduino.reset_output_buffer()
        
        # Send test command
        arduino.write(b'test\n')
        arduino.flush()
        
        # Read response
        response = arduino.readline().decode('utf-8', errors='replace').strip()
        print(f"Arduino response: {response}")
        
        return arduino
        
    except Exception as e:
        print(f"Failed to connect to Arduino: {e}")
        return None

def switch_page(arduino):
    if arduino is None:
        return
        
    try:
        # Send button press command
        cmd = "button_press\n"
        print(f"Sending command: {cmd.strip()}")
        arduino.write(cmd.encode())
        arduino.flush()
        
        # Read responses until we get CMD_END
        while True:
            response = arduino.readline().decode('utf-8', errors='replace').strip()
            if not response:
                break
            print(f"Arduino response: {response}")
            if response == "CMD_END":
                break
                
        time.sleep(0.1)  # Small delay
        
    except Exception as e:
        print(f"Error sending button press command: {e}")

def send_medicine_to_arduino(arduino, medicine_name, status):
    """Send detected medicine information to Arduino"""
    if arduino is None:
        return
        
    try:
        # Format command: detected_med Medicine Name|Status
        cmd = f"detected_med {medicine_name}|{status}\n"
        print(f"Sending medicine to Arduino: {cmd.strip()}")
        arduino.write(cmd.encode())
        arduino.flush()
        
        # Read responses until we get CMD_END
        while True:
            response = arduino.readline().decode('utf-8', errors='replace').strip()
            if not response:
                break
            print(f"Arduino response: {response}")
            if response == "CMD_END":
                break
                
        time.sleep(0.1)  # Small delay
        
    except Exception as e:
        print(f"Error sending medicine info: {e}")

class SharedVideoStream:
    def __init__(self, src=1):
        self.stream = cv2.VideoCapture(src)
        self.stream.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.stream.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.stream.set(cv2.CAP_PROP_FPS, 30)
        self.stopped = False
        self.frame = None
        self.lock = threading.Lock()
        
    def start(self):
        threading.Thread(target=self.update, daemon=True).start()
        return self
        
    def update(self):
        while not self.stopped:
            ret, frame = self.stream.read()
            if not ret:
                self.stopped = True
                break
            with self.lock:
                self.frame = frame
            
    def read(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()
        
    def stop(self):
        self.stopped = True
        self.stream.release()

def process_frame_with_gemini(frame):
    # Convert frame to PIL Image
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    pil_image = Image.fromarray(frame_rgb)
    
    # Convert to bytes
    img_byte_arr = io.BytesIO()
    pil_image.save(img_byte_arr, format="PNG", quality=85)
    img_bytes = img_byte_arr.getvalue()
    
    # Create model instance and generate description
    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = "If there is a medicine in this image, respond ONLY with its name in maximum 17 characters. If no medicine, respond with 'No medicine'. No other text."
    
    try:
        response = model.generate_content(
            [prompt, {"mime_type": "image/png", "data": img_bytes}],
            generation_config=genai.types.GenerationConfig(
                temperature=0.1
            )
        )
        # Clean and truncate the response
        text = response.text.strip()
        if len(text) > 17:
            text = text[:17]
        print(f"Medicine Detection: {text}")  # Print to terminal
        return text
    except Exception as e:
        print(f"Error in API call: {e}")
        return None

def check_medicine_schedule(medicine_name):
    """Check if the detected medicine should be taken now based on schedule"""
    # Get current time
    current_time = datetime.datetime.now().time()
    current_hour = current_time.hour
    
    # Check if medicine exists in our list
    medicine_name = medicine_name.upper()
    for med, info in MEDICINES.items():
        # Check for partial name match (in case of incomplete detection)
        if medicine_name in med.upper() or med.upper() in medicine_name:
            # Parse schedule time
            schedule_parts = info["schedule_time"].split()
            hour = int(schedule_parts[0])
            am_pm = schedule_parts[1].upper()
            
            # Convert to 24-hour format
            if am_pm == "PM" and hour < 12:
                schedule_hour = hour + 12
            elif am_pm == "AM" and hour == 12:
                schedule_hour = 0
            else:
                schedule_hour = hour
            
            # Calculate time difference (allowing for up to 2 hours early or 3 hours late)
            time_diff = current_hour - schedule_hour
            
            # If it's within the window for taking medicine
            if -2 <= time_diff <= 3:
                status = f"Take this medicine now ({info['schedule_time']})."
                if info["taken_today"]:
                    status = f"Already taken today ({info['schedule_time']})."
                return True, info["display"], info["schedule_time"], status
            else:
                return False, info["display"], info["schedule_time"], f"Should be taken at {info['schedule_time']}."
    
    # If medicine not found in our list
    return False, None, None, "Medicine not in schedule."

class SharedState:
    def __init__(self):
        self.current_page = 0
        self.MAX_PAGES = 6
        self.current_medicine = "Scanning..."
        self.medicine_status = ""
        self.lock = threading.Lock()
        self.frame_to_show = None
        self.should_quit = False

def integrated_processing_thread(shared_vs, shared_state, arduino):
    print("Starting integrated AR system...")
    
    mp_drawing = mp.solutions.drawing_utils
    mp_hands = mp.solutions.hands
    
    last_api_call = 0
    api_call_interval = 2.0
    processing = False
    last_switch_time = 0
    
    def process_frame_async(frame):
        nonlocal processing
        medicine_name = process_frame_with_gemini(frame)
        if medicine_name and medicine_name != "No medicine":
            should_take, display_name, schedule_time, status = check_medicine_schedule(medicine_name)
            
            with shared_state.lock:
                # Store both the medicine name and whether it should be taken
                shared_state.current_medicine = display_name if display_name else medicine_name
                shared_state.medicine_status = status
                
                # Print detailed information about the medicine
                if display_name:
                    print(f"Detected: {display_name}")
                    print(f"Recommendation: {status}")
                    
                    # Send medicine info to Arduino
                    send_medicine_to_arduino(arduino, display_name, status)
                else:
                    print(f"Detected medicine '{medicine_name}' not in schedule")
                    
                    # Send even unscheduled medicine to Arduino
                    send_medicine_to_arduino(arduino, medicine_name, "Medicine not in schedule")
        else:
            with shared_state.lock:
                shared_state.current_medicine = medicine_name or "No medicine"
                shared_state.medicine_status = ""
        
        processing = False
    
    with mp_hands.Hands(
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
            max_num_hands=2) as hands:
            
        while not shared_state.should_quit:
            try:
                frame = shared_vs.read()
                if frame is None:
                    print("No frame received")
                    continue
                
                # Process frame for hand detection
                image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image.flags.writeable = False
                results = hands.process(image)
                image.flags.writeable = True
                image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                
                # Process medicine detection
                current_time = time.time()
                if not processing and current_time - last_api_call >= api_call_interval:
                    processing = True
                    last_api_call = current_time
                    threading.Thread(target=process_frame_async, args=(frame.copy(),), daemon=True).start()
                
                # Draw medicine detection result
                with shared_state.lock:
                    # Create a semi-transparent overlay for medicine info
                    if shared_state.current_medicine != "No medicine" and shared_state.current_medicine != "Scanning...":
                        # Create a dark overlay for better text visibility
                        overlay = image.copy()
                        cv2.rectangle(overlay, (0, 0), (640, 100), (0, 0, 0), -1)
                        cv2.addWeighted(overlay, 0.5, image, 0.5, 0, image)
                        
                        # Display detected medicine with larger text
                        cv2.putText(image, f"Medicine: {shared_state.current_medicine}", (10, 30), 
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                        
                        # Display recommendation status with more prominence
                        if shared_state.medicine_status:
                            status_color = (0, 255, 0) if "Take this medicine now" in shared_state.medicine_status else (0, 165, 255)
                            cv2.putText(image, shared_state.medicine_status, (10, 70), 
                                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
                            
                            # Add a visual indicator for "take now"
                            if "Take this medicine now" in shared_state.medicine_status:
                                # Draw a green checkmark symbol
                                cv2.line(image, (570, 40), (585, 55), (0, 255, 0), 4)
                                cv2.line(image, (585, 55), (610, 25), (0, 255, 0), 4)
                    else:
                        # Display scanning status
                        cv2.putText(image, f"Medicine: {shared_state.current_medicine}", (10, 30), 
                                  cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                
                # Draw gesture instructions
                cv2.putText(image, "Join index finger and thumb to switch pages", (10, 90),
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
                        
                        if len(lmList) > 8:
                            # Calculate distance between index and thumb
                            distance = math.hypot(lmList[8][1] - lmList[4][1], lmList[8][2] - lmList[4][2])
                            
                            # Check for gesture
                            current_time = time.time()
                            if distance < 50 and (current_time - last_switch_time) > 1.0:
                                print(f"Gesture detected! Distance: {distance}")
                                with shared_state.lock:
                                    shared_state.current_page = (shared_state.current_page + 1) % shared_state.MAX_PAGES
                                switch_page(arduino)
                                last_switch_time = current_time
                            
                            # Draw line between index and thumb
                            cv2.line(image, (lmList[4][1], lmList[4][2]), 
                                    (lmList[8][1], lmList[8][2]), (0, 0, 255), 2)
                            
                            # Show distance
                            cv2.putText(image, f"Distance: {int(distance)}", (10, 150),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                        
                        # Draw landmarks
                        mp_drawing.draw_landmarks(
                            image, hand_landmarks, mp_hands.HAND_CONNECTIONS)
                
                # Show current page
                with shared_state.lock:
                    cv2.putText(image, f"Current Page: {shared_state.current_page}", (10, 120),
                              cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                
                # Update frame to show
                with shared_state.lock:
                    shared_state.frame_to_show = image.copy()
                    
            except Exception as e:
                print(f"Error in processing loop: {e}")
                continue

def main():
    print("Starting integrated AR system...")
    
    try:
        # Setup Arduino connection
        arduino = setup_arduino()
        
        # Set Arduino to medicine page (page 0) at startup
        if arduino:
            print("Setting Arduino to medicine page...")
            arduino.write(b"page 0\n")
            arduino.flush()
            time.sleep(0.5)
            
            # Read response
            while arduino.in_waiting:
                response = arduino.readline().decode('utf-8', errors='replace').strip()
                print(f"Arduino response: {response}")
        
        # Initialize shared video stream and state
        shared_vs = SharedVideoStream(src=0).start()
        shared_state = SharedState()
        time.sleep(2.0)  # Allow camera to warm up
        
        if shared_vs.read() is None:
            print("Error: Could not open camera!")
            if arduino:
                arduino.close()
            return
        
        # Create window in main thread
        window_name = 'Integrated_AR_System'
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.moveWindow(window_name, 0, 0)
        cv2.resizeWindow(window_name, 1280, 720)
        
        # Create and start processing thread
        processing_thread = threading.Thread(
            target=integrated_processing_thread, 
            args=(shared_vs, shared_state, arduino),
            daemon=True
        )
        processing_thread.start()
        
        print("\nPress 'q' to quit")
        print("System is running. You will see medicine detection results here:")
        print("\nMedicine Schedule:")
        for med, info in MEDICINES.items():
            print(f"- {med}: {info['schedule_time']}")
        
        # Main display loop
        while True:
            with shared_state.lock:
                frame = shared_state.frame_to_show
                if frame is not None:
                    cv2.imshow(window_name, frame)
                    shared_state.frame_to_show = None
            
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                shared_state.should_quit = True
                break
            
            # Handle medicine taken confirmation with 't' key
            if key == ord('t'):
                with shared_state.lock:
                    med_name = shared_state.current_medicine
                    if med_name in MEDICINES:
                        MEDICINES[med_name]["taken_today"] = True
                        print(f"Marked {med_name} as taken for today")
        
        # Wait for processing thread to finish
        processing_thread.join(timeout=1.0)
        
    except Exception as e:
        print(f"Error in main: {e}")
    
    finally:
        # Cleanup
        if 'shared_vs' in locals():
            shared_vs.stop()
        cv2.destroyAllWindows()
        if 'arduino' in locals() and arduino:
            arduino.close()
        print("System stopped")

if __name__ == "__main__":
    main() 