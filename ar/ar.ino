#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <SPI.h>
#include <EEPROM.h>  // Optional, required only if you plan to save data
#include <TinyGPS++.h>  // For GPS module
#include <esp_now.h>
#include <WiFi.h>

// OLED display settings - ESP32 pin assignments
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_CS   5
#define OLED_DC   4
#define OLED_RST  16

// Watchdog timeout (in seconds)
#define WDT_TIMEOUT_SECONDS 5

// LDR sensor pin
#define LDR_PIN 32  // Changed to GPIO32 for LDR sensor

// Ultrasonic sensor pins
#define BUZZER_PIN 25  // Buzzer on GPIO25
#define TRIG_PIN 26    // Ultrasonic trigger on GPIO26
#define ECHO_PIN 27    // Ultrasonic echo on GPIO27

// Ultrasonic variables
long timeInMicro;
int distanceInCm;
unsigned long lastBeepTime = 0;
bool ultrasonicEnabled = true;  // Enable/disable obstacle detection

// Auto brightness settings
#define MIN_BRIGHTNESS 0    // Minimum display brightness (0-255)
#define MAX_BRIGHTNESS 255  // Maximum display brightness (0-255)
#define BRIGHTNESS_UPDATE_INTERVAL 1000  // Update brightness every 1 second
unsigned long lastBrightnessUpdate = 0;

// Brightness smoothing
#define BRIGHTNESS_SAMPLES 5
int brightnessReadings[BRIGHTNESS_SAMPLES];
int brightnessIndex = 0;
int currentBrightness = MAX_BRIGHTNESS;

// Logo dimensions for boot animation
#define LOGO_WIDTH  32
#define LOGO_HEIGHT 32

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &SPI, OLED_DC, OLED_RST, OLED_CS);

// Button and sensor pins - ESP32 pin assignments
#define BUTTON_PIN 17

// GPS pins - ESP32 pin assignments
#define GPS_RX 12  // Connect to TX of GPS module
#define GPS_TX 13  // Connect to RX of GPS module
#define GPS_BAUD 9600  // Most NEO-6M modules use 9600 baud

// Button debounce variables
unsigned long lastDebounceTime = 0;
unsigned long debounceDelay = 200;
bool buttonState = HIGH;
bool lastButtonState = HIGH;

int currentPage = 4;  // Start with clock page
unsigned long lastPageChange = 0;

// Medicine list
String medicines[4]; // Keep fixed size of 4
int totalMedicines = 4;

// Emergency Contact
String emergencyContact = "Rishik";
String emergencyNumber = "+91 1234567890";

// Daily Schedule
String schedule[4]; // Keep fixed size of 4
int totalSchedule = 4;

// Time tracking variables
unsigned long startMillis;
int hours = 10;
int minutes = 45;
int seconds = 0;
unsigned long lastMillis = 0;
bool timeSetByGPS = false;

// Date variables
int day = 2;
int month = 5;
int year = 2025;

// Timezone offset in hours (adjust for your location)
// For example: 
// IST (India): +5.5
// EST (Eastern US): -5
// PST (Pacific US): -8
// JST (Japan): +9
// CET (Central Europe): +1
const float timezoneOffset = +5.5; // Set this to your local timezone offset

// Remove heart rate sensor variables and keep only what's needed for display
float bpm = 0;  // Current heart rate from mix.ino
int beatAvg = 0;  // Average heart rate from mix.ino
bool hrDataReady = false;
int signalQuality = 0;  // 0-100 quality indicator

// Terminal UI command buffer
String commandBuffer = "";
bool commandMode = false;

// GPS variables
TinyGPSPlus gps;
HardwareSerial GPSSerial(1); // Use UART1 for GPS
float gpsLat = 0.0;
float gpsLng = 0.0;
float gpsAlt = 0.0;
int gpsSats = 0;
bool gpsValid = false;
unsigned long lastGpsUpdate = 0;

// Timing controls to prevent blocking
unsigned long lastSerialCheck = 0;
unsigned long lastGPSCheck = 0;
unsigned long lastDisplayUpdate = 0;
unsigned long lastHeartRateCheck = 0;
const unsigned long SERIAL_INTERVAL = 50;  // 50ms
const unsigned long GPS_INTERVAL = 1000;   // 1 second
const unsigned long DISPLAY_INTERVAL = 100; // 100ms
const unsigned long HEART_RATE_INTERVAL = 20; // 20ms

// Custom While(1)AR logo
static const unsigned char PROGMEM logo_bmp[] = {
    0b00000111, 0b11100000,
    0b00001111, 0b11110000,
    0b00011111, 0b11111000,
    0b00111100, 0b00111100,
    0b01111000, 0b00011110,
    0b01110000, 0b00001110,
    0b11110001, 0b10001111,
    0b11110011, 0b11001111,
    0b11110011, 0b11001111,
    0b11110001, 0b10001111,
    0b01110000, 0b00001110,
    0b01111000, 0b00011110,
    0b00111100, 0b00111100,
    0b00011111, 0b11111000,
    0b00001111, 0b11110000,
    0b00000111, 0b11100000
};

// Add month days array at the top with other global variables
const uint8_t daysInMonth[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};

// Add helper functions for calendar
bool isLeapYear(int year) {
    return (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);
}

int getStartDay(int year, int month) {
    // Zeller's congruence algorithm
    if (month < 3) {
        month += 12;
        year--;
    }
    int k = year % 100;
    int j = year / 100;
    int h = (1 + ((13 * (month + 1)) / 5) + k + (k / 4) + (j / 4) - (2 * j)) % 7;
    return (h + 5) % 7; // Convert to Sunday = 0 format
}

