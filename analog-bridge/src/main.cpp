#include <Adafruit_ADS1X15.h>
#include <Arduino.h>
#include <Ethernet.h>
#include <SPI.h>
#include <Wire.h>

// ATtiny3226 wiring. PA0 remains dedicated to UPDI.
constexpr uint8_t ENC28J60_CS = PIN_PA4;
constexpr uint8_t ENC28J60_INT = PIN_PA6;
constexpr uint8_t ADS_ALERT_PINS[4] = {PIN_PB4, PIN_PB5, PIN_PC0, PIN_PC1};
constexpr uint8_t ADS_ADDRESSES[4] = {0x48, 0x49, 0x4A, 0x4B};
constexpr uint16_t ADS_SINGLE_MUX[4] = {
  ADS1X15_REG_CONFIG_MUX_SINGLE_0,
  ADS1X15_REG_CONFIG_MUX_SINGLE_1,
  ADS1X15_REG_CONFIG_MUX_SINGLE_2,
  ADS1X15_REG_CONFIG_MUX_SINGLE_3
};
constexpr uint16_t SCPI_PORT = 5025;

constexpr uint8_t CHANNEL_COUNT = 16;
constexpr uint16_t DEFAULT_RATE = 16;
constexpr uint32_t DEFAULT_PERIOD_MS = 1000UL / DEFAULT_RATE;

struct ChannelConfig {
  bool enabled;
  uint16_t rate;
  uint32_t periodMs;
  uint32_t nextDue;
};

struct Sample {
  uint32_t timestampMs;
  int16_t value;
  bool valid;
};

Adafruit_ADS1115 ads[4];
ChannelConfig channelConfig[CHANNEL_COUNT];
Sample latestSamples[CHANNEL_COUNT];
EthernetServer server(SCPI_PORT);
EthernetClient ethernetScpiClient;
String serialCommand;
String ethernetCommand;
String lastScpiError = "0,\"No error\"";

volatile bool adsAlert[4] = {false, false, false, false};
int8_t activeChannel[4] = {-1, -1, -1, -1};
uint8_t nextInput[4] = {0, 0, 0, 0};
uint32_t conversionStarted[4] = {0, 0, 0, 0};

void makeMacAddress(byte mac[6]) {
  // Hash the complete factory serial so devices from the same production lot
  // cannot receive the same MAC from only the first serial bytes.
  const uint8_t serial[] = {
    SIGROW.SERNUM0, SIGROW.SERNUM1, SIGROW.SERNUM2, SIGROW.SERNUM3,
    SIGROW.SERNUM4, SIGROW.SERNUM5, SIGROW.SERNUM6, SIGROW.SERNUM7,
    SIGROW.SERNUM8, SIGROW.SERNUM9
  };
  uint64_t hash = 0xcbf29ce484222325ULL;
  for (uint8_t value : serial) {
    hash ^= value;
    hash *= 0x100000001b3ULL;
  }

  // 0x02 means locally administered unicast; the remaining bytes identify it.
  mac[0] = 0x02;
  for (uint8_t index = 0; index < 5; ++index) {
    mac[index + 1] = static_cast<byte>(hash >> (index * 8));
  }
}

void printMacAddress(const byte mac[6]) {
  for (uint8_t i = 0; i < 6; ++i) {
    if (i) Serial.print(':');
    if (mac[i] < 0x10) Serial.print('0');
    Serial.print(mac[i], HEX);
  }
}

void onAdsAlert0() { adsAlert[0] = true; }
void onAdsAlert1() { adsAlert[1] = true; }
void onAdsAlert2() { adsAlert[2] = true; }
void onAdsAlert3() { adsAlert[3] = true; }

uint16_t normalizeRate(uint16_t rate) {
  const uint16_t supported[] = {8, 16, 32, 64, 128, 250, 475, 860};
  uint16_t selected = supported[0];
  for (uint8_t i = 0; i < 8; ++i) {
    if (rate >= supported[i]) {
      selected = supported[i];
    }
  }
  return selected;
}

uint16_t adsRate(uint16_t rate) {
  switch (normalizeRate(rate)) {
    case 8: return RATE_ADS1115_8SPS;
    case 32: return RATE_ADS1115_32SPS;
    case 64: return RATE_ADS1115_64SPS;
    case 128: return RATE_ADS1115_128SPS;
    case 250: return RATE_ADS1115_250SPS;
    case 475: return RATE_ADS1115_475SPS;
    case 860: return RATE_ADS1115_860SPS;
    default: return RATE_ADS1115_16SPS;
  }
}

