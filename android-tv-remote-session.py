#!/usr/bin/env python3

"""Backend for the Omarchy Android TV Remote plugin."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import secrets
import shlex
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Any

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth


MAX_STATE_SIZE = 65536
MAX_STDIN_LINE = 65536
MAX_DEVICES = 32
MAX_STRING_LEN = 64
NETWORK_TIMEOUT = 8.0
AVAHI_TIMEOUT = 10.0
MDNS_CACHE_TTL = 3.0
AVAHI_BROWSE = "/usr/bin/avahi-browse"
AVAHI_BROWSE_FALLBACK = "/bin/avahi-browse"

CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

REMOTE_KEYS = {
    "up": "DPAD_UP",
    "down": "DPAD_DOWN",
    "left": "DPAD_LEFT",
    "right": "DPAD_RIGHT",
    "select": "DPAD_CENTER",
    "back": "BACK",
    "home": "HOME",
    "menu": "MENU",
    "play-pause": "MEDIA_PLAY_PAUSE",
    "previous": "MEDIA_PREVIOUS",
    "next": "MEDIA_NEXT",
    "rewind": "MEDIA_REWIND",
    "ff": "MEDIA_FAST_FORWARD",
    "volume-down": "VOLUME_DOWN",
    "volume-up": "VOLUME_UP",
    "mute": "VOLUME_MUTE",
    "wake": "WAKEUP",
    "sleep": "SLEEP",
    "power": "POWER",
    "toggle-power": "POWER",
}

APP_LINKS = {
    "app-plex": "com.plexapp.android",
    "app-netflix": "com.netflix.ninja",
    "app-youtube": "com.google.android.youtube.tv",
}

MDNS_TYPES = ("_androidtvremote2._tcp", "_androidtvremote._tcp")
CLIENT_NAME = "Omarchy"


def sanitize_text(value: Any, max_len: int = MAX_STRING_LEN) -> str:
    cleaned = CONTROL_CHARS_RE.sub("", str(value or "")).strip()
    return cleaned[:max_len]


def sanitize_device(device: dict[str, Any]) -> dict[str, Any]:
    return {
        "identifier": sanitize_text(device.get("identifier")),
        "name": sanitize_text(device.get("name") or "Android TV"),
        "host": sanitize_text(device.get("host")),
        "address": sanitize_text(device.get("address")),
        "mac": sanitize_text(device.get("mac")),
        "paired": bool(device.get("paired")),
        "online": bool(device.get("online")),
    }


def emit(event: str, **values: Any) -> None:
    payload: dict[str, Any] = {"event": sanitize_text(event, 32)}
    for key, value in values.items():
        if isinstance(value, str):
            payload[key] = sanitize_text(value)
        elif isinstance(value, list):
            payload[key] = [
                sanitize_device(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            payload[key] = value
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def normalize_mac(value: str) -> str:
    hexes = re.sub(r"[^0-9A-Fa-f]", "", str(value or ""))
    if len(hexes) != 12:
        return ""
    hexes = hexes.upper()
    return ":".join(hexes[i : i + 2] for i in range(0, 12, 2))


def is_lan_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        return False
    return bool(
        addr.is_private
        and not addr.is_loopback
        and not addr.is_link_local
        and not addr.is_multicast
        and not addr.is_reserved
    )


def open_secure_settings_dir() -> int:
    """Open settings directory via descriptor-bound path walking."""
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    target_path = os.path.normpath(os.path.abspath(os.path.join(state_home, "omarchy", "settings")))
    parts = [part for part in target_path.split(os.sep) if part]
    if not parts:
        raise ValueError("Invalid settings directory path")

    uid = os.getuid()
    cur_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        user_seen = False
        for part in parts:
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=cur_fd,
                )
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=cur_fd)
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=cur_fd,
                )

            os.close(cur_fd)
            cur_fd = next_fd

            st = os.fstat(cur_fd)
            if not stat.S_ISDIR(st.st_mode):
                raise PermissionError(f"Path segment {part} is not a directory")

            if st.st_uid == uid:
                user_seen = True
            elif user_seen or st.st_uid != 0:
                raise PermissionError(f"Directory {part} owner mismatch: {st.st_uid} != {uid}")

        st = os.fstat(cur_fd)
        if st.st_uid != uid:
            raise PermissionError("Settings directory owner mismatch")
        try:
            os.fchmod(cur_fd, 0o700)
        except OSError:
            pass

        st = os.fstat(cur_fd)
        if (st.st_mode & 0o077) != 0:
            raise PermissionError("Settings directory permissions too permissive")

        return cur_fd
    except Exception:
        if cur_fd >= 0:
            os.close(cur_fd)
        raise


def safe_load_state(filename: str = "android-tv-remote.json") -> dict[str, Any]:
    """Read state using descriptor-bound directory access, no-follow, and size checks."""
    dir_fd = -1
    fd = -1
    empty = {"selected": "", "devices": {}}
    try:
        dir_fd = open_secure_settings_dir()
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            return empty

        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return empty
        if st.st_uid != os.getuid():
            return empty
        if st.st_size > MAX_STATE_SIZE:
            return empty

        content = os.read(fd, MAX_STATE_SIZE).decode("utf-8", errors="replace")
        data = json.loads(content)
        if isinstance(data, dict) and isinstance(data.get("devices"), dict):
            return data
        return empty
    except Exception:
        return empty
    finally:
        if fd >= 0:
            os.close(fd)
        if dir_fd >= 0:
            os.close(dir_fd)


def safe_save_state(payload: dict[str, Any], filename: str = "android-tv-remote.json") -> None:
    """Atomically write state with randomized 0600 temp file and directory fsync."""
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_STATE_SIZE:
        raise ValueError("State payload exceeds maximum size")

    tmp_name = f".{filename}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    dir_fd = -1
    tmp_fd = -1
    try:
        dir_fd = open_secure_settings_dir()
        tmp_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        st = os.fstat(tmp_fd)
        if st.st_uid != os.getuid():
            raise PermissionError("Directory owner mismatch")
        os.write(tmp_fd, encoded)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = -1
        os.replace(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
        tmp_name = ""
    finally:
        if tmp_fd >= 0:
            os.close(tmp_fd)
        if dir_fd >= 0:
            if tmp_name:
                try:
                    os.unlink(tmp_name, dir_fd=dir_fd)
                except OSError:
                    pass
            os.close(dir_fd)


class RemoteSession:
    def __init__(self, host: str, name: str) -> None:
        self.default_host = sanitize_text(host)
        self.default_name = sanitize_text(name) or "Android TV"
        self.host = self.default_host
        self.name = self.default_name
        self.identifier = ""
        self.loop = asyncio.get_running_loop()
        self.remote: AndroidTVRemote | None = None
        self.connected = False
        self.pairing: AndroidTVRemote | None = None
        self.pairing_identifier = ""
        self.discovered: dict[str, dict[str, Any]] = {}
        self._mdns_cache: dict[str, dict[str, Any]] = {}
        self._mdns_at = 0.0

        data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        self.cert_dir = Path(data_home) / "io.github.bjarkimg.android-tv-remote"
        self.cert_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.certfile = str(self.cert_dir / "cert.pem")
        self.keyfile = str(self.cert_dir / "key.pem")
        self.state: dict[str, Any] = {"selected": "", "devices": {}}

    def load_state(self) -> None:
        self.state = safe_load_state()
        selected = sanitize_text(self.state.get("selected", ""))
        device = self.state.get("devices", {}).get(selected, {})
        if isinstance(device, dict) and selected:
            self.host = sanitize_text(
                device.get("host") or device.get("address") or self.host
            )
            self.name = sanitize_text(device.get("name") or self.name) or self.default_name
            self.identifier = (
                normalize_mac(selected)
                or normalize_mac(str(device.get("mac") or device.get("identifier") or ""))
                or selected
            )

        if not self.host and self.default_host:
            self.host = self.default_host
            self.name = self.default_name

    def save_state(self) -> None:
        safe_save_state(self.state)

    def make_remote(self, host: str) -> AndroidTVRemote:
        return AndroidTVRemote(
            client_name=CLIENT_NAME,
            certfile=self.certfile,
            keyfile=self.keyfile,
            host=host,
            loop=self.loop,
        )

    async def ensure_cert(self) -> None:
        self.cert_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        remote = self.make_remote(self.host or "127.0.0.1")
        await remote.async_generate_cert_if_missing()

    async def start(self) -> None:
        await self.ensure_cert()
        self.load_state()
        try:
            records = await self.avahi_records(force=True)
            self.migrate_devices(records)
            self.load_state()
        except Exception:
            pass

        if not self.host and not self.identifier:
            emit("ready", status="unknown", connected=False, **self.active_device())
            return

        await self.connect(self.host, self.name, identifier=self.identifier)
        emit("ready", status=self.power_status(), connected=True, **self.active_device())

    async def resolve_host(self, host: str) -> str:
        host = sanitize_text(host)
        if not host:
            raise RuntimeError("missing host")
        if ":" in host and host.count(":") == 1:
            host = host.split(":", 1)[0]
        try:
            socket.inet_aton(host)
            if not is_lan_ipv4(host):
                raise RuntimeError("host must be a private LAN address")
            return host
        except OSError:
            addresses = await asyncio.wait_for(
                self.loop.getaddrinfo(
                    host,
                    None,
                    family=socket.AF_INET,
                    type=socket.SOCK_STREAM,
                ),
                timeout=NETWORK_TIMEOUT,
            )
            if not addresses:
                raise RuntimeError(f"could not resolve {host}")
            address = str(addresses[0][4][0])
            if not is_lan_ipv4(address):
                raise RuntimeError(f"{host} resolved to a non-LAN address")
            return address

    async def lookup_address(
        self, identifier: str, host: str
    ) -> tuple[str, dict[str, Any]]:
        """Find the current LAN IP for a device, preferring MAC via mDNS."""
        mac = normalize_mac(identifier)
        records: dict[str, dict[str, Any]] = {}
        try:
            records = await self.avahi_records()
        except Exception:
            records = {}

        if mac and mac in records:
            rec = records[mac]
            return str(rec["address"]), rec

        hint = sanitize_text(host)
        if hint:
            for rec in records.values():
                if rec.get("address") == hint or rec.get("host") == hint:
                    return str(rec["address"]), rec

        if hint:
            address = await self.resolve_host(hint)
            for rec in records.values():
                if rec.get("address") == address:
                    return address, rec
            return address, {
                "identifier": mac or address,
                "mac": mac,
                "name": "",
                "host": hint,
                "address": address,
            }

        name = sanitize_text(self.name).lower()
        if name:
            matches = [
                rec
                for rec in records.values()
                if str(rec.get("name") or "").strip().lower() == name
            ]
            if len(matches) == 1:
                rec = matches[0]
                return str(rec["address"]), rec

        raise RuntimeError("device is not on the network — open Devices to scan")

    async def connect(self, host: str, name: str = "", identifier: str = "") -> None:
        address, meta = await self.lookup_address(identifier, host)
        await self.close_connection()

        display_name = (
            sanitize_text(name)
            or sanitize_text(meta.get("name"))
            or self.name
            or "Android TV"
        )
        remote = self.make_remote(address)
        try:
            await asyncio.wait_for(remote.async_connect(), timeout=NETWORK_TIMEOUT)
        except CannotConnect as error:
            remote.disconnect()
            raise RuntimeError(f"could not reach {display_name} at {address}") from error
        except ConnectionClosed as error:
            remote.disconnect()
            raise RuntimeError(f"connection closed by {display_name}") from error
        except InvalidAuth as error:
            remote.disconnect()
            raise RuntimeError(f"{display_name} is not paired — open Devices to pair") from error

        identity = (
            normalize_mac(identifier)
            or normalize_mac(str(meta.get("mac") or meta.get("identifier") or ""))
        )
        if not identity:
            try:
                cert_name, cert_mac = await asyncio.wait_for(
                    remote.async_get_name_and_mac(),
                    timeout=NETWORK_TIMEOUT,
                )
                identity = normalize_mac(cert_mac)
                if cert_name:
                    display_name = sanitize_text(cert_name) or display_name
            except Exception:
                identity = ""
        if not identity:
            identity = address

        self.remote = remote
        self.connected = True
        self.host = address
        self.name = display_name
        self.identifier = identity
        device = {
            "identifier": identity,
            "mac": identity if normalize_mac(identity) else "",
            "name": display_name,
            "host": address,
            "address": address,
            "paired": True,
            "online": True,
        }
        if len(self.discovered) < MAX_DEVICES:
            self.discovered[identity] = device
        self.remember_device(device, select=True)

    async def close_connection(self) -> None:
        if self.remote is not None:
            self.remote.disconnect()
        self.remote = None
        self.connected = False

    async def close_pairing(self) -> None:
        if self.pairing is not None:
            self.pairing.disconnect()
        self.pairing = None
        self.pairing_identifier = ""

    async def close(self) -> None:
        await self.close_pairing()
        await self.close_connection()

    def remember_device(self, device: dict[str, Any], *, select: bool = False) -> None:
        identifier = (
            normalize_mac(str(device.get("mac") or device.get("identifier") or ""))
            or sanitize_text(device.get("identifier"))
        )
        if not identifier:
            return

        devices = self.state.setdefault("devices", {})
        address = sanitize_text(device.get("address") or device.get("host"))
        host = sanitize_text(device.get("host") or address)

        for old_id, existing in list(devices.items()):
            if old_id == identifier or not isinstance(existing, dict):
                continue
            existing_mac = normalize_mac(
                str(existing.get("mac") or existing.get("identifier") or old_id)
            )
            same_mac = bool(existing_mac and existing_mac == normalize_mac(identifier))
            same_addr = bool(
                address
                and (
                    existing.get("address") == address
                    or existing.get("host") == address
                    or existing.get("address") == host
                )
            )
            if same_mac or same_addr:
                devices.pop(old_id, None)
                if self.state.get("selected") == old_id:
                    self.state["selected"] = identifier
                if self.identifier == old_id:
                    self.identifier = identifier

        if len(devices) >= MAX_DEVICES and identifier not in devices:
            devices.pop(next(iter(devices)), None)

        previous = devices.get(identifier, {})
        stored = {
            "identifier": identifier,
            "mac": normalize_mac(identifier),
            "name": sanitize_text(device.get("name") or previous.get("name") or "Android TV")
            or "Android TV",
            "host": host or sanitize_text(previous.get("host")),
            "address": address or sanitize_text(previous.get("address")),
            "paired": bool(device.get("paired") if "paired" in device else previous.get("paired")),
        }
        devices[identifier] = stored
        if select and stored["paired"]:
            self.state["selected"] = identifier
        self.save_state()

    def migrate_devices(self, records: dict[str, dict[str, Any]]) -> None:
        devices = self.state.setdefault("devices", {})
        changed = False
        for old_id, stored in list(devices.items()):
            if not isinstance(stored, dict):
                continue
            mac = normalize_mac(old_id) or normalize_mac(
                str(stored.get("mac") or stored.get("identifier") or "")
            )
            match: dict[str, Any] | None = None
            if mac and mac in records:
                match = records[mac]
            else:
                address = sanitize_text(stored.get("address") or stored.get("host"))
                name = sanitize_text(stored.get("name")).lower()
                for rec in records.values():
                    if address and rec.get("address") == address:
                        match = rec
                        break
                    if name and sanitize_text(rec.get("name")).lower() == name:
                        match = rec
                        break

            new_id = ""
            if match:
                new_id = normalize_mac(str(match.get("identifier") or match.get("mac") or ""))
            if not new_id:
                new_id = mac
            if not new_id:
                continue

            stored["identifier"] = new_id
            stored["mac"] = new_id
            if match:
                stored["address"] = sanitize_text(match.get("address") or stored.get("address"))
                stored["host"] = sanitize_text(
                    match.get("address") or match.get("host") or stored.get("host")
                )
                if not stored.get("name"):
                    stored["name"] = sanitize_text(match.get("name") or "Android TV")
            if old_id != new_id:
                devices.pop(old_id, None)
                changed = True
            devices[new_id] = stored
            if self.state.get("selected") == old_id:
                self.state["selected"] = new_id
                changed = True
            if self.identifier == old_id:
                self.identifier = new_id
                self.host = sanitize_text(stored.get("host") or stored.get("address"))
                self.name = sanitize_text(stored.get("name") or self.name)

        if changed:
            self.save_state()

    async def remove_device(self, identifier: str) -> None:
        identifier = sanitize_text(identifier)
        mac = normalize_mac(identifier) or identifier
        if not mac:
            raise RuntimeError("no device selected")

        devices = self.state.setdefault("devices", {})
        stored = devices.pop(mac, None)
        if stored is None:
            stored = devices.pop(identifier, None)
            mac = identifier
        self.discovered.pop(mac, None)
        self.discovered.pop(identifier, None)
        if self.state.get("selected") in {mac, identifier}:
            remaining = [
                device_id
                for device_id, device in devices.items()
                if isinstance(device, dict) and device.get("paired")
            ]
            self.state["selected"] = remaining[0] if remaining else ""
        self.save_state()

        disconnected = self.identifier in {mac, identifier}
        if disconnected:
            await self.close_connection()
            self.host = ""
            self.name = self.default_name
            self.identifier = ""

        emit(
            "removed",
            identifier=mac,
            name=sanitize_text((stored or {}).get("name")),
            connected=self.connected,
        )
        emit("devices", devices=await self.scan_devices())

    @staticmethod
    def decode_avahi(value: str) -> str:
        return re.sub(
            r"\\(\d{3})",
            lambda match: chr(int(match.group(1), 10)),
            value,
        )

    def parse_avahi_line(self, raw_line: str, paired_ids: set[str]) -> dict[str, Any] | None:
        fields = raw_line.split(";")
        if len(fields) < 9 or fields[0] != "=" or fields[2] not in {"IPv4", "IPv6"}:
            return None
        address = sanitize_text(fields[7])
        if not is_lan_ipv4(address):
            return None
        name = sanitize_text(self.decode_avahi(fields[3]))
        host = sanitize_text(self.decode_avahi(fields[6]))
        identifier = ""
        if len(fields) >= 10:
            try:
                items = shlex.split(fields[9])
            except ValueError:
                items = [fields[9].strip().strip('"')]
            for item in items:
                key, separator, value = item.partition("=")
                if separator and key.lower() in {"bt", "mac"}:
                    identifier = normalize_mac(value)
                    break
        if not identifier:
            identifier = address
        return {
            "identifier": identifier,
            "mac": normalize_mac(identifier),
            "name": name or "Android TV",
            "host": host or address,
            "address": address,
            "paired": identifier in paired_ids,
            "online": True,
        }

    async def _stop_browse(self, process: asyncio.subprocess.Process) -> bytes:
        if process.returncode is not None:
            return b""
        try:
            process.terminate()
        except ProcessLookupError:
            return b""
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=1.0)
            return stdout or b""
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                process.kill()
            except ProcessLookupError:
                return b""
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=0.5)
                return stdout or b""
            except Exception:
                return b""

    async def _browse_mdns(self, service: str) -> bytes:
        executable = AVAHI_BROWSE if os.path.isfile(AVAHI_BROWSE) else AVAHI_BROWSE_FALLBACK
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "-frtpk",
                service,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=AVAHI_TIMEOUT)
                return stdout or b""
            except asyncio.TimeoutError:
                return await self._stop_browse(process)
        except (FileNotFoundError, PermissionError, OSError):
            if process is not None:
                await self._stop_browse(process)
            return b""

    async def avahi_records(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        if not force and self._mdns_cache and now - self._mdns_at < MDNS_CACHE_TTL:
            return self._mdns_cache

        paired_ids: set[str] = set()
        for key, stored in self.state.get("devices", {}).items():
            if not isinstance(stored, dict) or not stored.get("paired"):
                continue
            paired_ids.add(normalize_mac(key) or key)
            extra = normalize_mac(str(stored.get("mac") or stored.get("identifier") or ""))
            if extra:
                paired_ids.add(extra)

        records: dict[str, dict[str, Any]] = {}
        for service in MDNS_TYPES:
            blob = await self._browse_mdns(service)
            for raw_line in blob.decode(errors="replace").splitlines():
                parsed = self.parse_avahi_line(raw_line, paired_ids)
                if parsed is None:
                    continue
                records[parsed["identifier"]] = parsed
                if len(records) >= MAX_DEVICES:
                    break
            if records:
                break

        self._mdns_cache = records
        self._mdns_at = now
        return records

    async def probe_host(self, host: str, name: str = "") -> dict[str, Any]:
        address = await self.resolve_host(host)
        remote = self.make_remote(address)
        await remote.async_generate_cert_if_missing()
        try:
            cert_name, mac = await asyncio.wait_for(
                remote.async_get_name_and_mac(),
                timeout=NETWORK_TIMEOUT,
            )
        except CannotConnect as error:
            raise RuntimeError(f"could not reach {name or host} at {address}") from error
        finally:
            remote.disconnect()
        identifier = normalize_mac(mac) or address
        stored = self.state.get("devices", {}).get(identifier, {})
        return {
            "identifier": identifier,
            "mac": normalize_mac(identifier),
            "name": sanitize_text(name or cert_name or "Android TV") or "Android TV",
            "host": address,
            "address": address,
            "paired": bool(isinstance(stored, dict) and stored.get("paired")),
            "online": True,
        }

    async def scan_devices(self) -> list[dict[str, Any]]:
        visible: dict[str, dict[str, Any]] = {}
        records = await self.avahi_records(force=True)
        self.migrate_devices(records)
        self.load_state()

        for identifier, device in records.items():
            stored = self.state.get("devices", {}).get(identifier, {})
            if isinstance(stored, dict) and stored.get("paired"):
                device = {**device, "paired": True, "name": stored.get("name") or device.get("name")}
                self.remember_device(device, select=False)
            if len(self.discovered) < MAX_DEVICES:
                self.discovered[identifier] = device
            visible[identifier] = device
            if len(visible) >= MAX_DEVICES:
                break

        for identifier, stored in self.state.get("devices", {}).items():
            if identifier in visible or not isinstance(stored, dict):
                continue
            visible[identifier] = {
                "identifier": identifier,
                "mac": normalize_mac(identifier),
                "name": sanitize_text(stored.get("name") or "Android TV") or "Android TV",
                "host": sanitize_text(stored.get("host") or stored.get("address")),
                "address": sanitize_text(stored.get("address") or stored.get("host")),
                "paired": bool(stored.get("paired")),
                "online": False,
            }
            if len(visible) >= MAX_DEVICES:
                break

        return list(
            sorted(
                visible.values(),
                key=lambda item: (
                    not item.get("paired"),
                    not item.get("online"),
                    str(item.get("name") or ""),
                ),
            )
        )

    def active_device(self) -> dict[str, str]:
        return {
            "identifier": self.identifier,
            "name": self.name,
            "host": self.host,
        }

    def power_status(self) -> str:
        if self.remote is None or self.remote.is_on is None:
            return "unknown"
        return "awake" if self.remote.is_on else "asleep"

    def device_from_id(self, identifier: str) -> dict[str, Any] | None:
        identifier = sanitize_text(identifier)
        mac = normalize_mac(identifier) or identifier
        device = self.discovered.get(mac) or self.discovered.get(identifier)
        if isinstance(device, dict):
            return device
        stored = self.state.get("devices", {}).get(mac) or self.state.get("devices", {}).get(
            identifier
        )
        if isinstance(stored, dict):
            return stored
        return None

    async def switch_device(self, identifier: str) -> None:
        identifier = sanitize_text(identifier)
        mac = normalize_mac(identifier) or identifier
        if mac == self.identifier and self.connected:
            emit("switched", connected=True, **self.active_device())
            return

        device = self.device_from_id(mac)
        if not isinstance(device, dict):
            raise RuntimeError("that device is no longer available")
        if not device.get("paired"):
            raise RuntimeError(f"{device.get('name') or 'Android TV'} is not paired")

        await self.connect(
            str(device.get("host") or device.get("address") or ""),
            str(device.get("name") or ""),
            identifier=mac,
        )
        emit("switched", connected=True, **self.active_device())

    async def start_pairing(self, identifier: str) -> None:
        await self.close_pairing()
        identifier = sanitize_text(identifier)
        mac = normalize_mac(identifier) or identifier
        device = self.device_from_id(mac)
        if not isinstance(device, dict):
            raise RuntimeError("that device is no longer available")

        host = str(device.get("host") or device.get("address") or "")
        address, meta = await self.lookup_address(mac, host)
        remote = self.make_remote(address)
        await remote.async_generate_cert_if_missing()
        try:
            await asyncio.wait_for(remote.async_start_pairing(), timeout=NETWORK_TIMEOUT)
        except CannotConnect as error:
            remote.disconnect()
            raise RuntimeError(
                f"could not start pairing with {device.get('name') or host}"
            ) from error

        self.pairing = remote
        self.pairing_identifier = normalize_mac(str(meta.get("identifier") or mac)) or mac
        emit(
            "pairing-pin",
            identifier=self.pairing_identifier,
            name=sanitize_text(device.get("name") or "Android TV") or "Android TV",
        )

    async def finish_pairing(self, pin: str) -> None:
        if self.pairing is None:
            raise RuntimeError("pairing has not been started")
        code = pin.strip().upper()
        if not re.fullmatch(r"[0-9A-F]{6}", code):
            raise ValueError("enter the six-character code shown on the TV")

        identifier = self.pairing_identifier
        host = self.pairing.host
        try:
            await asyncio.wait_for(self.pairing.async_finish_pairing(code), timeout=NETWORK_TIMEOUT)
        except InvalidAuth as error:
            raise RuntimeError("the TV did not accept that code") from error
        finally:
            await self.close_pairing()

        device = self.device_from_id(identifier) or {}
        await self.connect(str(host), str(device.get("name") or ""), identifier=identifier)
        emit("paired", connected=True, **self.active_device())

    async def add_host(self, host: str, name: str = "") -> None:
        device = await self.probe_host(host, name)
        identifier = str(device["identifier"])
        if len(self.discovered) < MAX_DEVICES:
            self.discovered[identifier] = device
        self.remember_device(device, select=False)
        if device["paired"]:
            await self.connect(device["host"], device["name"], identifier=identifier)
            emit("switched", connected=True, **self.active_device())
            return
        await self.start_pairing(identifier)

    async def handle_request(self, request: dict[str, Any]) -> None:
        operation = sanitize_text(request.get("op", ""))
        if operation == "discover":
            emit("devices", devices=await self.scan_devices())
            return
        if operation == "switch":
            await self.switch_device(str(request.get("identifier", "")))
            return
        if operation == "pair-start":
            await self.start_pairing(str(request.get("identifier", "")))
            return
        if operation == "pair-finish":
            await self.finish_pairing(str(request.get("pin", "")))
            return
        if operation == "pair-cancel":
            await self.close_pairing()
            emit("pairing-cancelled")
            return
        if operation == "add":
            await self.add_host(str(request.get("host", "")), str(request.get("name", "")))
            return
        if operation == "remove":
            await self.remove_device(str(request.get("identifier", "")))
            return
        raise ValueError(f"unknown operation: {operation}")

    async def dispatch(self, action: str, *, retry: bool = True) -> str:
        if not self.connected or self.remote is None:
            if not self.host and not self.identifier:
                raise RuntimeError("no device selected — open Devices to scan or add a host")
            await self.connect(self.host, self.name, identifier=self.identifier)

        assert self.remote is not None
        try:
            if action == "power-off":
                if self.remote.is_on is False:
                    return "asleep"
                self.remote.send_key_command("SLEEP")
                await asyncio.sleep(0.05)
                return "asleep"
            if action == "wake":
                self.remote.send_key_command("WAKEUP")
                await asyncio.sleep(0.05)
                return "awake"
            if action in REMOTE_KEYS:
                self.remote.send_key_command(REMOTE_KEYS[action])
                await asyncio.sleep(0.05)
                return ""
            if action in APP_LINKS:
                self.remote.send_launch_app_command(APP_LINKS[action])
                await asyncio.sleep(0.05)
                return ""
            if action == "status":
                return self.power_status()
            raise ValueError(f"unknown action: {action[:32]}")
        except ConnectionClosed:
            self.connected = False
            if not retry:
                raise
            await self.connect(self.host, self.name, identifier=self.identifier)
            return await self.dispatch(action, retry=False)

    async def run(self) -> None:
        try:
            await self.start()
        except Exception as error:
            emit("error", action="connect", message=str(error), connected=False)

        while True:
            line = await asyncio.to_thread(sys.stdin.readline, MAX_STDIN_LINE)
            if not line:
                break

            raw_command = line.strip()
            if not raw_command:
                continue
            if raw_command == "quit":
                break

            started = time.monotonic()
            try:
                if raw_command.startswith("{"):
                    await self.handle_request(json.loads(raw_command))
                    continue

                result = await self.dispatch(raw_command)
                emit(
                    "result",
                    action=raw_command[:32],
                    result=result,
                    status=self.power_status(),
                    elapsedMs=round((time.monotonic() - started) * 1000, 1),
                    connected=self.connected,
                    **self.active_device(),
                )
            except Exception as error:
                emit(
                    "error",
                    action=raw_command[:24],
                    message=str(error),
                    connected=self.connected,
                    status=self.power_status(),
                )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="", help="Android TV hostname or IP address")
    parser.add_argument("--name", default="Android TV", help="display name for the device")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_arguments()
    session = RemoteSession(args.host, args.name)
    try:
        await session.run()
    finally:
        await session.close()
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
