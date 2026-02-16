#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <esp_now.h>
#include <WiFi.h>

// MAC Address of the receiver ESP32 (AR device)
uint8_t receiverMacAddress[] = {0xEC, 0x62, 0x60, 0x99, 0xC0, 0x40}; // AR device MAC address

// Data structure for sensor readings
typedef struct sensor_readings {
  float heartRate;
  int heartRateAvg;
  int spo2;
  int spo2Avg;
  float temperature;
  bool fallDetected;
  bool validReadings;
} sensor_readings;

// Create a sensor readings object
sensor_readings sensorData;

// ESP-NOW peer info
esp_now_peer_info_t peerInfo;

// Callback when data is sent
void OnDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  // Serial.print("Last Packet Send Status: ");
  // Serial.println(status == ESP_NOW_SEND_SUCCESS ? "Delivery Success" : "Delivery Fail");
}

// I2C Pins for ESP32 - Using OLED pins
#define OLED_SDA 21
#define OLED_SCL 22
#define MPU_SDA 18
#define MPU_SCL 19

// Buzzer Pin
#define BUZZER_PIN 5

// Create separate I2C instances
TwoWire I2C_OLED = TwoWire(0); // SHARED OLED
TwoWire I2C_MPU = TwoWire(1);

// OLED Display Settings
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
#define SCREEN_ADDRESS 0x3C
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &I2C_OLED, OLED_RESET);

// MPU6050 Settings
const int MPU_ADDR = 0x68;
int16_t accelerometer_x, accelerometer_y, accelerometer_z;
int16_t gyro_x, gyro_y, gyro_z;
int16_t temperature;

// Fall detection parameters
#define FALL_THRESHOLD 0.46       // 30% less sensitive (0.35 * 1.3)
#define IMPACT_THRESHOLD 2.6     // 30% less sensitive (2.0 * 1.3)
#define MIN_FALL_DURATION 4      // Increased from 3 to 4 for more confirmation time
#define FALL_TIMEOUT 5000        // Time to keep fall alert active (5 seconds)
#define BUZZER_ALERT_TIME 10000  // 10 seconds buzzer duration
#define FALL_RECOVERY_CHECK 4    // Increased from 3 to 4 for more stable recovery check
bool fallDetected = false;
unsigned long fallDetectedTime = 0;
unsigned long fallStartTime = 0;
float accelerationMagnitude = 0;   // Total acceleration magnitude
float previousAccMagnitude = 0;    // For fall trend detection
float restingAccMagnitude = 0;     // Baseline at rest (should be ~1g)
float peakAcceleration = 0;        // Track peak during potential fall
bool potentialFall = false;        // Track fall state
#define FALL_SAMPLES 6             // Reduced samples for faster response
float accMagnitudeBuffer[FALL_SAMPLES] = {0}; // Buffer for smoothing
int accBufferIndex = 0;
#define REST_SAMPLES 20            // Fewer samples for faster startup
int restSampleCount = 0;

// Buzzer variables
bool buzzerActive = false;
unsigned long buzzerStartTime = 0;

// New fall detection variables from example code
float ax = 0, ay = 0, az = 0, gx = 0, gy = 0, gz = 0;
boolean fall = false; 
boolean trigger1 = false; 
boolean trigger2 = false; 
boolean trigger3 = false; 
byte trigger1count = 0; 
byte trigger2count = 0; 
byte trigger3count = 0; 
int angleChange = 0;

// Heart Rate calculation variables (Modified for External Sensor)
float beatsPerMinute = 0;
int beatAvg = 0;
byte pulseLED = 2; // Blink with heartbeat

// Serial2 for External Arduino Connection
#define RX2_PIN 16
#define TX2_PIN 17

String serialBuffer = "";

// Function to scan for I2C devices
void scanI2CBus(TwoWire &wire, const char* busName) {
  byte error, address;
  int deviceCount = 0;
  
  Serial.print("Scanning I2C bus (");
  Serial.print(busName);
  Serial.println(")...");

  for(address = 1; address < 127; address++) {
    wire.beginTransmission(address);
    error = wire.endTransmission();
    
    if(error == 0) {
      Serial.print("Device found at address 0x");
      if(address < 16) Serial.print("0");
      Serial.print(address, HEX);
      
      if(address == SCREEN_ADDRESS) Serial.print(" (OLED Display)");
      if(address == MPU_ADDR) Serial.print(" (MPU6050)");
      
      Serial.println();
      deviceCount++;
      delay(10);
    }
  }
}

