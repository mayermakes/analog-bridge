#!/usr/bin/env python3
"""Network oscilloscope for AIN0 on every discovered ADS1115.

The four traces are logical channels 0, 4, 8, and 12, corresponding to AIN0 on
ADS1115 addresses 0x48, 0x49, 0x4A, and 0x4B. The web UI is served locally.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

SCPI_PORT = 5025
MAX_DEVICES = 4
DEFAULT_HISTORY_SECONDS = 30.0
DEFAULT_POLL_SECONDS = 0.25
DEFAULT_WEB_PORT = 8090
RAW_FULL_SCALE = 32768.0
ADS_RANGE_VOLTS = 4.096
TRACE_CHANNELS = (0, 4, 8, 12)


@dataclass
class Sample:
    elapsed: float
    device_timestamp_ms: int
    raw: int
    volts: float


@dataclass
class Device:
    address: str
    identity: str
    socket: socket.socket
    lock: threading.Lock = field(default_factory=threading.Lock)
    boot_timestamp_ms: Optional[int] = None
    samples: dict[int, deque[Sample]] = field(
        default_factory=lambda: {channel: deque() for channel in TRACE_CHANNELS}
    )

    def query_channels(self) -> dict[int, tuple[int, int]]:
        with self.lock:
            try:
                self.socket.sendall(b"MEAS:RAW:ALL?\n")
                lines = read_lines(self.socket, 16)
            except OSError:
                return {}
        samples: dict[int, tuple[int, int]] = {}
        for line in lines:
            fields = line.split(",")
            if len(fields) != 5 or fields[0] not in {str(channel) for channel in TRACE_CHANNELS}:
                continue
            try:
                channel = int(fields[0])
                raw = int(fields[3])
                timestamp_ms = int(fields[4])
            except ValueError:
                continue
            samples[channel] = (raw, timestamp_ms)
        return samples

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass


def read_lines(connection: socket.socket, count: int) -> list[str]:
    lines: list[str] = []
    buffer = b""
    deadline = time.monotonic() + 2.0
    while len(lines) < count and time.monotonic() < deadline:
        connection.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            data = connection.recv(4096)
        except socket.timeout:
            break
        if not data:
            break
        buffer += data
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            value = raw.decode("ascii", errors="replace").strip()
            if value:
                lines.append(value)
                if len(lines) == count:
                    return lines
    return lines


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
    return sorted(networks or {ipaddress.ip_network("192.168.1.0/24")}, key=str)


def identify(host: str) -> Optional[Device]:
    try:
        connection = socket.create_connection((host, SCPI_PORT), timeout=0.5)
        connection.settimeout(2.0)
        connection.sendall(b"*IDN?\n")
        response = read_lines(connection, 1)
        if not response or not response[0].startswith("AnalogBridge,16-Channel Analog Bridge"):
            connection.close()
            return None
        return Device(host, response[0], connection)
    except OSError:
        return None


def discover() -> list[Device]:
    hosts = [str(host) for network in local_networks() for host in network.hosts()]
    found: list[Device] = []
    found_lock = threading.Lock()

    def probe(host: str) -> None:
        device = identify(host)
        if device is None:
            return
        with found_lock:
            if len(found) < MAX_DEVICES:
                found.append(device)
            else:
                device.close()

    threads = [threading.Thread(target=probe, args=(host,), daemon=True) for host in hosts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return sorted(found, key=lambda device: device.address)


class Scope:
    def __init__(self, devices: list[Device], history_seconds: float, poll_seconds: float):
        self.devices = devices
        self.history_seconds = history_seconds
        self.poll_seconds = poll_seconds
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.updated = 0.0
        self.running = True

    def poll(self) -> None:
        while self.running:
            for device in self.devices:
                for channel, result in device.query_channels().items():
                    raw, device_timestamp_ms = result
                    with self.lock:
                        if device.boot_timestamp_ms is None:
                            device.boot_timestamp_ms = device_timestamp_ms
                        elapsed = (device_timestamp_ms - device.boot_timestamp_ms) / 1000.0
                        device.samples[channel].append(Sample(
                                elapsed=elapsed,
                                device_timestamp_ms=device_timestamp_ms,
                                raw=raw,
                                volts=raw * ADS_RANGE_VOLTS / RAW_FULL_SCALE,
                            ))
                        cutoff = elapsed - self.history_seconds
                        while (device.samples[channel]
                               and device.samples[channel][0].elapsed < cutoff):
                            device.samples[channel].popleft()
                        self.updated = time.time()
            time.sleep(self.poll_seconds)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "updated": self.updated,
                "channels": list(TRACE_CHANNELS),
                "devices": [
                    {
                        "address": device.address,
                        "identity": device.identity,
                        "boot_timestamp_ms": device.boot_timestamp_ms,
                        "samples": {
                            str(channel): [sample.__dict__ for sample in samples]
                            for channel, samples in device.samples.items()
                        },
                    }
                    for device in self.devices
                ],
            }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Analog Bridge Oscilloscope</title><style>
:root { color-scheme:dark; font-family:system-ui,sans-serif; background:#0d1218; color:#e7edf4; }
body { margin:0; padding:22px; } h1 { margin:0 0 5px; font-size:1.5rem; } #summary { color:#99a7b6; margin-bottom:16px; }
.toolbar { border:1px solid #2d3946; background:#151d26; padding:12px; margin-bottom:12px; display:flex; flex-wrap:wrap; gap:10px 18px; align-items:center; }
.device { display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding-right:12px; border-right:1px solid #344250; }
.device:last-child { border-right:0; } .device-name { color:#b9c5d1; font:12px ui-monospace,SFMono-Regular,Menlo,monospace; margin-right:3px; }
label { color:#d5dee7; font:13px ui-monospace,SFMono-Regular,Menlo,monospace; cursor:pointer; } input { accent-color:#5ce1a4; vertical-align:-1px; }
.panel { border:1px solid #2d3946; background:#151d26; padding:12px; } canvas { width:100%; height:min(68vh,620px); display:block; background:#0b1015; }
.legend { color:#aebbc8; font-size:13px; line-height:18px; margin-top:9px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; vertical-align:-1px; }
</style></head><body><h1>AIN0 Overlay Oscilloscope</h1><div id="summary">Discovering Ethernet devices...</div><div id="controls" class="toolbar"></div><section class="panel"><canvas id="scope"></canvas><div id="legend" class="legend"></div></section>
<script>
const colors=['#5ce1a4','#60a5fa','#f6c85f','#ff6b81'];
const enabled=new Set(), knownKeys=new Set(); let latestData=null;
function traceKey(deviceIndex,channel){return `${deviceIndex}:${channel}`;}
function buildControls(data){const controls=document.querySelector('#controls');controls.innerHTML='';data.devices.forEach((device,deviceIndex)=>{const group=document.createElement('div');group.className='device';const name=document.createElement('span');name.className='device-name';name.textContent=`D${deviceIndex+1} ${device.address}`;group.appendChild(name);data.channels.forEach((channel,traceIndex)=>{const key=traceKey(deviceIndex,channel);if(!knownKeys.has(key)){enabled.add(key);knownKeys.add(key);}const label=document.createElement('label');const input=document.createElement('input');input.type='checkbox';input.checked=enabled.has(key);input.dataset.key=key;input.addEventListener('change',()=>{input.checked?enabled.add(key):enabled.delete(key);draw(data);});label.append(input,` CH${channel}`);group.appendChild(label);});controls.appendChild(group);});}
function draw(data){const canvas=document.querySelector('#scope'),ctx=canvas.getContext('2d'),ratio=devicePixelRatio,cw=canvas.clientWidth,ch=canvas.clientHeight;canvas.width=cw*ratio;canvas.height=ch*ratio;ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,cw,ch);const traces=[];data.devices.forEach((device,deviceIndex)=>data.channels.forEach((channel,traceIndex)=>{if(enabled.has(traceKey(deviceIndex,channel))) traces.push({device,deviceIndex,channel,traceIndex,samples:device.samples[String(channel)]||[]});}));const selected=traces.filter(trace=>trace.samples.length);if(!selected.length){ctx.fillStyle='#8b99a8';ctx.fillText('select a trace or wait for samples',16,28);document.querySelector('#legend').textContent='';return;}const allSamples=selected.flatMap(trace=>trace.samples);const maxTimestamp=Math.max(...allSamples.map(sample=>sample.device_timestamp_ms));const minTimestamp=maxTimestamp-30000;const minY=0,maxY=4.096;const x=t=>(t-minTimestamp)/30000*cw,y=v=>ch-(Math.max(minY,Math.min(maxY,v))-minY)/(maxY-minY)*ch;ctx.strokeStyle='#26313d';ctx.lineWidth=1;for(let i=0;i<=4;i++){const yy=i/4*ch;ctx.beginPath();ctx.moveTo(0,yy);ctx.lineTo(cw,yy);ctx.stroke();}traces.forEach(trace=>{const samples=trace.samples.filter(sample=>sample.device_timestamp_ms>=minTimestamp);if(!samples.length)return;ctx.strokeStyle=colors[trace.traceIndex];ctx.lineWidth=2;ctx.beginPath();samples.forEach((sample,index)=>{const px=x(sample.device_timestamp_ms),py=y(sample.volts);if(index)ctx.lineTo(px,py);else ctx.moveTo(px,py);});ctx.stroke();});ctx.fillStyle='#9eacba';ctx.font='12px system-ui';ctx.fillText(maxY.toFixed(2)+' V',8,14);ctx.fillText(minY.toFixed(2)+' V',8,ch-6);ctx.fillText('timestamp window: 30 s',cw-145,ch-6);document.querySelector('#legend').innerHTML=traces.map(trace=>`<span style="margin-right:14px"><i class="dot" style="background:${colors[trace.traceIndex]}"></i>D${trace.deviceIndex+1} CH${trace.channel}</span>`).join('');}
function render(data){latestData=data;buildControls(data);draw(data);let total=0;data.devices.forEach(device=>data.channels.forEach(channel=>{total+=(device.samples[String(channel)]||[]).length;}));const updated=data.updated?new Date(data.updated*1000).toLocaleTimeString():'waiting';document.querySelector('#summary').textContent=`${data.devices.length} bridge(s) | ${total} samples | overlaid by firmware timestamp | updated ${updated}`;}
async function refresh(){try{const response=await fetch('/api/state');render(await response.json());}catch(error){document.querySelector('#summary').textContent='Monitor connection lost';}} refresh();setInterval(refresh,500);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    scope: Scope

    def do_GET(self) -> None:
        if self.path == "/":
            body = PAGE.encode()
            content_type = "text/html; charset=utf-8"
        elif self.path == "/api/state":
            body = json.dumps(self.scope.snapshot()).encode()
            content_type = "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--history", type=float, default=DEFAULT_HISTORY_SECONDS)
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args()

    print("Searching local IPv4 /24 networks for SCPI devices...")
    devices = discover()
    if not devices:
        raise SystemExit("No Ethernet Analog Bridge devices found")
    for device in devices:
        print(f"Found {device.identity} at {device.address}")

    scope = Scope(devices, args.history, args.poll)
    threading.Thread(target=scope.poll, daemon=True).start()
    Handler.scope = scope
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Open http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scope.running = False
        server.server_close()
        for device in devices:
            device.close()


if __name__ == "__main__":
    main()
