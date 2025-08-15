#include <WiFi.h>
#include <HTTPClient.h>
#include <Adafruit_BMP280.h>
#include <Adafruit_Sensor.h>

// ====== WiFi & Server ======
const char* WIFI_SSID     = "YOUR_WIFI";
const char* WIFI_PASSWORD = "YOUR_PASS";
const char* SERVER        = "http://<SERVER_IP>:5000"; // Flask server

// ====== TB6612 Pins ======
#define STBY 33

// Left motor -> A channel
#define AIN1 26
#define AIN2 27
#define PWMA 25

// Right motor -> B channel
#define BIN1 12
#define BIN2 13
#define PWMB 14

// ====== Sensors ======
#define TRIG_PIN 5
#define ECHO_PIN 18
#define RAIN_PIN 35
#define MOISTURE_PIN 32
#define PH_PIN 33  // if conflicts with STBY, move PH to 39 and keep STBY=33
#define LDR_PIN 34

Adafruit_BMP280 bmp; // I2C 21/22

// ====== Helpers ======
void motorStop(){
  digitalWrite(STBY, HIGH);
  digitalWrite(AIN1, LOW); digitalWrite(AIN2, LOW);
  digitalWrite(BIN1, LOW); digitalWrite(BIN2, LOW);
  ledcWrite(0, 0); ledcWrite(1, 0);
}
void motorForward(int spd){ // 0-255
  digitalWrite(STBY, HIGH);
  digitalWrite(AIN1, HIGH); digitalWrite(AIN2, LOW);
  digitalWrite(BIN1, HIGH); digitalWrite(BIN2, LOW);
  ledcWrite(0, spd); ledcWrite(1, spd);
}
void motorBackward(int spd){
  digitalWrite(STBY, HIGH);
  digitalWrite(AIN1, LOW); digitalWrite(AIN2, HIGH);
  digitalWrite(BIN1, LOW); digitalWrite(BIN2, HIGH);
  ledcWrite(0, spd); ledcWrite(1, spd);
}
void motorLeft(int spd){
  digitalWrite(STBY, HIGH);
  // left backward, right forward
  digitalWrite(AIN1, LOW); digitalWrite(AIN2, HIGH);
  digitalWrite(BIN1, HIGH); digitalWrite(BIN2, LOW);
  ledcWrite(0, spd); ledcWrite(1, spd);
}
void motorRight(int spd){
  digitalWrite(STBY, HIGH);
  // left forward, right backward
  digitalWrite(AIN1, HIGH); digitalWrite(AIN2, LOW);
  digitalWrite(BIN1, LOW); digitalWrite(BIN2, HIGH);
  ledcWrite(0, spd); ledcWrite(1, spd);
}

long readUltrasonicCM(){
  digitalWrite(TRIG_PIN, LOW); delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH); delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  long duration = pulseIn(ECHO_PIN, HIGH, 30000);
  if (!duration) return -1;
  return duration * 0.034 / 2.0;
}

void postJSON(const String& path, const String& json){
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(String(SERVER) + path);
  http.addHeader("Content-Type","application/json");
  http.POST(json);
  http.end();
}

void setup(){
  Serial.begin(115200);

  pinMode(STBY, OUTPUT);
  pinMode(AIN1, OUTPUT); pinMode(AIN2, OUTPUT); pinMode(PWMA, OUTPUT);
  pinMode(BIN1, OUTPUT); pinMode(BIN2, OUTPUT); pinMode(PWMB, OUTPUT);
  digitalWrite(STBY, HIGH);

  // PWM channels
  ledcSetup(0, 15000, 8); ledcAttachPin(PWMA, 0);
  ledcSetup(1, 15000, 8); ledcAttachPin(PWMB, 1);

  pinMode(TRIG_PIN, OUTPUT); pinMode(ECHO_PIN, INPUT);
  analogSetPinAttenuation(RAIN_PIN, ADC_11db);
  analogSetPinAttenuation(MOISTURE_PIN, ADC_11db);
  analogSetPinAttenuation(PH_PIN, ADC_11db);
  analogSetPinAttenuation(LDR_PIN, ADC_11db);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("WiFi");
  while (WiFi.status() != WL_CONNECTED){ delay(500); Serial.print("."); }
  Serial.println("\nWiFi OK");

  if(!bmp.begin(0x76)){
    Serial.println("BMP280 not found");
  }

  motorStop();
}

unsigned long tSensor=0, tCtrl=0, tTele=0;

void loop(){
  unsigned long now = millis();

  // 1) সেন্সর আপলোড (প্রতি ~3s)
  if (now - tSensor > 3000){
    tSensor = now;
    int rain = analogRead(RAIN_PIN);
    int moist = analogRead(MOISTURE_PIN);
    int ph = analogRead(PH_PIN);
    int ldr = analogRead(LDR_PIN);
    long dist = readUltrasonicCM();
    float tempC = NAN, pres = NAN;
    if (bmp.begin(0x76)){ tempC = bmp.readTemperature(); pres = bmp.readPressure()/100.0; }

    String json = "{";
    json += "\"ultrasonic_cm\":" + String(dist) + ",";
    json += "\"rain_raw\":" + String(rain) + ",";
    json += "\"moisture_raw\":" + String(moist) + ",";
    json += "\"ph_raw\":" + String(ph) + ",";
    json += "\"ldr_raw\":" + String(ldr) + ",";
    json += "\"bmp_temp_c\":" + String(tempC,2) + ",";
    json += "\"bmp_pressure_hpa\":" + String(pres,2);
    json += "}";
    postJSON("/api/sensors", json);
  }

  // 2) কন্ট্রোল পোল (প্রতি ~150ms)
  if (now - tCtrl > 150){
    tCtrl = now;
    if (WiFi.status() == WL_CONNECTED){
      HTTPClient http;
      http.begin(String(SERVER) + "/api/control/latest");
      int code = http.GET();
      if (code == 200){
        String body = http.getString();
        // কমপ্যাক্ট পার্স (সরল); ভাল হলে ArduinoJson ব্যবহার করুন
        int ic = body.indexOf("\"cmd\"");
        int is = body.indexOf("\"speed\"");
        String cmd="stop"; int speed=150;
        if (ic >= 0){
          int q1 = body.indexOf('"', ic+5);
          int q2 = body.indexOf('"', q1+1);
          int q3 = body.indexOf('"', q2+1);
          int q4 = body.indexOf('"', q3+1);
          if (q3>0 && q4>q3) cmd = body.substring(q3+1, q4);
        }
        if (is >= 0){
          int c = body.indexOf(':', is);
          int e = body.indexOf(',', c); if (e<0) e = body.indexOf('}', c);
          if (c>0 && e>c) speed = body.substring(c+1, e).toInt();
        }

        speed = constrain(speed, 0, 255);
        if      (cmd=="forward")  motorForward(speed);
        else if (cmd=="backward") motorBackward(speed);
        else if (cmd=="left")     motorLeft(speed);
        else if (cmd=="right")    motorRight(speed);
        else                      motorStop();

      }
      http.end();
    }
  }

  // 3) টেলিমেট্রি (ঐচ্ছিক, প্রতি 5s)
  if (now - tTele > 5000){
    tTele = now;
    String tel = "{\"battery_v\":"; tel += "0"; tel += "}"; // আপনার মেজারমেন্ট হলে যুক্ত করুন
    postJSON("/api/telemetry", tel);
  }
}