void setup() {
  Serial.begin(115200);
  
  // Initialize Serial2 for external Arduino communication
  Serial2.begin(9600, SERIAL_8N1, RX2_PIN, TX2_PIN);
  Serial.println("External Sensor Serial Initialized on RX:16 TX:17");

  delay(500); // Longer delay for stable initialization
  Serial.println("Starting initialization...");
  
  // Set device as a Wi-Fi Station
  WiFi.mode(WIFI_STA);
  
  // Print MAC address once
  Serial.print("This device's MAC Address: ");
  Serial.println(WiFi.macAddress());
  
  // Initialize ESP-NOW
  if (esp_now_init() != ESP_OK) {
    Serial.println("Error initializing ESP-NOW");
    return;
  }
  
  // Register send callback
  esp_now_register_send_cb(OnDataSent);
  
  // Register peer
  memcpy(peerInfo.peer_addr, receiverMacAddress, 6);
  peerInfo.channel = 0;  
  peerInfo.encrypt = false;
  
  // Add peer        
  if (esp_now_add_peer(&peerInfo) != ESP_OK) {
    Serial.println("Failed to add peer");
    return;
  }
  
  // Initialize I2C buses
  I2C_OLED.begin(OLED_SDA, OLED_SCL);
  I2C_MPU.begin(MPU_SDA, MPU_SCL);
  
  // Set I2C clock speeds
  I2C_OLED.setClock(100000);
  I2C_MPU.setClock(100000);
  
  // Initialize buzzer pin
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW); // Ensure buzzer is off at startup

  // Pulse LED
  pinMode(pulseLED, OUTPUT);
  
  // Initialize OLED
  if(!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println(F("SSD1306 allocation failed"));
    for(;;);
  }

  // Clear the buffer
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0,0);
  display.println("Initializing...");
  display.display();
  delay(1000);

  // Initialize Button PIN - CAREFUL: GPIO 13 used for button in previous code
  pinMode(13, INPUT_PULLUP);

  // Initialize MPU6050
  I2C_MPU.beginTransmission(MPU_ADDR);
  I2C_MPU.write(0x6B);
  I2C_MPU.write(0);
  I2C_MPU.endTransmission(true);
  
  Serial.println("Fall detection enabled. Threshold: " + String(FALL_THRESHOLD));
  
  // Fill sensor data with defaults
  sensorData.heartRate = 0;
  sensorData.heartRateAvg = 0;
  sensorData.spo2 = 98; // Default mockup
  sensorData.spo2Avg = 98;
  sensorData.temperature = 36.5; // Default mockup
  sensorData.fallDetected = false;
  sensorData.validReadings = false;
}

void readExternalSensor() {
  while (Serial2.available()) {
    char c = Serial2.read();
    if (c == '\n') {
      serialBuffer.trim();
      if (serialBuffer.startsWith("HR:")) {
        String hrStr = serialBuffer.substring(3);
        int newBPM = hrStr.toInt();
        
        if (newBPM > 30 && newBPM < 220) {
          beatsPerMinute = newBPM;
          beatAvg = newBPM; // Simple avg for now
          
          sensorData.heartRate = beatsPerMinute;
          sensorData.heartRateAvg = beatAvg;
          sensorData.validReadings = true;
          
          // Blink LED
          digitalWrite(pulseLED, HIGH);
          delay(50);
          digitalWrite(pulseLED, LOW);
          
          Serial.print("External HR Received: ");
          Serial.println(beatsPerMinute);
        }
      }
      serialBuffer = "";
    } else {
      serialBuffer += c;
    }
  }
}

void updateMPU6050() {
  I2C_MPU.beginTransmission(MPU_ADDR);
  I2C_MPU.write(0x3B);
  I2C_MPU.endTransmission(false);
  I2C_MPU.requestFrom(MPU_ADDR, 14, true);
  
  accelerometer_x = I2C_MPU.read()<<8 | I2C_MPU.read();
  accelerometer_y = I2C_MPU.read()<<8 | I2C_MPU.read();
  accelerometer_z = I2C_MPU.read()<<8 | I2C_MPU.read();
  
  // Calculate magnitude for fall detection
  float Ax = accelerometer_x;
  float Ay = accelerometer_y;
  float Az = accelerometer_z;
  
  // Simple magnitude calculation
  float magnitude = sqrt(pow(Ax, 2) + pow(Ay, 2) + pow(Az, 2));
  
  // If magnitude drops very low (free fall) or spikes very high (impact)
  // Normal gravity is ~16384 on default scale
  
  if (magnitude > 35000) { // Impact threshold
      if (!fallDetected) {
          fallDetected = true;
          fallDetectedTime = millis();
          fallStartTime = millis();
          Serial.println("Fall Detected (Impact)");
      }
  }
}

void displayStatusPage() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  
  display.setCursor(0, 0);
  display.println("Health Monitor");
  display.drawLine(0, 9, 128, 9, SSD1306_WHITE);
  
  display.setCursor(0, 15);
  display.print("HR: ");
  display.setTextSize(2);
  display.print((int)beatsPerMinute);
  display.setTextSize(1);
  display.println(" BPM");
  
  display.setCursor(0, 35);
  display.print("Status: ");
  display.println(sensorData.validReadings ? "Connected" : "Waiting...");
  
  if (fallDetected) {
     display.setCursor(0, 50);
     display.println("!! FALL DETECTED !!");
  }
  
  display.display();
}

void loop() {
  // 1. Read External Sensor Data
  readExternalSensor();

  // 2. Read MPU6050 and Check Fall (Simplified for stability)
  updateMPU6050();

  // 3. Update Display
  static unsigned long lastDisplayUpdate = 0;
  if (millis() - lastDisplayUpdate > 100) {
    displayStatusPage();
    lastDisplayUpdate = millis();
  }

  // 4. Send Data to AR Glasses via ESP-NOW
  static unsigned long lastSendTime = 0;
  if (millis() - lastSendTime > 200) { // Send every 200ms
    sensorData.fallDetected = fallDetected;
    esp_now_send(receiverMacAddress, (uint8_t *) &sensorData, sizeof(sensorData));
    lastSendTime = millis();
  }
  
  // Handle Buzzer for Fall
  if (fallDetected && millis() - fallDetectedTime < BUZZER_ALERT_TIME) {
    digitalWrite(BUZZER_PIN, HIGH);
  } else {
    digitalWrite(BUZZER_PIN, LOW);
    if (millis() - fallDetectedTime > BUZZER_ALERT_TIME) {
         fallDetected = false; // Auto reset after alert time
    }
  }
}
