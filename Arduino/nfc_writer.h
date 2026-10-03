#ifndef NFC_WRITER_H
#define NFC_WRITER_H

#include <Wire.h>
#include "ST25DVSensor.h"
#include "config.h"

class NfcWriter
{
private:
  ST25DV st25dv;
  bool initialized;

public:
  NfcWriter();
  bool begin();
  bool writePlaceholder(const char *text = "Updating...");
  bool writePhotoUri(const String &fullUrl);
  void powerOff();
};

#endif // NFC_WRITER_H
