#include <Adafruit_ADS1X15.h>
#include <Arduino.h>
#include <Wire.h>

constexpr uint8_t ADS_ADDRESSES[4] = {0x48, 0x49, 0x4A, 0x4B};
constexpr uint32_t REPORT_INTERVAL_MS = 1000;

Adafruit_ADS1115 ads[4];
bool adsPresent[4] = {false, false, false, false};

void printHexAddress(uint8_t address) {
  Serial.print(F("0x"));
  if (address < 0x10) Serial.print('0');
  Serial.print(address, HEX);
}

void setup() {
  Serial.begin(115200);
  Wire.begin();
  Wire.setClock(400000);

  delay(500);
  Serial.println(F("ADS1115 raw ADC test"));
  Serial.println(F("I2C: SCL=PB0 SDA=PB1"));
  Serial.println(F("address,channel,raw"));

  for (uint8_t device = 0; device < 4; ++device) {
    adsPresent[device] = ads[device].begin(ADS_ADDRESSES[device]);
    Serial.print(F("probe,"));
    printHexAddress(ADS_ADDRESSES[device]);
    Serial.print(',');
    Serial.println(adsPresent[device] ? F("present") : F("missing"));
    if (adsPresent[device]) {
      ads[device].setGain(GAIN_ONE);
      ads[device].setDataRate(RATE_ADS1115_16SPS);
    }
  }
}

void loop() {
  for (uint8_t device = 0; device < 4; ++device) {
    for (uint8_t channel = 0; channel < 4; ++channel) {
      Serial.print(F("read,"));
      printHexAddress(ADS_ADDRESSES[device]);
      Serial.print(',');
      Serial.print(channel);
      Serial.print(',');
      if (adsPresent[device]) {
        Serial.println(ads[device].readADC_SingleEnded(channel));
      } else {
        Serial.println(F("ADS_MISSING"));
      }
    }
  }
  Serial.println(F("--"));
  delay(REPORT_INTERVAL_MS);
}