// Public control point for the host protocol and application code.
bool setChannelSamplingRate(uint8_t channel, uint16_t rate) {
  if (channel >= CHANNEL_COUNT || rate == 0) {
    return false;
  }
  channelConfig[channel].rate = normalizeRate(rate);
  channelConfig[channel].periodMs = 1000UL / channelConfig[channel].rate;
  channelConfig[channel].nextDue = millis();
  return true;
}

bool setChannelEnabled(uint8_t channel, bool enabled) {
  if (channel >= CHANNEL_COUNT) {
    return false;
  }
  channelConfig[channel].enabled = enabled;
  channelConfig[channel].nextDue = millis();
  return true;
}

void sendSample(uint8_t channel, int16_t value, uint32_t timestamp) {
  latestSamples[channel] = {timestamp, value, true};
}

void startDueConversions(uint32_t now) {
  for (uint8_t device = 0; device < 4; ++device) {
    if (activeChannel[device] >= 0) {
      continue;
    }
    for (uint8_t offset = 0; offset < 4; ++offset) {
      uint8_t input = (nextInput[device] + offset) % 4;
      uint8_t channel = device * 4 + input;
      if (channelConfig[channel].enabled &&
          static_cast<int32_t>(now - channelConfig[channel].nextDue) >= 0) {
        ads[device].setDataRate(adsRate(channelConfig[channel].rate));
        ads[device].startADCReading(ADS_SINGLE_MUX[input], false);
        activeChannel[device] = channel;
        nextInput[device] = (input + 1) % 4;
        conversionStarted[device] = now;
        channelConfig[channel].nextDue = now + channelConfig[channel].periodMs;
        break;
      }
    }
  }
}

void finishConversions(uint32_t now) {
  for (uint8_t device = 0; device < 4; ++device) {
    if (activeChannel[device] < 0) {
      continue;
    }
    // The alert input is wired for conversion-ready. The timeout keeps the
    // scheduler recoverable if a sensor is unplugged or its alert line fails.
    if (!ads[device].conversionComplete() && now - conversionStarted[device] < 130) {
      continue;
    }
    adsAlert[device] = false;
    int16_t value = ads[device].getLastConversionResults();
    sendSample(activeChannel[device], value, now);
    activeChannel[device] = -1;
  }
}

void setScpiError(const __FlashStringHelper *message) {
  lastScpiError = "-100,\"";
  lastScpiError += message;
  lastScpiError += '"';
}

void resetConfiguration() {
  uint32_t now = millis();
  for (uint8_t channel = 0; channel < CHANNEL_COUNT; ++channel) {
    channelConfig[channel] = {true, DEFAULT_RATE, DEFAULT_PERIOD_MS, now};
  }
}

bool parseChannel(const String &command, int &channel, int &separator) {
  if (!command.startsWith(F("CONF:CHAN"))) return false;
  separator = command.indexOf(':', 9);
  if (separator < 0) return false;
  channel = command.substring(9, separator).toInt();
  return channel >= 0 && channel < CHANNEL_COUNT;
}

void writeChannelStatus(Print &output, uint8_t channel) {
  output.print(channel);
  output.print(',');
  output.print(channelConfig[channel].enabled ? 1 : 0);
  output.print(',');
  output.print(channelConfig[channel].rate);
  output.print(',');
  if (latestSamples[channel].valid) output.print(latestSamples[channel].value);
  else output.print(F("NAN"));
  output.print(',');
  if (latestSamples[channel].valid) output.println(latestSamples[channel].timestampMs);
  else output.println(F("NAN"));
}

