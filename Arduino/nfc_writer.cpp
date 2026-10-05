#include "nfc_writer.h"
#include "driver/rtc_io.h"

NfcWriter::NfcWriter() : st25dv(-1, -1, &Wire), initialized(false) {}

bool NfcWriter::begin()
{
  // Power on the NFC module (GPIO4 directly supplies ST25DV VCC)
  pinMode(NFC_POWER_PIN, OUTPUT);
  digitalWrite(NFC_POWER_PIN, HIGH);
  delay(NFC_POWER_ON_DELAY_MS); // Wait for ST25DV to stabilize after power on

  Wire.begin(NFC_SDA_PIN, NFC_SCL_PIN);

  if (st25dv.begin() == 0)
  {
    Serial.println(F("NFC: ST25DV init done"));
    initialized = true;
    return true;
  }
  else
  {
    Serial.println(F("NFC: ST25DV init failed"));
    initialized = false;
    return false;
  }
}

bool NfcWriter::writePlaceholder(const char *text)
{
  if (!initialized)
  {
    Serial.println(F("NFC: Not initialized, skipping placeholder write"));
    return false;
  }

  Serial.print(F("NFC: Writing placeholder: "));
  Serial.println(text);

  if (st25dv.writeText(String(text), "en", NDEF_TEXT_UTF8))
  {
    Serial.println(F("NFC: Placeholder write failed"));
    return false;
  }

  delay(50);
  Serial.println(F("NFC: Placeholder written"));
  return true;
}

bool NfcWriter::writePhotoUri(const String &fullUrl)
{
  if (!initialized)
  {
    Serial.println(F("NFC: Not initialized, skipping write"));
    return false;
  }

  if (fullUrl.isEmpty())
  {
    Serial.println(F("NFC: Empty URL, skipping write"));
    return false;
  }

  const char *uriProtocol;
  String uriMessage;

  // Detect URI prefix and select appropriate NDEF URI identifier
  if (fullUrl.startsWith("https://"))
  {
    uriProtocol = URI_ID_0x04_STRING; // "https://"
    uriMessage = fullUrl.substring(8); // Strip "https://"
  }
  else if (fullUrl.startsWith("http://"))
  {
    uriProtocol = URI_ID_0x03_STRING; // "http://"
    uriMessage = fullUrl.substring(7); // Strip "http://"
  }
  else
  {
    // No recognized prefix, default to http://
    uriProtocol = URI_ID_0x03_STRING;
    uriMessage = fullUrl;
  }

  Serial.print(F("NFC: Writing URI: "));
  Serial.println(fullUrl);

  if (st25dv.writeURI(uriProtocol, uriMessage.c_str(), ""))
  {
    Serial.println(F("NFC: Write failed"));
    return false;
  }

  delay(100);

  Serial.println(F("NFC: Write successful"));
  return true;
}

void NfcWriter::powerOff()
{
  // End I2C communication before cutting power
  Wire.end();

  // Cut power to ST25DV (GPIO4 directly controls VCC)
  digitalWrite(NFC_POWER_PIN, LOW);

  // Use RTC GPIO hold to maintain LOW state during deep sleep
  rtc_gpio_init(static_cast<gpio_num_t>(NFC_POWER_PIN));
  rtc_gpio_set_direction(static_cast<gpio_num_t>(NFC_POWER_PIN), RTC_GPIO_MODE_OUTPUT_ONLY);
  rtc_gpio_set_level(static_cast<gpio_num_t>(NFC_POWER_PIN), 0);
  rtc_gpio_hold_en(static_cast<gpio_num_t>(NFC_POWER_PIN));

  initialized = false;
  Serial.println(F("NFC: Power off, GPIO held LOW for deep sleep"));
}
