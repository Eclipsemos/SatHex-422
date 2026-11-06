"""Protocol, configuration and time-bounded telemetry values (no hardware IO)."""
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import re
import threading
import time


@dataclass
class Config:
    port: str = "/dev/ttyS6"
    ethernet1: str = "auto"
    ethernet2: str = "auto"
    wifi: str = "auto"
    smi: str = "/usr/local/houmo-sdk/tools/hm_smi/hm_smi"
    card: int = 0
    soc_temp_path: str = ""
    soc_divisor: int = 10
    current_path: str = "/sys/class/hwmon/hwmon8/curr1_input"
    current_divisor: int = 1000
    wifi_timeout: int = 45
    cache_ttl_ms: int = 5000
    sample_interval_ms: int = 1000
    sensor_timeout_ms: int = 2000

    @classmethod
    def read(cls, path):
        values = {}
        defaults = cls()
        allowed = {f.name for f in fields(cls)}
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or key not in allowed or key in values:
                raise ValueError("invalid or duplicate configuration key")
            values[key] = int(value) if isinstance(getattr(defaults, key), int) else value
        c = cls(**values)
        for key, low, high in (("card", 0, 255), ("soc_divisor", 1, 1000000000),
                               ("current_divisor", 1, 1000000000), ("wifi_timeout", 5, 300),
                               ("cache_ttl_ms", 100, 600000), ("sample_interval_ms", 100, 60000),
                               ("sensor_timeout_ms", 100, 10000)):
            if not low <= getattr(c, key) <= high:
                raise ValueError("configuration out of range: " + key)
        for key in ("port", "smi", "soc_temp_path", "current_path"):
            value = getattr(c, key)
            if (value and not value.startswith("/")) or any(ord(x) < 32 for x in value):
                raise ValueError("invalid path: " + key)
        if not c.port or not c.smi:
            raise ValueError("port and smi required")
        for key in ("ethernet1", "ethernet2", "wifi"):
            if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", getattr(c, key)):
                raise ValueError("invalid interface")
        if (c.ethernet1, c.ethernet2, c.wifi) == ("eth0", "eth1", "wlan0"):
            c.ethernet1 = c.ethernet2 = c.wifi = "auto"
        return c


class Framer:
    def __init__(self):
        self.buffer = bytearray()
        self.overflow = False

    def feed(self, data):
        """None represents one overlong frame. Empty CR/LF sequences are ignored."""
        for byte in data:
            if byte in (10, 13):
                if self.overflow:
                    yield None
                elif self.buffer:
                    yield bytes(self.buffer)
                self.buffer.clear()
                self.overflow = False
            elif not self.overflow:
                if len(self.buffer) == 512:
                    self.buffer.clear()
                    self.overflow = True
                else:
                    self.buffer.append(byte)


def escape(value):
    return value.replace("\\", "\\\\").replace(",", "\\,")


def wifi_arguments(value):
    parts = [""]
    escaped = False
    for ch in value:
        if escaped:
            if ch not in "\\,":
                raise ValueError("bad escape")
            parts[-1] += ch
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ",":
            parts.append("")
        else:
            parts[-1] += ch
    if escaped or len(parts) != 2:
        raise ValueError("bad arguments")
    ssid, password = parts
    if not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError("invalid ssid")
    if password and not (8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password)
                         or re.fullmatch(r"[a-fA-F0-9]{64}", password)):
        raise ValueError("invalid password")
    return ssid, password


def parse(frame):
    if frame is None:
        return "ERROR", None
    try:
        line = frame.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "ERROR", None
    for command in ("STA", "IP1", "IP2", "WIFI"):
        if line in ("AT+" + command, "AT+" + command + "?"):
            return command, None
    if line.startswith("AT+WIFI="):
        try:
            return "SET_WIFI", wifi_arguments(line[len("AT+WIFI="):])
        except ValueError:
            return "WIFI_ERR", None
    return "ERROR", None


def scaled(value, divisor):
    try:
        text = value.strip()
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
            return None
        number = Decimal(text) / Decimal(divisor)
        if not number.is_finite() or abs(number) > 2147483647:
            return None
        return int(number.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def parse_smi(text, card):
    """Highest selected-card temperature; tool's power value, never a card sum."""
    selected = False
    in_temperature = False
    temperatures, powers = [], []
    for line in text.splitlines():
        device = re.search(r"device\s*(\d+)\s+detail\s+infos", line, re.I)
        if device:
            selected = int(device[1]) == card
            in_temperature = False
        if selected and re.match(r"\s*Temperature\s*:", line, re.I):
            in_temperature = True
            continue
        if in_temperature:
            match = re.fullmatch(r"\s*\w+\s*:\s*([+-]?\d+(?:\.\d+)?)\s*C\s*", line)
            if match:
                temperatures.append(scaled(match[1], Decimal("0.01")))
            elif line.strip():
                in_temperature = False
        power = re.fullmatch(r"\s*(?:Board_Power|Total_Power)\s*:\s*(\d+(?:\.\d+)?)\s*W\s*", line)
        if power:
            powers.append(scaled(power[1], Decimal("0.001")))
    temperature = max(temperatures) if temperatures and None not in temperatures else None
    power = powers[0] if powers and None not in powers and len(set(powers)) == 1 else None
    return temperature, power


class Cache:
    def __init__(self, ttl_ms, clock=time.monotonic):
        self.ttl = ttl_ms / 1000
        self.clock = clock
        self.lock = threading.Lock()
        self.values = [None] * 4

    def update(self, index, value):
        if value is not None:
            with self.lock:
                self.values[index] = (value, self.clock())

    def reply(self):
        with self.lock:
            now = self.clock()
            values = [str(v[0]) if v is not None and 0 <= now - v[1] < self.ttl else "-"
                      for v in self.values]
        return "+STA:" + ",".join(values)
