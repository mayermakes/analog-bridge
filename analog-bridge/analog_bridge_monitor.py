#!/usr/bin/env python3
"""Discover and monitor up to two Analog Bridge devices.

Ethernet discovery scans local IPv4 /24 networks for TCP port 5025.
USB discovery requires pyserial and probes serial ports at 115200 8N1.
The web UI is served at http://127.0.0.1:8080 by default.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

TCP_PORT = 5025
CHANNELS_PER_DEVICE = 16
MAX_DEVICES = 2
DEFAULT_HIGH_V = 2.0
DEFAULT_LOW_V = 0.8
DEFAULT_GAIN_V = 4.096
DEFAULT_POLL_SECONDS = 0.25


@dataclass
class Device:
    transport: str
    address: str
    label: str
    connection: object
    lock: threading.Lock = field(default_factory=threading.Lock)
    channels: list[dict] = field(default_factory=list)

    def command(self, command: str, expected_lines: int = 1) -> list[str]:
        with self.lock:
            if self.transport == "Ethernet":
                sock = self.connection
                sock.sendall((command + "\n").encode("ascii"))
                lines = read_socket_lines(sock, expected_lines)
            else:
                serial_port = self.connection
                serial_port.write((command + "\n").encode("ascii"))
                lines = []
                deadline = time.monotonic() + 2
                while len(lines) < expected_lines and time.monotonic() < deadline:
                    line = serial_port.readline().decode("ascii", errors="replace").strip()
                    if line:
                        lines.append(line)
            return lines

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass


def read_socket_lines(sock: socket.socket, count: int) -> list[str]:
    lines: list[str] = []
    buffer = b""
    deadline = time.monotonic() + 2
    while len(lines) < count and time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            line = raw.decode("ascii", errors="replace").strip()
            if line:
                lines.append(line)
                if len(lines) == count:
                    break
    return lines

def identify_ethernet(host: str) -> Optional[Device]:
    try:
        sock = socket.create_connection((host, TCP_PORT), timeout=0.25)
        sock.settimeout(2)
        sock.sendall(b"*IDN?\n")
        response = read_socket_lines(sock, 1)
        if not response or not response[0].startswith("AnalogBridge,16-Channel Analog Bridge"):
            sock.close()
            return None
        return Device("Ethernet", host, response[0], sock)
    except (OSError, ValueError):
        return None


def local_networks() -> list[ipaddress.IPv4Network]:
    networks: set[ipaddress.IPv4Network] = set()
    try:
        output = subprocess.check_output(
            ["ip", "-o", "-4", "addr", "show", "up"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in output.splitlines():
            fields = line.split()
            try:
                address = fields[fields.index("inet") + 1]
                interface = ipaddress.ip_interface(address)
            except (ValueError, IndexError):
                continue
            if interface.ip.is_private:
                networks.add(ipaddress.ip_network(f"{interface.ip}/24", strict=False))
    except (OSError, subprocess.SubprocessError):
        pass
    if not networks:
        networks.add(ipaddress.ip_network("192.168.1.0/24"))
    return sorted(networks, key=str)


def discover_ethernet() -> list[Device]:
    hosts = [str(host) for network in local_networks() for host in network.hosts()]
    found: list[Device] = []
    found_lock = threading.Lock()

    def probe(host: str) -> None:
        device = identify_ethernet(host)
        if device is not None:
            with found_lock:
                if len(found) < MAX_DEVICES:
                    found.append(device)
                else:
                    device.close()

    # The bridge accepts one TCP client, so avoid flooding its listener while
    # scanning the whole subnet.
    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(probe, hosts))
    return sorted(found, key=lambda device: device.address)


def discover_usb(serial_path: Optional[str] = None) -> list[Device]:
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        return []

    found: list[Device] = []
    ports = list_ports.comports()
    if serial_path:
        ports = [port_info for port_info in ports if port_info.device == serial_path]
        if not ports:
            print(f"USB serial port not found: {serial_path}; probing available USB ports")
            ports = list_ports.comports()
    ports = [
        port_info
        for port_info in ports
        if port_info.device.startswith(("/dev/ttyUSB", "/dev/ttyACM"))
        or port_info.vid is not None
    ]
    for port_info in ports:
        try:
            serial_port = serial.Serial(
                port_info.device,
                115200,
                timeout=0.2,
                dsrdtr=False,
                rtscts=False,
            )
            # Keep CP2102 modem-control lines from holding the ATtiny in reset.
            serial_port.dtr = False
            serial_port.rts = False
            identity = ""
            time.sleep(2.0)
            for _ in range(3):
                serial_port.reset_input_buffer()
                serial_port.write(b"*IDN?\r\n")
                serial_port.flush()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    line = serial_port.readline().decode("ascii", errors="replace").strip()
                    if line.startswith("AnalogBridge,16-Channel Analog Bridge"):
                        identity = line
                        break
                if identity:
                    break
            if identity:
                found.append(Device("USB", port_info.device, identity, serial_port))
            else:
                print(f"USB port did not answer SCPI: {port_info.device} ({port_info.description})")
                serial_port.close()
        except (OSError, serial.SerialException) as error:
            print(f"Cannot open USB serial port {port_info.device}: {error}")
            continue
        if len(found) >= MAX_DEVICES:
            break
    return found


def parse_measurements(device: Device, lines: list[str]) -> list[dict]:
    measurements = []
    for line in lines:
        fields = line.split(",")
        if len(fields) != 5:
            continue
        try:
            channel, enabled, rate = (int(fields[index]) for index in range(3))
            value = None if fields[3] == "NAN" else int(fields[3])
            timestamp = None if fields[4] == "NAN" else int(fields[4])
        except ValueError:
            continue
        voltage = None if value is None else value * DEFAULT_GAIN_V / 32768.0
        measurements.append({
            "channel": channel,
            "enabled": bool(enabled),
            "rate": rate,
            "raw": value,
            "voltage": voltage,
            "timestamp": timestamp,
        })
    by_channel = {item["channel"]: item for item in measurements}
    return [by_channel.get(channel, {"channel": channel}) for channel in range(CHANNELS_PER_DEVICE)]


class Monitor:
    def __init__(self, devices: list[Device], high_v: float, low_v: float, poll_seconds: float):
        self.devices = devices
        self.high_v = high_v
        self.low_v = low_v
        self.poll_seconds = poll_seconds
        self.lock = threading.Lock()
        self.last_update = None
        self.running = True

    def poll(self) -> None:
        while self.running:
            for device in self.devices:
                try:
                    channels = parse_measurements(device, device.command("MEAS:RAW:ALL?", 16))
                    with self.lock:
                        device.channels = channels
                        self.last_update = time.time()
                except (OSError, ValueError, TimeoutError):
                    pass
            time.sleep(self.poll_seconds)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "thresholds": {"high": self.high_v, "low": self.low_v},
                "updated": self.last_update,
                "devices": [
                    {
                        "transport": device.transport,
                        "address": device.address,
                        "identity": device.label,
                        "channels": device.channels,
                    }
                    for device in self.devices
                ],
            }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Analog Bridge Monitor</title><style>
:root { color-scheme: dark; font-family: system-ui, sans-serif; background:#10151c; color:#e9eef5; }
body { margin:0; padding:24px; } h1 { margin:0 0 6px; font-size:1.6rem; }
#summary { color:#9eacbb; margin-bottom:20px; } .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; }
.channel { aspect-ratio:1; padding:16px; box-sizing:border-box; border:1px solid #2e3946; background:#18212b; display:flex; flex-direction:column; justify-content:space-between; }
.channel h2 { margin:0; font-size:1rem; color:#b9c5d1; } .state { font-size:2rem; font-weight:700; letter-spacing:.02em; }
.high { color:#55d98a; } .low { color:#6bb5ff; } .doubt { color:#ff4f5e; } .missing { color:#8995a3; }
.value { font-size:1.25rem; } .meta { color:#8c9aaa; font-size:.83rem; line-height:1.5; }
</style></head><body><h1>Analog Bridge Monitor</h1><div id="summary">Discovering devices...</div><main class="grid" id="grid"></main>
<script>
const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function render(data) { const grid=document.querySelector('#grid'); const cards=[];
  data.devices.forEach((device,di) => device.channels.forEach((ch,ci) => {
    const voltage=ch.voltage; let state='doubt'; let cls='doubt';
    if (voltage === null || voltage === undefined) { state='NO DATA'; cls='missing'; }
    else if (voltage >= data.thresholds.high) { state='HIGH'; cls='high'; }
    else if (voltage <= data.thresholds.low) { state='LOW'; cls='low'; }
    cards.push(`<article class="channel"><h2>Device ${di+1} · CH ${ci}</h2><div class="state ${cls}">${state}</div><div class="value">${voltage == null ? 'N/A' : voltage.toFixed(3)+' V'}</div><div class="meta">${esc(device.transport)} · ${esc(device.address)}<br>timestamp: ${ch.timestamp == null ? 'N/A' : ch.timestamp+' ms'}</div></article>`);
  }));
  grid.innerHTML=cards.join(''); const updated=data.updated ? new Date(data.updated*1000).toLocaleTimeString() : 'waiting';
  document.querySelector('#summary').textContent=`${data.devices.length} device(s), ${cards.length} channel(s) · HIGH ≥ ${data.thresholds.high.toFixed(2)} V · LOW ≤ ${data.thresholds.low.toFixed(2)} V · updated ${updated}`;
}
async function refresh(){ try { render(await (await fetch('/api/state')).json()); } catch(e) { document.querySelector('#summary').textContent='Monitor connection lost'; }}
refresh(); setInterval(refresh, 250);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    monitor: Monitor

    def do_GET(self) -> None:
        if self.path == "/api/state":
            body = json.dumps(self.monitor.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            self.send_error(404)
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--high", type=float, default=DEFAULT_HIGH_V, help="HIGH threshold in volts")
    parser.add_argument("--low", type=float, default=DEFAULT_LOW_V, help="LOW threshold in volts")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS, help="poll interval in seconds")
    parser.add_argument("--serial-port", help="USB-UART device path, for example /dev/ttyUSB0")
    parser.add_argument("--host", default="127.0.0.1", help="web server bind address")
    parser.add_argument("--port", type=int, default=8080, help="web server port")
    args = parser.parse_args()
    if args.low >= args.high:
        parser.error("--low must be less than --high")

    devices: list[Device] = []
    while not devices:
        usb_devices = discover_usb(args.serial_port)
        devices = list(usb_devices)
        if devices:
            print("USB device(s) found; skipping Ethernet discovery.")
        elif args.serial_port is None:
            print("Searching local IPv4 networks for Ethernet devices...")
            devices = discover_ethernet()
        if not devices:
            print("No Analog Bridge devices found; retrying in 2 seconds")
            try:
                time.sleep(2)
            except KeyboardInterrupt:
                raise SystemExit("Discovery cancelled")
    devices = devices[:MAX_DEVICES]
    for device in devices:
        print(f"Found {device.label} via {device.transport}: {device.address}")

    monitor = Monitor(devices, args.high, args.low, args.poll)
    poll_thread = threading.Thread(target=monitor.poll, daemon=True)
    poll_thread.start()
    Handler.monitor = monitor
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Open http://{args.host}:{args.port} in a browser")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.running = False
        server.server_close()
        for device in devices:
            device.close()


if __name__ == "__main__":
    main()
