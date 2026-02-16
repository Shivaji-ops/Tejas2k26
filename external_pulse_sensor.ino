
// External Pulse Sensor Code
// connect Pulse Sensor Signal Pin to Analog A0
// connect Pulse Sensor VCC to 5V
// connect Pulse Sensor GND to GND

const int PulseSensorPurplePin = 0;  // Pulse Sensor PURPLE WIRE connected to ANALOG PIN 0
int Signal;                          // Holds the incoming raw data. Signal value can range from 0-1024
int Threshold = 550;                 // Determine which Signal to "count as a beat", and which to ignore.

// Variables for BPM calculation
unsigned long lastBeatTime = 0;
int BPM = 0;
bool Pulse = false;

void setup() {
  Serial.begin(9600);  // Serial Communication to ESP32
  pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
  Signal = analogRead(PulseSensorPurplePin);  // Read the PulseSensor's value.

  // Simple Peak Detection
  if (Signal > Threshold && !Pulse) {
    Pulse = true;
    digitalWrite(LED_BUILTIN, HIGH);
    
    unsigned long currentTime = millis();
    unsigned long timeDifference = currentTime - lastBeatTime;
    
    // Calculate BPM if time difference is reasonable for a heart rate (30-220 BPM)
    // 60000ms / 220 = 272ms
    // 60000ms / 30 = 2000ms
    if (timeDifference > 272 && timeDifference < 2000) {
      BPM = 60000 / timeDifference;
      
      // Send data format: "HR:75"
      Serial.print("HR:");
      Serial.println(BPM);
    }
    
    lastBeatTime = currentTime;
  } 
  
  if (Signal < Threshold && Pulse) {
    Pulse = false;
    digitalWrite(LED_BUILTIN, LOW);
  }

  delay(10);
}