void drawCalendar() {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);

    // Get current month and year from GPS if available
    int displayMonth = timeSetByGPS ? month : 5; // Default to May if no GPS
    int displayYear = timeSetByGPS ? year : 2025; // Default to 2025 if no GPS

    // Display the month and year
    display.setCursor(30, 0);
    const char* monthNames[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", 
                               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    display.print(monthNames[displayMonth - 1]);
    display.print(" ");
    display.print(displayYear);

    // Days of the week
    const char* days[] = {"S", "M", "T", "W", "T", "F", "S"};
    for (int i = 0; i < 7; i++) {
        display.setCursor(10 + i * 15, 10);
        display.print(days[i]);
    }

    // Get the start day and total days
    int startDay = getStartDay(displayYear, displayMonth);
    int totalDays = daysInMonth[displayMonth - 1];
    if (displayMonth == 2 && isLeapYear(displayYear)) totalDays = 29;

    // Draw calendar dates
    int currentDate = timeSetByGPS ? day : 18; // Changed from 9 to 18 for May 18th
    int calendarDay = 1;
    
    for (int row = 0; row < 6; row++) {
        for (int col = 0; col < 7; col++) {
            if (row == 0 && col < startDay) {
                // Skip empty spaces before first day
                continue;
            } else if (calendarDay <= totalDays) {
                int x = 12 + col * 15;
                int y = 20 + row * 10;
                
                // For current date, draw inverted number
                if (calendarDay == currentDate && timeSetByGPS) {
                    // Draw filled rectangle as background
                    display.fillRect(x-1, y-1, 9, 9, SSD1306_WHITE);
                    // Draw number in black (inverted)
                    display.setTextColor(SSD1306_BLACK);
                    display.setCursor(x, y);
                    display.print(calendarDay);
                    // Reset text color to white for other numbers
                    display.setTextColor(SSD1306_WHITE);
                } else {
                    // Draw normal number
                    display.setCursor(x, y);
                    display.print(calendarDay);
                }
                
                calendarDay++;
            }
        }
    }

    display.display();
}

// Data structure for sensor readings (must match sender)
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
sensor_readings receivedData;

// Flag for new data received
bool newDataReceived = false;

// Callback function that will be executed when data is received
void OnDataRecv(const esp_now_recv_info_t* esp_now_info, const uint8_t *incomingData, int len) {
    memcpy(&receivedData, incomingData, sizeof(sensor_readings));
    newDataReceived = true;
    
    // Print detailed sensor data
    Serial.println("\n--- Received Sensor Data ---");
    Serial.print("Heart Rate: "); 
    Serial.print(receivedData.heartRate);
    Serial.print(" BPM (Avg: ");
    Serial.print(receivedData.heartRateAvg);
    Serial.println(" BPM)");
    
    Serial.print("SPO2: ");
    Serial.print(receivedData.spo2);
    Serial.print("% (Avg: ");
    Serial.print(receivedData.spo2Avg);
    Serial.println("%)");
    
    Serial.print("Temperature: ");
    Serial.print(receivedData.temperature);
    Serial.println("°C");
    
    Serial.print("Fall Detected: ");
    Serial.println(receivedData.fallDetected ? "YES!" : "No");
    
    Serial.print("Readings Valid: ");
    Serial.println(receivedData.validReadings ? "Yes" : "No");
    Serial.println("-------------------------");
}

// Add after other global variables, around line 128
// Detected medicine information
String detectedMedicine = "";
String medicineStatus = "";
bool medicineDetected = false;
unsigned long lastMedicineDisplay = 0;

// LDR wear detection
#define LDR_THRESHOLD 1500    // Threshold for detecting if device is worn
bool deviceWorn = false;     // Flag to track if device is being worn

void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("Synapse AR initializing...");
    
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
    
    // Register callback function for received data
    esp_now_register_recv_cb(OnDataRecv);
    
    // Add delay before GPS initialization to prevent boot loop
    delay(2000);
    
    // Initialize GPS with hardware flow control disabled
    GPSSerial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX, GPS_TX);
    Serial.println("GPS module initialized");
    
    // Add startup delay for GPS module to stabilize
    delay(500);
    
    // Initialize other peripherals and pins
    pinMode(BUTTON_PIN, INPUT_PULLUP);
    pinMode(LDR_PIN, INPUT);
    
    // Setup ultrasonic pins
    pinMode(BUZZER_PIN, OUTPUT);
    pinMode(TRIG_PIN, OUTPUT);
    pinMode(ECHO_PIN, INPUT);
    Serial.println("Ultrasonic sensor initialized");
    
    // Initialize medicines and schedules
    medicines[0] = "DICLOWIN 650 9 PM";
    medicines[1] = "IMEGLYN 1000 8 AM";
    medicines[2] = "Crocin 2 PM";
    medicines[3] = "Dolo 6 PM";
    
    schedule[0] = "7 AM - Breakfast";
    schedule[1] = "1.10 PM - Lunch";
    schedule[2] = "8 PM - Dinner";
    schedule[3] = "9 PM - Medicine";
    
    // Initialize brightness readings
    for(int i = 0; i < BRIGHTNESS_SAMPLES; i++) {
        brightnessReadings[i] = analogRead(LDR_PIN);
        delay(10);
    }
    
    if(!display.begin(SSD1306_SWITCHCAPVCC)) {
        Serial.println("SSD1306 allocation failed");
        while(1);
    }
    
    display.setRotation(2);
    display.clearDisplay();
    
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0,0);
    display.println("Connections OK!");
    display.display();
    
    Serial.println("OLED initialized");
    startMillis = millis();
    showBootAnimation();
    lastPageChange = millis();
    renderPage();
    
    // Test buzzer at startup
    pinMode(BUZZER_PIN, OUTPUT);
    testBuzzer();
}

void loop() {
    // Check for terminal commands
    checkSerial();
    
    // Update GPS data
    updateGPS();
    
    // Update ultrasonic sensor for obstacle detection
    if (ultrasonicEnabled) {
        handleUltrasonic();
    }
    
    // Update brightness
    updateDisplayBrightness();
    
    // Handle button input and display updates
    int reading = digitalRead(BUTTON_PIN);
    if (reading != lastButtonState) {
        lastDebounceTime = millis();
    }
    
    if ((millis() - lastDebounceTime) > debounceDelay) {
        if (reading != buttonState) {
            buttonState = reading;
            if (buttonState == LOW) {
                currentPage = (currentPage + 1) % 6;  // Changed from 5 to 6 to include calendar
                renderPage();
                lastPageChange = millis();
            }
        }
    }
    lastButtonState = reading;
    
    // Update pages that need continuous refresh
    if (currentPage == 4) {
        drawTime();
    } else if (currentPage == 3) {
        if (millis() - lastPageChange > 250) {  // Refresh 4 times per second
            drawHeartRate();
            lastPageChange = millis();
        }
    }
    
    // Small delay to prevent tight loops
    delay(10);
}

void handleButton() {
    // Read button state
    int reading = digitalRead(BUTTON_PIN);
    
    // Debounce handling
    if (reading != lastButtonState) {
        lastDebounceTime = millis();
    }
    
    if ((millis() - lastDebounceTime) > debounceDelay) {
        if (reading != buttonState) {
            buttonState = reading;
            if (buttonState == LOW) {
                // Change page
                currentPage = (currentPage + 1) % 6;  // Changed from 5 to 6 to include calendar
                Serial.print("Page changed to: ");
                Serial.println(currentPage);
                renderPage();  // Immediately render the new page
                lastPageChange = millis();
            }
        }
    }
    
    lastButtonState = reading;
}

