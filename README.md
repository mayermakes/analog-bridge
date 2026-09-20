# Analog Bridge

A modular, open hardware 16-channel measurement and logic-monitoring platform built around an ATtiny3226 and four ADS1115 ADCs. The bridge exposes a line-oriented SCPI interface over Ethernet and USB serial, making it useful for automated test fixtures, digital logic observation, and lightweight data acquisition.

> **Status:** Work in progress. The hardware, firmware, and host tools are under active development.

## Features

- 16 single-ended analog inputs per device
- 3.3 V logic-level classification in the included monitor application
- Four ADS1115 converters on a shared I²C bus
- ATtiny3226-based firmware
- Ethernet connectivity through an ENC28J60
- USB-UART access at 115200 8N1
- SCPI commands for identification, channel configuration, status, and measurements
- Python web dashboards for live channel monitoring and oscilloscope-style plotting
- KiCad PCB and schematic sources
- 3D-printable enclosure plates and manufacturing files

## Repository layout

```text
analog-bridge/
├── analog-bridge-PCB/       # KiCad project, schematic, and PCB layout
├── analog-bridge/            # PlatformIO firmware and Python host tools
│   ├── src/                  # Firmware sources
│   ├── include/              # Firmware headers
│   ├── lib/                  # Project libraries
│   ├── test/                 # PlatformIO tests
│   ├── platformio.ini        # Build and upload configuration
│   ├── API_README.md         # Complete SCPI and transport reference
│   ├── analog_bridge_monitor.py
│   └── analog_bridge_oscilloscope.py
└── manufacturing/            # 3MF and SVG enclosure/manufacturing files
```

## Channel mapping

Each ADS1115 contributes four single-ended inputs:

| Logical channels | ADS1115 address | Inputs |
| --- | --- | --- |
| 0–3 | `0x48` | AIN0–AIN3 |
| 4–7 | `0x49` | AIN0–AIN3 |
| 8–11 | `0x4A` | AIN0–AIN3 |
| 12–15 | `0x4B` | AIN0–AIN3 |

The firmware uses the ADS1115 `GAIN_ONE` setting, with a nominal input range of approximately ±4.096 V. Always observe the electrical limits of the ADC and the assembled hardware.

## SCPI interface

The device accepts one command per line. Commands are case-insensitive and can be sent over either transport:

- **Ethernet:** DHCP, TCP port `5025`
- **USB serial:** `115200 8N1`

Example Ethernet session:

```bash
printf '*IDN?\n' | nc 192.168.1.42 5025
printf 'MEAS:RAW? 6\n' | nc 192.168.1.42 5025
```

Frequently used commands include:

| Command | Purpose |
| --- | --- |
| `*IDN?` | Return device identification |
| `*RST` | Restore enabled channels and 16 SPS defaults |
| `SYST:ERR?` | Read and clear the latest error |
| `CONF:CHAN<n>:RATE <sps>` | Set a channel’s sampling rate |
| `CONF:CHAN<n>:STAT ON\|OFF` | Enable or disable a channel |
| `MEAS:RAW? <n>` | Read the latest raw ADC value |
| `MEAS:RAW:ALL?` | Return all 16 channels as CSV records |

Supported rates are 8, 16, 32, 64, 128, 250, 475, and 860 SPS. For the full command reference, wiring information, response formats, and transport details, see [`analog-bridge/API_README.md`](analog-bridge/API_README.md).

> The SCPI interface has no authentication or encryption. Use Ethernet access only on a trusted network.

## Firmware development

The firmware is built with [PlatformIO](https://platformio.org/) and the Arduino framework.

### Prerequisites

- PlatformIO Core or the PlatformIO IDE extension
- An ATtiny3226 UPDI programmer
- `pymcuprog` for UPDI upload:

```bash
python3 -m pip install pymcuprog
```

### Build and upload

From the firmware directory:

```bash
cd analog-bridge
pio run
pio run -e Upload_UPDI -t upload
```

Configure the upload port and speed for your UPDI adapter according to your local setup. The default PlatformIO environment is `Upload_UPDI`.

### ADC diagnostic firmware

The repository includes a direct ADC test firmware that bypasses the normal SCPI scheduler:

```bash
pio run -e ADC_TEST -t upload
```

Open the serial port at `115200 8N1` to view probe and raw conversion output. Use this mode to isolate ADC, I²C, power, address, or wiring problems.

## Python host tools

The host applications use only the Python standard library unless USB serial discovery is needed.

### Live channel monitor

The monitor discovers up to two bridges over Ethernet or USB serial and serves a live 32-channel dashboard. Ethernet discovery scans local IPv4 `/24` networks for TCP port `5025`; USB discovery requires `pyserial`.

```bash
cd analog-bridge
python3 -m pip install pyserial  # optional, required for USB discovery
python3 analog_bridge_monitor.py
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080). By default, channels are classified as:

- `HIGH`: ≥ 2.00 V
- `LOW`: ≤ 0.80 V
- `doubt`: between the two thresholds

Customize the thresholds, serial port, or web server:

```bash
python3 analog_bridge_monitor.py \
  --serial-port /dev/ttyUSB0 \
  --high 2.2 \
  --low 0.7 \
  --port 8080
```

### Network oscilloscope

The oscilloscope discovers up to four Ethernet bridges and plots AIN0 from each ADS1115: logical channels 0, 4, 8, and 12.

```bash
python3 analog_bridge_oscilloscope.py
```

Open [http://127.0.0.1:8090](http://127.0.0.1:8090). Adjust the rolling history and polling interval with:

```bash
python3 analog_bridge_oscilloscope.py --history 60 --poll 0.1 --port 8090
```

The tools use the device’s millisecond timestamp, which is relative to device boot rather than synchronized Unix time.

## Hardware sources

The KiCad project is in [`analog-bridge-PCB/`](analog-bridge-PCB/). It includes the main PCB, project configuration, and analog-section schematic. Manufacturing and enclosure assets are in [`manufacturing/`](manufacturing/), including SVG and 3MF files.

The primary ATtiny3226 connections are:

| Function | Pin |
| --- | --- |
| I²C SCL | PB0 |
| I²C SDA | PB1 |
| UART TX | PB2 |
| UART RX | PB3 |
| ADS alert 0–3 | PB4, PB5, PC0, PC1 |
| ENC28J60 MOSI/MISO/SCK | PA1, PA2, PA3 |
| ENC28J60 CS/INT | PA4, PA6 |
| UPDI | PA0 |

## Safety and limitations

- Verify input voltage, grounding, and signal integrity before connecting external equipment.
- The analog inputs are not isolated and the SCPI network service is unauthenticated.
- Measurement rates are per-channel scheduling targets; use returned timestamps to determine the achieved rate.
- Configuration is held in RAM and returns to the default state after reset or power loss.

## Contributing

Issues and pull requests are welcome. When reporting a hardware or firmware problem, include the board revision, firmware environment, transport used, wiring, and relevant serial or SCPI output.

## License

This project is licensed under the [Open Community License v1.1 (OCL v1.1)](LICENSE), without add-on conditions.

OCL v1.1 grants non-commercial users the right to use, copy, modify, and repair the project and requires distributed derivatives to remain under OCL. Commercial business use is limited to internal use unless a separate business or repair license is obtained. See [`LICENSE`](LICENSE) for the complete terms.

Third-party components and dependencies distributed with or alongside this project remain under their respective licenses where those licenses are incompatible with OCL. The authoritative OCL text and available add-ons are maintained by the [OpenCommunityLicence project](https://github.com/OpenCommunityLicence/OpenCommunityLicence).
