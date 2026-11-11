"""NetworkManager D-Bus integration; secrets never enter argv or logs."""
import ipaddress
import time
import uuid
from .core import escape

NM = "org.freedesktop.NetworkManager"
ROOT = "/org/freedesktop/NetworkManager"
PROPS = "org.freedesktop.DBus.Properties"
DEVICE = NM + ".Device"
WIRELESS = DEVICE + ".Wireless"
ACTIVE = NM + ".Connection.Active"
CONNECTION = NM + ".Settings.Connection"


class NMClient:
    def __init__(self, stop, timeout=45):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.stop = stop
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.cleaning = False

    def call(self, path, interface, method, signature=None, args=(), raw=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or (self.stop.is_set() and not self.cleaning):
            raise TimeoutError("operation cancelled or expired")
        result = self.bus.call_sync(NM, path, interface, method,
                                   self.GLib.Variant(signature, args) if signature else None,
                                   None, self.Gio.DBusCallFlags.NONE,
                                   max(1, min(3000, int(remaining * 1000))), None)
        return result if raw else result.unpack()

    def get(self, path, interface, prop):
        return self.call(path, PROPS, "Get", "(ss)", (interface, prop))[0]

    def radio(self, enabled):
        self.call(ROOT, PROPS, "Set", "(ssv)", (NM, "WirelessEnabled", self.GLib.Variant("b", enabled)))

    def device(self, name):
        if not name:
            raise ValueError("wireless device missing")
        return self.call(ROOT, NM, "GetDeviceByIpIface", "(s)", (name,))[0]

    def snapshot(self, device):
        if self.get(device, DEVICE, "State") != 100:
            raise ValueError("not activated")
        ap = self.get(device, WIRELESS, "ActiveAccessPoint")
        if ap == "/":
            raise ValueError("missing access point")
        raw = self.get(ap, NM + ".AccessPoint", "Ssid")
        ssid = bytes(raw).decode("utf-8", errors="strict")
        if not ssid or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
            raise ValueError("ssid cannot be represented")
        config = self.get(device, DEVICE, "Ip4Config")
        addresses = self.get(config, NM + ".IP4Config", "AddressData")
        for record in addresses:
            address = ipaddress.IPv4Address(record["address"])
            if not (address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast):
                mask = str(ipaddress.IPv4Network((str(address), record["prefix"]), strict=False).netmask)
                # Do not report a stale SSID paired with an address from a new activation.
                if self.get(device, DEVICE, "State") != 100 or self.get(device, WIRELESS, "ActiveAccessPoint") != ap:
                    raise ValueError("connection changed during query")
                return ssid, str(address), mask
        raise ValueError("no usable IPv4")

    def query(self, name):
        ssid, address, mask = self.snapshot(self.device(name))
        return "+WIFI:" + ",".join((escape(ssid), address, mask))

    def pause(self, seconds=.15):
        if self.stop.wait(seconds) or time.monotonic() >= self.deadline:
            raise TimeoutError("operation cancelled or expired")

    def connect(self, name, ssid, password):
        device = self.device(name)
        if self.get(device, DEVICE, "DeviceType") != 2:
            raise ValueError("not wireless")
        old_radio = self.get(ROOT, NM, "WirelessEnabled")
        # The off/on procedure affects every Wi-Fi device, so checkpoint them all.
        devices = [d for d in self.call(ROOT, NM, "GetDevices")[0]
                   if self.get(d, DEVICE, "DeviceType") == 2]
        checkpoint = self.call(ROOT, NM, "CheckpointCreate", "(aouu)",
                               (devices, self.timeout + 15, 0))[0]
        connection = None
        committed = False
        try:
            self.radio(False)
            while self.get(ROOT, NM, "WirelessEnabled") or self.get(device, DEVICE, "State") >= 40:
                self.pause()
            self.radio(True)
            while self.get(device, DEVICE, "State") < 30:
                self.pause()
            variant = self.GLib.Variant
            settings = {
                "connection": {"id": variant("s", "gs422-" + str(uuid.uuid4())),
                               "uuid": variant("s", str(uuid.uuid4())),
                               "type": variant("s", "802-11-wireless"),
                               "interface-name": variant("s", name),
                               "autoconnect": variant("b", True)},
                "802-11-wireless": {"ssid": variant("ay", ssid.encode()), "mode": variant("s", "infrastructure")},
                "ipv4": {"method": variant("s", "auto")},
                "ipv6": {"method": variant("s", "auto")},
            }
            if password:
                settings["802-11-wireless-security"] = {"key-mgmt": variant("s", "wpa-psk"),
                                                       "psk": variant("s", password)}
            connection, active, _ = self.call(ROOT, NM, "AddAndActivateConnection2", "(a{sa{sv}}ooa{sv})",
                                              (settings, device, "/", {"persist": variant("s", "volatile")}))
            settings.clear()
            password = None
            while True:
                state = self.get(active, ACTIVE, "State")
                if state == 4:
                    raise ValueError("activation failed")
                if state == 2 and self.get(device, DEVICE, "ActiveConnection") == active:
                    try:
                        current, _, _ = self.snapshot(device)
                        if current == ssid:
                            break
                    except (ValueError, OSError):
                        pass
                self.pause()
            self.call(connection, CONNECTION, "Save")
            self.call(ROOT, NM, "CheckpointDestroy", "(o)", (checkpoint,))
            committed = True
            return "+WIFI:OK"
        finally:
            # Never log D-Bus exceptions: they may contain input or secrets.
            if not committed:
                self.cleaning = True
                self.deadline = time.monotonic() + 10
                try:
                    self.radio(old_radio)
                    self.call(ROOT, NM, "CheckpointRollback", "(o)", (checkpoint,))
                except Exception:
                    # The checkpoint's automatic timer remains the final recovery guard.
                    pass
                if connection:
                    try:
                        self.call(connection, CONNECTION, "Delete")
                    except Exception:
                        pass