void checkSerial() {
    // Process any incoming serial data
    while (Serial.available() > 0) {
        char inChar = (char)Serial.read();
        
        // Check for newline or carriage return to process command
        if (inChar == '\n' || inChar == '\r') {
            if (commandBuffer.length() > 0) {
                processCommand(commandBuffer);
                commandBuffer = "";
            }
        } else {
            // Add character to buffer
            commandBuffer += inChar;
        }
    }
}

void processCommand(String command) {
    command.trim();
    
    // Send acknowledgment that we received the command
    Serial.print("CMD_RECEIVED:");
    Serial.println(command);
    
    // Handle button press command - both "B" and "button_press" should work
    if (command == "B" || command == "button_press") {
        // Force a page change directly
        currentPage = (currentPage + 1) % 6;
        Serial.print("Page changed to: ");
        Serial.println(currentPage);
        renderPage();  // Immediately render the new page
        lastPageChange = millis();
        
        // Send success response
        Serial.println("Page changed successfully");
        Serial.println("CMD_END");
        return;
    }
    
    // Handle medicine detection command
    if (command.startsWith("detected_med ")) {
        String medData = command.substring(13);
        int statusPos = medData.indexOf("|");
        
        if (statusPos > 0) {
            detectedMedicine = medData.substring(0, statusPos);
            medicineStatus = medData.substring(statusPos + 1);
            medicineDetected = true;
            lastMedicineDisplay = millis();
            
            // If currently on the medicine page, refresh it
            if (currentPage == 0) {
                drawMedicineList();
            }
            
            Serial.print("Medicine detected: ");
            Serial.println(detectedMedicine);
            Serial.print("Status: ");
            Serial.println(medicineStatus);
        }
        
        Serial.println("CMD_END");
        return;
    }
    
    // Handle direct page command
    if (command.startsWith("page ")) {
        int newPage = command.substring(5).toInt();
        if (newPage >= 0 && newPage < 6) {  // Validate page number
            currentPage = newPage;
            renderPage();  // Immediately render the new page
            lastPageChange = millis();
            Serial.print("Switched to page: ");
            Serial.println(currentPage);
        }
        Serial.println("CMD_END");
        return;
    }
    
    // Handle menu selection
    if (command == "1") {
        listMedicines();
    } else if (command == "2") {
        listSchedule();
    } else if (command == "3") {
        Serial.println("\n--- Update Medicine ---");
        Serial.println("Format: med [index] [name]");
        Serial.println("Example: med 1 Aspirin");
    } else if (command == "4") {
        Serial.println("\n--- Update Schedule ---");
        Serial.println("Format: sch [index] [details]");
        Serial.println("Example: sch 2 2 PM - Lunch");
    } else if (command == "5") {
        Serial.println("\n--- Update Emergency Contact ---");
        Serial.println("Format: emergency [name] [number]");
        Serial.println("Example: emergency Dr Smith +1234567890");
    } else if (command == "6") {
        Serial.println("\n--- Show GPS Data ---");
        if (gpsValid) {
            Serial.print("Latitude: ");
            Serial.println(gpsLat, 6);
            Serial.print("Longitude: ");
            Serial.println(gpsLng, 6);
            Serial.print("Altitude: ");
            Serial.print(gpsAlt, 1);
            Serial.println(" meters");
            Serial.print("Satellites: ");
            Serial.println(gpsSats);
        } else {
            Serial.println("GPS signal not acquired yet");
        }
    } else if (command == "7" || command == "hr") {
        Serial.println("\n--- Heart Rate Data ---");
        if (newDataReceived && receivedData.validReadings) {
            Serial.print("Current BPM: ");
            Serial.println(receivedData.heartRate, 0);
            Serial.print("Average BPM: ");
            Serial.println(receivedData.heartRateAvg);
            Serial.print("Valid Readings: Yes");
            
            // Include the HR_DATA format for web interface compatibility
            Serial.print("HR_DATA:");
            Serial.print(receivedData.heartRate, 0);
            Serial.print(",");
            Serial.println("100");  // Quality indicator
        } else {
            Serial.println("Waiting for heart rate data from mix device");
            Serial.print("HR_DATA:0,0");  // Indicate no data
        }
    } else if (command == "8" || command == "ultrasonic") {
        Serial.println("\n--- Ultrasonic Sensor Data ---");
        Serial.print("Current Distance: ");
        Serial.print(distanceInCm);
        Serial.println(" cm");
        Serial.print("Obstacle Detection: ");
        Serial.println(ultrasonicEnabled ? "Enabled" : "Disabled");
    } else if (command == "ultrasonic on") {
        ultrasonicEnabled = true;
        Serial.println("Obstacle detection enabled");
    } else if (command == "ultrasonic off") {
        ultrasonicEnabled = false;
        noTone(BUZZER_PIN); // Stop any current beeping
        Serial.println("Obstacle detection disabled");
    } else if (command == "menu" || command == "0") {
        printTerminalMenu();
    } else if (command.startsWith("med ")) {
        updateMedicine(command.substring(4));
    } else if (command.startsWith("sch ")) {
        updateSchedule(command.substring(4));
    } else if (command.startsWith("emergency ")) {
        updateEmergencyContact(command.substring(10));
    } else if (command == "help") {
        printTerminalMenu();
    } else if (command == "status") {
        // Add a status command for easier connection testing
        Serial.println("\n--- Device Status ---");
        Serial.println("Device: Synapse AR");
        Serial.print("Current Page: ");
        Serial.println(currentPage + 1);
        Serial.print("Heart Rate: ");
        if (hrDataReady) {
            Serial.print(bpm, 0);
            Serial.println(" BPM");
        } else {
            Serial.println("Not measured");
        }
        Serial.print("GPS: ");
        Serial.println(gpsValid ? "Connected" : "Not connected");
        Serial.print("Ultrasonic: ");
        Serial.print(ultrasonicEnabled ? "Enabled" : "Disabled");
        Serial.print(" (");
        Serial.print(distanceInCm);
        Serial.println(" cm)");
        Serial.println("====================================");
    } else if (command == "ping") {
        // Simple ping command for connection testing
        Serial.println("pong");
    } else if (command != "") {
        Serial.println("Unknown command. Type 'menu' or 'help' for options.");
    }
    
    // Always end with CMD_END
    Serial.println("CMD_END");
}