void processScpiCommand(String command, Print &output) {
  command.trim();
  command.toUpperCase();
  if (command.length() == 0) return;

  if (command == F("*IDN?")) {
    output.println(F("AnalogBridge,16-Channel Analog Bridge,ATtiny3226,1.0"));
    return;
  }
  if (command == F("*RST")) {
    resetConfiguration();
    output.println(F("OK"));
    return;
  }
  if (command == F("SYST:ERR?")) {
    output.println(lastScpiError);
    lastScpiError = "0,\"No error\"";
    return;
  }
  if (command == F("MEAS:RAW:ALL?")) {
    for (uint8_t channel = 0; channel < CHANNEL_COUNT; ++channel) {
      writeChannelStatus(output, channel);
    }
    return;
  }
  if (command.startsWith(F("MEAS:RAW?"))) {
    int channel = command.substring(9).toInt();
    if (channel >= 0 && channel < CHANNEL_COUNT && latestSamples[channel].valid) {
      output.println(latestSamples[channel].value);
    } else if (channel >= 0 && channel < CHANNEL_COUNT) {
      output.println(F("NAN"));
    } else {
      setScpiError(F("Invalid channel"));
      output.println(F("ERROR"));
    }
    return;
  }

  int channel;
  int separator;
  if (parseChannel(command, channel, separator)) {
    String setting = command.substring(separator);
    if (setting == F(":RATE?")) {
      output.println(channelConfig[channel].rate);
      return;
    }
    if (setting.startsWith(F(":RATE "))) {
      int rate = setting.substring(6).toInt();
      if (rate > 0 && setChannelSamplingRate(channel, rate)) {
        output.println(channelConfig[channel].rate);
      } else {
        setScpiError(F("Invalid rate"));
        output.println(F("ERROR"));
      }
      return;
    }
    if (setting == F(":STAT?")) {
      output.println(channelConfig[channel].enabled ? F("ON") : F("OFF"));
      return;
    }
    if (setting.startsWith(F(":STAT "))) {
      String state = setting.substring(6);
      if (state == F("ON") || state == F("1")) {
        setChannelEnabled(channel, true);
      } else if (state == F("OFF") || state == F("0")) {
        setChannelEnabled(channel, false);
      } else {
        setScpiError(F("Invalid state"));
        output.println(F("ERROR"));
        return;
      }
      output.println(F("OK"));
      return;
    }
  }

  setScpiError(F("Undefined header"));
  output.println(F("ERROR"));
}

template <typename T>
void readScpiInput(T &input, String &command) {
  while (input.available()) {
    char character = static_cast<char>(input.read());
    if (character == '\n' || character == '\r') {
      if (command.length()) {
        processScpiCommand(command, input);
        command = "";
      }
    } else if (command.length() < 96) {
      command += character;
    }
  }
}

void handleScpi() {
  readScpiInput(Serial, serialCommand);
  if (!ethernetScpiClient || !ethernetScpiClient.connected()) {
    ethernetScpiClient = server.available();
    ethernetCommand = "";
  }
  if (ethernetScpiClient) {
    readScpiInput(ethernetScpiClient, ethernetCommand);
  }
}

void startEthernet(const byte mac[6]) {
  while (Ethernet.begin(mac) == 0 || Ethernet.localIP() == IPAddress(0, 0, 0, 0)) {
    Serial.println(F("DHCP failed; retrying in 2 seconds"));
    delay(2000);
  }
}

void setup() {
  Serial.begin(115200);
  Wire.begin();
  Wire.setClock(400000);

  pinMode(ENC28J60_CS, OUTPUT);
  digitalWrite(ENC28J60_CS, HIGH);
  pinMode(ENC28J60_INT, INPUT_PULLUP);
  for (uint8_t i = 0; i < 4; ++i) pinMode(ADS_ALERT_PINS[i], INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(ADS_ALERT_PINS[0]), onAdsAlert0, FALLING);
  attachInterrupt(digitalPinToInterrupt(ADS_ALERT_PINS[1]), onAdsAlert1, FALLING);
  attachInterrupt(digitalPinToInterrupt(ADS_ALERT_PINS[2]), onAdsAlert2, FALLING);
  attachInterrupt(digitalPinToInterrupt(ADS_ALERT_PINS[3]), onAdsAlert3, FALLING);

  for (uint8_t channel = 0; channel < CHANNEL_COUNT; ++channel) {
    channelConfig[channel] = {true, DEFAULT_RATE, DEFAULT_PERIOD_MS, static_cast<uint32_t>(millis())};
    latestSamples[channel] = {0, 0, false};
  }
  for (uint8_t device = 0; device < 4; ++device) {
    if (!ads[device].begin(ADS_ADDRESSES[device])) {
      Serial.print(F("ADS1115 missing at 0x"));
      Serial.println(ADS_ADDRESSES[device], HEX);
    }
    ads[device].setGain(GAIN_ONE);
  }

  byte mac[6];
  makeMacAddress(mac);
  Serial.print(F("MAC: "));
  printMacAddress(mac);
  Serial.println();
  Ethernet.init(ENC28J60_CS);
  startEthernet(mac);
  server.begin();
  Serial.print(F("SCPI ready: tcp://"));
  Serial.println(Ethernet.localIP());
}

void loop() {
  uint32_t now = millis();
  finishConversions(now);
  startDueConversions(now);
  Ethernet.maintain();
  handleScpi();
}