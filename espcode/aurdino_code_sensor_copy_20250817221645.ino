#include <WiFi.h>
#include <HTTPClient.h>
#include "DHT.h"

// ===== Pin Definitions =====
#define DHTPIN 4
#define DHTTYPE DHT22

#define TRIG_PIN 5
#define ECHO_PIN 18

#define RAIN_SENSOR_AO 34   // Analog pin for rain sensor (AO)
#define RAIN_SENSOR_DO 32   // Digital pin for rain sensor (DO)

#define MQ_SENSOR_AO 35     // Analog pin for MQ gas sensor (AO)
#define MQ_SENSOR_DO 33     // Digital pin for MQ gas sensor (DO)

#define SOIL_SENSOR_DO 25   // Digital pin for YL-69 soil moisture sensor (DO)

#define LDR_PIN 14          // Analog pin for LDR (light sensor)

#define PH_SENSOR_AO 26     // Analog pin for pH sensor

// ===== WiFi Credentials =====
const char* ssid = "wahid";
const char* password = "123456789";

// ===== Backend Endpoint =====
const char* serverUrl = "https://weed-detection-iot-backend.vercel.app/api/sensors";

// ===== Sensor Objects =====
DHT dht(DHTPIN, DHTTYPE);

void setup() {
  Serial.begin(115200);

  // Ultrasonic sensor
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);

  // Rain sensor
  pinMode(RAIN_SENSOR_AO, INPUT);
  pinMode(RAIN_SENSOR_DO, INPUT);

  // MQ gas sensor
  pinMode(MQ_SENSOR_AO, INPUT);
  pinMode(MQ_SENSOR_DO, INPUT);

  // Soil moisture sensor (digital only)
  pinMode(SOIL_SENSOR_DO, INPUT);

  // LDR sensor
  pinMode(LDR_PIN, INPUT);

  // pH sensor
  pinMode(PH_SENSOR_AO, INPUT);

  // DHT sensor
  dht.begin();

  // WiFi connection
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nConnected to WiFi!");
}

// ===== Ultrasonic Sensor Function =====
float getDistance() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  long duration = pulseIn(ECHO_PIN, HIGH);
  float distance = duration * 0.034 / 2; // cm
  return distance;
}

// ===== Rain Sensor Functions =====
int getRainLevelAnalog() { return analogRead(RAIN_SENSOR_AO); }
int getRainLevelDigital() { return digitalRead(RAIN_SENSOR_DO); }

// ===== MQ Gas Sensor Functions =====
int getGasLevelAnalog() { return analogRead(MQ_SENSOR_AO); }
int getGasLevelDigital() { return digitalRead(MQ_SENSOR_DO); }

// ===== Soil Moisture Sensor Function (Digital Only) =====
int getSoilMoistureDigital() { return digitalRead(SOIL_SENSOR_DO); }

// ===== LDR Sensor Function =====
int getLDRValue() { return analogRead(LDR_PIN); } // 0 (dark) → 4095 (bright)

// ===== pH Sensor Function =====
int getPHValue() { return analogRead(PH_SENSOR_AO); } // Raw analog value, convert to pH if needed

void loop() {
  if (WiFi.status() == WL_CONNECTED) {
    float temperature = dht.readTemperature();
    float humidity = dht.readHumidity();
    float distance = getDistance();

    int rainAnalog = getRainLevelAnalog();
    int rainDigital = getRainLevelDigital();

    int gasAnalog = getGasLevelAnalog();
    int gasDigital = getGasLevelDigital();

    int soilDigital = getSoilMoistureDigital();
    int ldrValue = getLDRValue();
    int phValue = getPHValue();

    if (isnan(temperature) || isnan(humidity)) {
      Serial.println("Failed to read from DHT sensor!");
      return;
    }

    HTTPClient http;
    http.begin(serverUrl);
    http.addHeader("Content-Type", "application/json");

    String jsonData = "{\"temperature\":" + String(temperature) +
                      ",\"humidity\":" + String(humidity) +
                      ",\"distance\":" + String(distance) +
                      ",\"rain_level_analog\":" + String(rainAnalog) +
                      ",\"rain_level_digital\":" + String(rainDigital) +
                      ",\"gas_level_analog\":" + String(gasAnalog) +
                      ",\"gas_level_digital\":" + String(gasDigital) +
                      ",\"soil_moisture_digital\":" + String(soilDigital) +
                      ",\"ldr_value\":" + String(ldrValue) +
                      ",\"ph_value\":" + String(phValue) + "}";

    int httpResponseCode = http.POST(jsonData);

    if (httpResponseCode > 0) {
      Serial.println("Data sent! Response: " + String(httpResponseCode));
      Serial.println(http.getString());
    } else {
      Serial.println("Error sending data: " + String(httpResponseCode));
    }
    http.end();
  }

  delay(5000); // every 5 seconds
}