void printTerminalMenu() {
    Serial.println("\n====================================");
    Serial.println("   SYNAPSE AR TERMINAL INTERFACE");
    Serial.println("====================================");
    Serial.println("1 - List Medicines");
    Serial.println("2 - List Schedule");
    Serial.println("3 - Update Medicine");
    Serial.println("4 - Update Schedule");
    Serial.println("5 - Update Emergency Contact");
    Serial.println("6 - Show GPS Data");
    Serial.println("7 - Show Heart Rate Data");
    Serial.println("8 - Show Ultrasonic Data");
    Serial.println("ultrasonic on/off - Toggle obstacle detection");
    Serial.println("0 - Show this menu");
    Serial.println("====================================");
    Serial.println("Current Status: Display showing page " + String(currentPage+1) + "/6");
    Serial.println("Type a command:");
}

void listMedicines() {
    Serial.println("\n--- Current Medicines ---");
    for (int i = 0; i < totalMedicines; i++) {
        Serial.println(String(i+1) + ". " + medicines[i]);
    }
    Serial.println("-------------------------");
}

void listSchedule() {
    Serial.println("\n--- Current Schedule ---");
    for (int i = 0; i < totalSchedule; i++) {
        Serial.println(String(i+1) + ". " + schedule[i]);
    }
    Serial.println("-------------------------");
}

void updateMedicine(String params) {
    int spacePos = params.indexOf(' ');
    if (spacePos > 0) {
        int index = params.substring(0, spacePos).toInt() - 1;
        String newMedicine = params.substring(spacePos + 1);
        
        if (index >= 0 && index < 4) {
            medicines[index] = newMedicine;
            Serial.println("Medicine " + String(index+1) + " updated to: " + newMedicine);
            renderPage();
        } else {
            Serial.println("Invalid index. Use 1-4.");
        }
    } else {
        Serial.println("Invalid format. Use: med [index] [name]");
    }
}

void updateSchedule(String params) {
    int spacePos = params.indexOf(' ');
    if (spacePos > 0) {
        int index = params.substring(0, spacePos).toInt() - 1;
        String newSchedule = params.substring(spacePos + 1);
        
        if (index >= 0 && index < 4) {
            schedule[index] = newSchedule;
            Serial.println("Schedule " + String(index+1) + " updated to: " + newSchedule);
            renderPage();
        } else {
            Serial.println("Invalid index. Use 1-4.");
        }
    } else {
        Serial.println("Invalid format. Use: sch [index] [details]");
    }
}

void updateEmergencyContact(String params) {
    // Find the start of the phone number (either a + or a digit)
    int numberPos = -1;
    for (int i = 0; i < params.length(); i++) {
        if (params.charAt(i) == '+' || isdigit(params.charAt(i))) {
            numberPos = i;
            break;
        }
    }
    
    if (numberPos > 0) {
        // Extract name and trim any trailing spaces
        String name = params.substring(0, numberPos);
        name.trim();
        
        // Extract number
        String number = params.substring(numberPos);
        number.trim();
        
        emergencyContact = name;
        emergencyNumber = number;
        Serial.println("Emergency contact updated:");
        Serial.println("Name: " + emergencyContact);
        Serial.println("Number: " + emergencyNumber);
        renderPage();
    } else {
        Serial.println("Invalid format. Use: emergency [name] [number]");
        Serial.println("Number must start with + or a digit");
    }
}

void showBootAnimation() {
    display.clearDisplay();
    
    // Phase 1: Enhanced scanning effect
    for(int16_t i = 0; i < display.height(); i += 2) {
        display.clearDisplay();
        // Draw multiple scan lines for a more dynamic effect
        display.drawFastHLine(0, i, display.width(), SSD1306_WHITE);
        if (i > 5) display.drawFastHLine(0, i-5, display.width()/2, SSD1306_WHITE);
        if (i > 10) display.drawFastHLine(display.width()/2, i-10, display.width()/2, SSD1306_WHITE);
        display.display();
        delay(5);
    }
    
    // Phase 2: Improved matrix-style raining code effect
    const int numDrops = 15; // More drops
    int dropX[numDrops];
    int dropY[numDrops];
    int dropSpeed[numDrops];
    int dropLength[numDrops];
    int dropBrightness[numDrops]; // Variable brightness
    
    // Initialize drops
    for(int i = 0; i < numDrops; i++) {
        dropX[i] = random(0, display.width());
        dropY[i] = random(-20, -1);
        dropSpeed[i] = random(1, 4); // More variable speed
        dropLength[i] = random(5, 20); // Longer drops
        dropBrightness[i] = random(0, 2); // 0 = dim, 1 = bright
    }
    
    // Animate drops - 30 frames
    for(int frame = 0; frame < 30; frame++) {
        display.clearDisplay();
        
        for(int i = 0; i < numDrops; i++) {
            // Draw the drop with variable brightness
            for(int j = 0; j < dropLength[i]; j++) {
                int y = dropY[i] - j;
                if(y >= 0 && y < display.height()) {
                    if (j == 0 || dropBrightness[i] == 1) {
                        display.drawPixel(dropX[i], y, SSD1306_WHITE); // Bright head
                    } else if (j % 3 != 0) { // Skip some pixels for a dashed effect
                        display.drawPixel(dropX[i], y, SSD1306_WHITE);
                    }
                }
            }
            
            // Move the drop
            dropY[i] += dropSpeed[i];
            
            // Reset if off screen
            if(dropY[i] > display.height() + dropLength[i]) {
                dropX[i] = random(0, display.width());
                dropY[i] = random(-20, -1);
                dropBrightness[i] = random(0, 2);
            }
        }
        
        display.display();
        delay(10);
    }
    
    // Phase 3: Enhanced synapse network forming - EXTENDED TIME
    display.clearDisplay();
    
    const int numNodes = 12; // More nodes
    int nodeX[numNodes];
    int nodeY[numNodes];
    int nodeSize[numNodes]; // Variable node sizes
    bool connected[numNodes][numNodes] = {false};
    
    // Place nodes randomly with different sizes
    for(int i = 0; i < numNodes; i++) {
        nodeX[i] = random(10, display.width()-10);
        nodeY[i] = random(10, display.height()-10);
        nodeSize[i] = random(1, 3); // Different node sizes
    }
    
    // First, animated appearance of nodes
    for(int i = 0; i < numNodes; i++) {
        // Fade in effect
        for(int r = 0; r <= nodeSize[i]; r++) {
            display.drawCircle(nodeX[i], nodeY[i], r, SSD1306_WHITE);
            display.display();
            delay(10);
        }
        // Fill the node
        display.fillCircle(nodeX[i], nodeY[i], nodeSize[i], SSD1306_WHITE);
        display.display();
    }
    
    // Then, enhanced connections forming - MORE CONNECTIONS AND TIME
    // Increased to 25 connections from 8
    for(int conn = 0; conn < 25; conn++) {
        int from = random(0, numNodes);
        int to = random(0, numNodes);
        
        // Don't connect to self or already connected
        if(from != to && !connected[from][to]) {
            // Animate the connection forming with a pulsing effect
            for(float t = 0.0; t <= 1.0; t += 0.1) { // Faster animation (0.1 step instead of 0.05)
                int x = nodeX[from] + t * (nodeX[to] - nodeX[from]);
                int y = nodeY[from] + t * (nodeY[to] - nodeY[from]);
                
                display.clearDisplay();
                
                // Redraw all nodes and existing connections
                for(int n = 0; n < numNodes; n++) {
                    display.fillCircle(nodeX[n], nodeY[n], nodeSize[n], SSD1306_WHITE);
                }
                
                // Draw existing connections
                for(int f = 0; f < numNodes; f++) {
                    for(int t2 = 0; t2 < numNodes; t2++) {
                        if(connected[f][t2]) {
                            display.drawLine(nodeX[f], nodeY[f], nodeX[t2], nodeY[t2], SSD1306_WHITE);
                        }
                    }
                }
                
                // Draw the forming connection
                display.drawLine(nodeX[from], nodeY[from], x, y, SSD1306_WHITE);
                
        display.display();
        delay(2); // Reduced delay from 5ms to 2ms
    }
    
            connected[from][to] = true;
            connected[to][from] = true;
            
            // Pulse effect when connection completes
            for(int pulse = 0; pulse < 3; pulse++) {
                display.clearDisplay();
                
                // Draw all nodes and connections
                for(int n = 0; n < numNodes; n++) {
                    display.fillCircle(nodeX[n], nodeY[n], nodeSize[n], SSD1306_WHITE);
                }
                
                for(int f = 0; f < numNodes; f++) {
                    for(int t2 = 0; t2 < numNodes; t2++) {
                        if(connected[f][t2]) {
                            if(f == from && t2 == to && pulse % 2 == 0) {
                                // Pulse the newest connection by drawing it thicker
                                display.drawLine(nodeX[f], nodeY[f], nodeX[t2], nodeY[t2], SSD1306_WHITE);
                                display.drawLine(nodeX[f]+1, nodeY[f], nodeX[t2]+1, nodeY[t2], SSD1306_WHITE);
                            } else {
                                display.drawLine(nodeX[f], nodeY[f], nodeX[t2], nodeY[t2], SSD1306_WHITE);
                            }
                        }
                    }
                }
                
                display.display();
                delay(50);
            }
        }
    }
    
    // Final network sparkle effect
    for(int i = 0; i < 5; i++) {
        // Random node to highlight
        int highlightNode = random(0, numNodes);
        
        // Draw with highlighting
        display.clearDisplay();
        for(int n = 0; n < numNodes; n++) {
            if(n == highlightNode) {
                // Draw the highlighted node with a glow
                for(int r = nodeSize[n] + 2; r >= nodeSize[n]; r--) {
                    display.drawCircle(nodeX[n], nodeY[n], r, SSD1306_WHITE);
                }
                display.fillCircle(nodeX[n], nodeY[n], nodeSize[n], SSD1306_WHITE);
            } else {
                display.fillCircle(nodeX[n], nodeY[n], nodeSize[n], SSD1306_WHITE);
            }
        }
        
        // Draw all connections
        for(int f = 0; f < numNodes; f++) {
            for(int t = 0; t < numNodes; t++) {
                if(connected[f][t]) {
                    if(f == highlightNode || t == highlightNode) {
                        // Highlight connections to the highlighted node
                        display.drawLine(nodeX[f], nodeY[f], nodeX[t], nodeY[t], SSD1306_WHITE);
                        display.drawLine(nodeX[f]+1, nodeY[f], nodeX[t]+1, nodeY[t], SSD1306_WHITE);
                    } else {
                        display.drawLine(nodeX[f], nodeY[f], nodeX[t], nodeY[t], SSD1306_WHITE);
                    }
                }
            }
        }
        
        display.display();
        delay(100);
    }
    
    // Transition effect - wipe in from both sides
    for(int16_t i = 0; i <= display.width()/2; i += 2) {
        display.clearDisplay();
        display.fillRect(0, 0, i, display.height(), SSD1306_WHITE);
        display.fillRect(display.width()-i, 0, i, display.height(), SSD1306_WHITE);
        display.display();
        delay(5);
    }
    
    delay(100);
    
    // Phase 4: Display "SYNAPSE AR" for 2 seconds with slick transition
    display.clearDisplay();
    
    // First, create an animated border that draws itself
    for(int i = 0; i < display.width(); i += 4) {
        // Draw top and bottom border segments
        display.drawFastHLine(0, 0, i, SSD1306_WHITE);
        display.drawFastHLine(0, display.height()-1, i, SSD1306_WHITE);
        display.display();
        delay(3);
    }
    
    for(int i = 0; i < display.height(); i += 3) {
        // Draw left and right border segments
        display.drawFastVLine(0, 0, i, SSD1306_WHITE);
        display.drawFastVLine(display.width()-1, 0, i, SSD1306_WHITE);
        display.display();
        delay(3);
    }
    
    // Complete the border
    display.drawRect(0, 0, display.width(), display.height(), SSD1306_WHITE);
    display.display();
    
    // Animate SYNAPSE text appearing letter by letter
    const char* synapseText = "SYNAPSE";
    display.setTextSize(2);
    display.setTextColor(SSD1306_WHITE);
    
    // Calculate position to center "SYNAPSE"
    int16_t x1, y1;
    uint16_t w, h;
    display.getTextBounds(synapseText, 0, 0, &x1, &y1, &w, &h);
    int16_t x = (display.width() - w) / 2;
    
    for(int i = 0; i < strlen(synapseText); i++) {
        display.setCursor(x + i * (w / strlen(synapseText)), 16);
        char c[2] = {synapseText[i], '\0'};
        display.print(c);
        display.display();
        delay(80);
    }
    
    // Calculate position to center "AR"
    const char* arText = "AR";
    display.getTextBounds(arText, 0, 0, &x1, &y1, &w, &h);
    x = (display.width() - w) / 2;
    
    // Animate AR text appearing letter by letter
    for(int i = 0; i < strlen(arText); i++) {
        display.setCursor(x + i * (w / strlen(arText)), 40);
        char c[2] = {arText[i], '\0'};
        display.print(c);
    display.display();
        delay(120);
    }
    
    // Draw animated underline
    int lineY = 42 + h;
    for(int i = 0; i <= w; i += 2) {
        display.drawLine(x, lineY, x + i, lineY, SSD1306_WHITE);
        display.display();
        delay(5);
    }
    
    // Keep the text displayed for 2 seconds
    delay(2000);
    
    // Fade out effect with improved visual style
    for(int16_t i=0; i<4; i++) {
        if(i % 2 == 0) {
            // Create a more dramatic effect with negative display
        display.invertDisplay(true);
        } else {
        display.invertDisplay(false);
        }
        delay(80);
    }
    
    // Final wipe
    for(int16_t i = 0; i < display.height(); i += 3) {
    display.clearDisplay();
        display.fillRect(0, i, display.width(), 3, SSD1306_WHITE);
        display.display();
        delay(5);
    }
    
    display.clearDisplay();
    display.display();
}

void drawMedicineList() {
    display.clearDisplay();
    display.setTextSize(1);  // Base text size of 1 (smallest standard size)
    display.setTextColor(SSD1306_WHITE);
    
    // Draw title with normal size
    display.setCursor(0, 0);
    display.println("Medicines:");
    
    // Draw detected medicine if available (less than 1 minute old)
    if (medicineDetected && (millis() - lastMedicineDisplay < 60000)) {
        // Draw a highlight box
        display.drawRect(0, 8, display.width(), 20, SSD1306_WHITE);
        
        // Center the detected medicine name
        int16_t x1, y1;
        uint16_t w, h;
        display.getTextBounds(detectedMedicine, 0, 0, &x1, &y1, &w, &h);
        int centerX = (display.width() - w) / 2;
        
        // Show the detected medicine
        display.setCursor(centerX, 12);
        display.print(detectedMedicine);
        
        // Show status on next line
        display.setCursor(4, 21);
        display.print(medicineStatus);
        
        // Draw a separator line
        display.drawLine(0, 30, display.width(), 30, SSD1306_WHITE);
        
        // Draw the medicine list with smaller spacing
        for (int i = 0; i < 3; i++) {  // Only show 3 medicines when detection is active
            display.setCursor(4, 33 + (i * 10));
            display.print("- ");
            display.println(medicines[i]);
        }
    } else {
        // Draw each medicine with proper spacing but slightly compressed
        for (int i = 0; i < 4; i++) {
            // Position medicines slightly closer together for smaller visual appearance
            display.setCursor(4, 10 + (i * 10));  // Reduced vertical spacing by 20%
            display.print("- ");
            display.println(medicines[i]);
            
            // Get medicine name and time
            String medInfo = medicines[i];
            int dashPos = medInfo.indexOf(" - ");
            
            if (dashPos > 0) {
                String medName = medInfo.substring(0, dashPos);
                String medTime = medInfo.substring(dashPos + 3);
                
                // Check if it's time to take this medicine
                bool shouldTake = checkMedicineTime(medTime);
                
                // Display recommendation below medicine name in smaller font (using even smaller positioning)
                display.setCursor(15, 10 + (i * 10) + 7);  // Reduced vertical spacing for recommendation
                if (shouldTake) {
                    display.print("Take now");
                } else {
                    display.print("Next dose: ");
                    display.print(medTime);
                }
            }
        }
    }
    
    display.display();
}

// Check if it's time to take a medicine based on scheduled time
bool checkMedicineTime(String scheduleTime) {
    // Get current hour and AM/PM
    int currentHour = hours;
    bool isPM = false;
    if (currentHour >= 12) {
        isPM = true;
        if (currentHour > 12) {
            currentHour -= 12;
        }
    }
    if (currentHour == 0) {
        currentHour = 12; // 12 AM
    }
    
    // Parse medicine schedule time
    int spacePos = scheduleTime.indexOf(" ");
    if (spacePos > 0) {
        int scheduleHour = scheduleTime.substring(0, spacePos).toInt();
        String amPm = scheduleTime.substring(spacePos + 1);
        
        // Convert to 24-hour format for comparison
        int schedule24Hour = scheduleHour;
        if (amPm.equals("PM") && scheduleHour < 12) {
            schedule24Hour += 12;
        } else if (amPm.equals("AM") && scheduleHour == 12) {
            schedule24Hour = 0;
        }
        
        int current24Hour = hours;
        
        // Calculate time difference (allow 2 hours early and 3 hours late)
        int timeDiff = current24Hour - schedule24Hour;
        if (timeDiff >= -2 && timeDiff <= 3) {
            return true;
        }
    }
    
    return false;
}

void drawEmergencyContact() {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);
    display.print("Emergency Contact:");
    display.setCursor(0, 16);
    display.print(emergencyContact);
    display.setCursor(0, 28);
    display.print(emergencyNumber);
    display.display();
}

void drawSchedule() {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    
    // Draw title
    display.setCursor(0, 0);
    display.println("Today's Schedule:");
    
    // Draw each schedule item with proper spacing
    for (int i = 0; i < 4; i++) {
        display.setCursor(4, 12 + (i * 12));  // Increased vertical spacing
        display.println(schedule[i]);
    }
    
    display.display();
}

void updateTime() {
    // If time is set by GPS, we don't need to update manually
    if (timeSetByGPS) {
        // Just update seconds to keep it smooth between GPS updates
        unsigned long currentMillis = millis();
        if (currentMillis - lastMillis >= 1000) {
            lastMillis = currentMillis;
            seconds++;
            if (seconds >= 60) {
                seconds = 0;
                minutes++;
                if (minutes >= 60) {
                    minutes = 0;
                    hours++;
                    if (hours >= 24) {
                        hours = 0;
                        // Update day - this is simplified and doesn't handle month boundaries
                        day++;
                    }
                }
            }
        }
    } else {
        // Traditional time update
    unsigned long currentMillis = millis();
    if (currentMillis - lastMillis >= 1000) {
        lastMillis = currentMillis;
        seconds++;
        if (seconds >= 60) {
            seconds = 0;
            minutes++;
            if (minutes >= 60) {
                minutes = 0;
                hours++;
                if (hours >= 24) {
                    hours = 0;
                    }
                }
            }
        }
    }
}

void drawTime() {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    
    // Only update time manually if not set by GPS
    if (!timeSetByGPS) {
        updateTime();
    }
    
    // Convert to 12-hour format
    int displayHours = hours;
    bool isPM = false;
    
    if (displayHours >= 12) {
        isPM = true;
        if (displayHours > 12) {
            displayHours -= 12;
        }
    }
    
    if (displayHours == 0) {
        displayHours = 12; // 12 AM instead of 0
    }
    
    // Draw time in 12-hour format with seconds
    display.setTextSize(2);
    display.setCursor(5, 20);
    
    // Hours (no leading zero in 12-hour format)
    display.print(displayHours);
    display.print(":");
    
    // Minutes with leading zero
    if (minutes < 10) display.print("0");
    display.print(minutes);
    
    // Seconds with leading zero
    display.print(":");
    if (seconds < 10) display.print("0");
    display.print(seconds);
    
    // AM/PM indicator
    display.setTextSize(1);
    display.print(" ");
    display.print(isPM ? "PM" : "AM");
    
    // Draw date with GPS data if available
    display.setTextSize(1);
    display.setCursor(10, 50);
    
    if (timeSetByGPS) {
        // Format date: DD Mon YYYY
        char dateStr[16];
        const char* monthNames[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
        sprintf(dateStr, "%d %s %d", day, monthNames[month-1], year);
        display.print(dateStr);
    } else {
        display.print("18 May 2025");  // Changed from "9 May 2025" to "18 May 2025"
    }
    
    display.display();
}

void drawHeartRate() {
  display.clearDisplay();
  
  // Title with heart symbol
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print("HEART RATE");
  
  // Add small heart icon
  display.fillCircle(90, 4, 3, SSD1306_WHITE);
  display.fillCircle(96, 4, 3, SSD1306_WHITE);
  display.fillTriangle(87, 6, 99, 6, 93, 12, SSD1306_WHITE);
  
  // If we have received data from mix.ino
  if (newDataReceived && receivedData.validReadings) {
    // Display average BPM in large font
    display.setTextSize(3);
    
    // Use received average heart rate data
    int displayBpm = receivedData.heartRateAvg;
    if (displayBpm < 60) displayBpm = 60;
    if (displayBpm > 160) displayBpm = 160;
    
    // Calculate width to center the reading
    String bpmText = String(displayBpm);
    int16_t x1, y1;
    uint16_t w, h;
    display.getTextBounds(bpmText, 0, 0, &x1, &y1, &w, &h);
    int centerX = (SCREEN_WIDTH - w) / 2;
    
    display.setCursor(centerX, 24);
    display.print(bpmText);
    
    // BPM label in smaller font
    display.setTextSize(1);
    display.setCursor(centerX + w + 2, 32);
    display.print("BPM");
    
    // Show status in small font at bottom
    display.setTextSize(1);
    display.setCursor(10, 50);
    display.print("Status: ");
    display.print(receivedData.validReadings ? "Valid" : "Invalid");
  } else {
    // Show waiting message
    display.setTextSize(2);
    display.setCursor(10, 24);
    display.print("---");
    
    display.setTextSize(1);
    display.setCursor(10, 50);
    display.print("Waiting for data...");
  }
  
  display.display();
}

void renderPage() {
    // Debug output
    Serial.print("Rendering page: ");
    Serial.println(currentPage);
    
    switch (currentPage) {
        case 0:
            drawMedicineList();
            break;
        case 1:
            drawEmergencyContact();
            break;
        case 2:
            drawSchedule();
            break;
        case 3:
            drawHeartRate();
            break;
        case 4:
            drawTime();
            break;
        case 5:
            drawCalendar();
            break;
    }
    
    // Ensure display is updated
    display.display();
    
    // Confirm render complete
    Serial.println("Page render complete");
}

// Update GPS data from NEO-6M module
void updateGPS() {
    // Add safety check to prevent buffer overflow
    int available = GPSSerial.available();
    if (available > 100) {
        // Too many characters in the buffer, clear it to prevent lockup
        while (GPSSerial.available() > 0) {
            GPSSerial.read();
        }
        return;
    }
    
    // Process GPS data while available, with timeout protection
    unsigned long startTime = millis();
    while (GPSSerial.available() > 0 && (millis() - startTime < 100)) {
        char c = GPSSerial.read();
        // Debug: Print the raw NMEA data (COMMENT THIS OUT if causing issues)
        // Serial.print(c); // <-- Comment this line if causing boot loops
        gps.encode(c);
    }
    
    // Update GPS values every second
    if (millis() - lastGpsUpdate > 1000) {
        lastGpsUpdate = millis();
        
        // Update time and date if available from GPS
        if (gps.time.isUpdated() && gps.date.isUpdated()) {
            // Get UTC time from GPS
            int utcHour = gps.time.hour();
            minutes = gps.time.minute();
            seconds = gps.time.second();
            
            // Get date from GPS
            day = gps.date.day();
            month = gps.date.month();
            year = gps.date.year();
            
            // Apply timezone offset to convert UTC to local time
            float localHour = utcHour + timezoneOffset;
            
            // Handle date change if needed
            if (localHour >= 24) {
                localHour -= 24;
                day++; // Increment day for next day
                
                // Simple month rollover (doesn't handle all edge cases)
                int daysInMonth[] = {0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
                // Adjust February for leap years
                if (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) {
                    daysInMonth[2] = 29;
                }
                
                if (day > daysInMonth[month]) {
                    day = 1;
                    month++;
                    if (month > 12) {
                        month = 1;
                        year++;
                    }
                }
            } else if (localHour < 0) {
                localHour += 24;
                day--; // Decrement day for previous day
                
                // Handle month/year boundary when going back
                if (day < 1) {
                    month--;
                    if (month < 1) {
                        month = 12;
                        year--;
                    }
                    
                    // Set day to last day of previous month
                    int daysInPrevMonth[] = {0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
                    // Adjust February for leap years
                    if (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) {
                        daysInPrevMonth[2] = 29;
                    }
                    day = daysInPrevMonth[month];
                }
            }
            
            // Convert floating point hours to integer
            hours = (int)localHour;
            
            // Handle fractional hour (for half-hour timezones like India)
            int fractionalMinutes = (localHour - hours) * 60;
            minutes += fractionalMinutes;
            if (minutes >= 60) {
                minutes -= 60;
                hours++;
                if (hours >= 24) {
                    hours -= 24;
                    // Day already handled above
                }
            }
            
            if (!timeSetByGPS) {
                timeSetByGPS = true;
                Serial.println("Time and date set from GPS!");
                Serial.print("UTC Time: ");
                Serial.print(utcHour); Serial.print(":");
                Serial.print(minutes); Serial.print(":");
                Serial.println(seconds);
                
                Serial.print("Local Time: ");
                Serial.print(hours); Serial.print(":");
                Serial.print(minutes); Serial.print(":");
                Serial.println(seconds);
                
                Serial.print("Date: ");
                Serial.print(day); Serial.print("/");
                Serial.print(month); Serial.print("/");
                Serial.println(year);
                
                Serial.print("Timezone offset: ");
                Serial.print(timezoneOffset);
                Serial.println(" hours");
            }
        }
        
        if (gps.location.isUpdated()) {
            gpsLat = gps.location.lat();
            gpsLng = gps.location.lng();
            gpsValid = true;
            // Debug: Print when location is updated
            Serial.println("GPS Location Updated!");
            Serial.print("Lat: "); Serial.println(gpsLat, 6);
            Serial.print("Lng: "); Serial.println(gpsLng, 6);
        }
        
        if (gps.altitude.isUpdated()) {
            gpsAlt = gps.altitude.meters();
        }
        
        if (gps.satellites.isUpdated()) {
            gpsSats = gps.satellites.value();
            // Debug: Print satellite count
            Serial.print("Satellites: "); Serial.println(gpsSats);
        }
    }
}

void updateDisplayBrightness() {
    static unsigned long lastBrightnessUpdate = 0;
    
    // Only update brightness periodically
    if (millis() - lastBrightnessUpdate < BRIGHTNESS_UPDATE_INTERVAL) {
        return;
    }
    lastBrightnessUpdate = millis();
    
    // Read LDR value
    int ldrValue = analogRead(LDR_PIN);
    
    // Update rolling average
    brightnessReadings[brightnessIndex] = ldrValue;
    brightnessIndex = (brightnessIndex + 1) % BRIGHTNESS_SAMPLES;
    
    // Calculate average
    long sum = 0;
    for(int i = 0; i < BRIGHTNESS_SAMPLES; i++) {
        sum += brightnessReadings[i];
    }
    int avgReading = sum / BRIGHTNESS_SAMPLES;
    
    // INVERTED LOGIC: Higher value means device is being worn (more light detected)
    // Lower value means it's not being worn (less light detected)
    deviceWorn = (avgReading >= LDR_THRESHOLD);
    
    // Binary brightness control - only very bright or very dim
    int targetBrightness;
    if (avgReading < 2000) {  // Dark environment
        targetBrightness = 255;  // Maximum brightness
    } else {  // Bright environment
        targetBrightness = 30;   // Minimum brightness but still readable
    }
    
    // Only change if we're switching levels
    if (targetBrightness != currentBrightness) {
        currentBrightness = targetBrightness;
        
        // Update display contrast
        display.ssd1306_command(SSD1306_SETCONTRAST);
        display.ssd1306_command(currentBrightness);
    }
}

// Update the handleUltrasonic function to always provide ultrasonic alerts but with different behaviors
void handleUltrasonic() {
    static unsigned long lastMeasurement = 0;
    
    // Add delay between measurements to prevent interference
    if (millis() - lastMeasurement < 100) {  // 100ms between measurements
        return;
    }
    lastMeasurement = millis();
    
    // Ensure ECHO pin is in a known state
    pinMode(ECHO_PIN, INPUT_PULLUP);
    delayMicroseconds(5);
    
    // Trigger ultrasonic pulse
    digitalWrite(TRIG_PIN, LOW);
    delayMicroseconds(5);
    digitalWrite(TRIG_PIN, HIGH);
    delayMicroseconds(15);
    digitalWrite(TRIG_PIN, LOW);

    // Get distance
    timeInMicro = pulseIn(ECHO_PIN, HIGH, 30000); // 30ms timeout
    
    // Calculate distance (with validation)
    if (timeInMicro == 0) {
        distanceInCm = 300; // Set to max range
    } else if (timeInMicro < 100) {  // Minimum valid timing
        distanceInCm = 300;
    } else {
        distanceInCm = timeInMicro * 0.034 / 2; // Speed of sound = 0.034 cm/μs
    }

    // Validate distance with more reasonable limits
    if (distanceInCm <= 2 || distanceInCm >= 300) {
        distanceInCm = 300; // Set to max range
        noTone(BUZZER_PIN);  // No beep if out of range
        return;
    }

    unsigned long currentTime = millis();
    
    // Different beep patterns based on whether the device is worn or not
    if (deviceWorn) {
        // WORN STATE: Less aggressive but still functional beep pattern
        
        // Calculate beep intervals - more gentle when worn
        int beepInterval;
        if (distanceInCm < 15) {  // Very close
            beepInterval = 200;  // Less frequent beeping when worn
        } else if (distanceInCm < 35) {
            beepInterval = 400;  // Medium interval
        } else if (distanceInCm < 65) {
            beepInterval = 800;  // Longer interval
        } else if (distanceInCm < 105) {
            beepInterval = 1200;  // Much longer interval
        } else {
            noTone(BUZZER_PIN);  // No beep when too far
            return;
        }
        
        // Generate beeps with gentler tones when worn
        if (currentTime - lastBeepTime >= beepInterval) {
            if (distanceInCm < 15) {
                // Gentler tone for close objects when worn
                tone(BUZZER_PIN, 2500, 80);  // Lower frequency, shorter duration
            } else {
                // Even gentler for farther objects
                tone(BUZZER_PIN, 2000, 60);  // Lowest frequency, shortest duration
            }
            lastBeepTime = currentTime;
        }
    } else {
        // NOT WORN STATE: Very aggressive beep pattern
        
        // Calculate beep intervals - very aggressive when not worn
        int beepInterval;
        if (distanceInCm < 15) {  // Very close
            beepInterval = 50;  // Very fast beeping
        } else if (distanceInCm < 35) {
            beepInterval = 150;  // Fast beeping
        } else if (distanceInCm < 65) {
            beepInterval = 300;  // Medium beeping
        } else if (distanceInCm < 105) {
            beepInterval = 500;  // Slower beeping
        } else {
            noTone(BUZZER_PIN);  // No beep when too far
            return;
        }
        
        // Generate beeps with aggressive tones when not worn
        if (currentTime - lastBeepTime >= beepInterval) {
            if (distanceInCm < 15) {
                // Very aggressive for close objects when not worn
                tone(BUZZER_PIN, 4000, 150);  // Highest frequency, longest duration
            } else if (distanceInCm < 35) {
                // Medium aggressive for medium-range objects
                tone(BUZZER_PIN, 3500, 120);  // Medium-high frequency
            } else {
                // Less aggressive for farther objects
                tone(BUZZER_PIN, 3000, 100);  // Still attention-getting
            }
            lastBeepTime = currentTime;
        }
    }
}

// Add this function to test the buzzer directly
void testBuzzer() {
    Serial.println("Testing buzzer...");
    // Test different frequencies
    tone(BUZZER_PIN, 2000, 500);  // 2kHz for 500ms
    delay(1000);
    tone(BUZZER_PIN, 3000, 500);  // 3kHz for 500ms
    delay(1000);
    tone(BUZZER_PIN, 1000, 500);  // 1kHz for 500ms
    Serial.println("Buzzer test complete");
}