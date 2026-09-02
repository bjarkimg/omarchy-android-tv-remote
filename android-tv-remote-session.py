#!/usr/bin/env python3

"""Backend for the Omarchy Android TV Remote plugin."""

from __future__ import annotations

import argparse
import asyncio
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
AVAHI_TIMEOUT = 5.0

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


def emit(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, separators=(",", ":")), flush=True)


def get_secure_settings_dir() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    settings_dir = state_home / "omarchy" / "settings"
    os.makedirs(settings_dir, mode=0o700, exist_ok=True)
    return settings_dir


def safe_load_state(filename: str = "android-tv-remote.json") -> dict[str, Any]:
    """Read state using descriptor-bound directory access, no-follow, and size checks."""
    settings_dir = get_secure_settings_dir()
    dir_fd = -1
    fd = -1
    try:
        dir_fd = os.open(str(settings_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            return {"selected": "", "devices": {}}

        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return {"selected": "", "devices": {}}
        if st.st_uid != os.getuid():
            return {"selected": "", "devices": {}}
        if st.st_size > MAX_STATE_SIZE:
            return {"selected": "", "devices": {}}

        content = os.read(fd, MAX_STATE_SIZE).decode("utf-8", errors="replace")
        data = json.loads(content)
        if isinstance(data, dict) and isinstance(data.get("devices"), dict):
            return data
        return {"selected": "", "devices": {}}
    except Exception:
        return {"selected": "", "devices": {}}
    finally:
        if fd >= 0:
            os.close(fd)
        if dir_fd >= 0:
            os.close(dir_fd)


def safe_save_state(payload: dict[str, Any], filename: str = "android-tv-remote.json") -> None:
    """Atomically write state with randomized 0600 temp file and directory fsync."""
    settings_dir = get_secure_settings_dir()
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_STATE_SIZE:
        raise ValueError("State payload exceeds maximum size")

    tmp_name = f"{filename}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    tmp_path = settings_dir / tmp_name
    target_path = settings_dir / filename
    dir_fd = -1
    tmp_fd = -1
    try:
        dir_fd = os.open(str(settings_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
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
        os.replace(tmp_path, target_path)
    finally:
        if tmp_fd >= 0:
            os.close(tmp_fd)
        if dir_fd >= 0:
            os.close(dir_fd)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


class RemoteSession:
    def __init__(self, host: str, name: str) -> None:
        self.default_host = host.strip()[:MAX_STRING_LEN]
        self.default_name = (name.strip() or "Android TV")[:MAX_STRING_LEN]
        self.host = self.default_host
        self.name = self.default_name
        self.identifier = ""
        self.loop = asyncio.get_running_loop()
        self.remote: AndroidTVRemote | None = None
        self.connected = False
        self.pairing: AndroidTVRemote | None = None
        self.pairing_identifier = ""
        self.discovered: dict[str, dict[str, Any]] = {}

        data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        self.cert_dir = data_home / "io.github.bjarkimg.android-tv-remote"
        os.makedirs(self.cert_dir, mode=0o700, exist_ok=True)
        self.certfile = str(self.cert_dir / "cert.pem")
        self.keyfile = str(self.cert_dir / "key.pem")
        self.state: dict[str, Any] = {"selected": "", "devices": {}}

    def load_state(self) -> None:
        self.state = safe_load_state()
        selected = str(self.state.get("selected", ""))[:MAX_STRING_LEN]
        device = self.state.get("devices", {}).get(selected, {})
        if isinstance(device, dict):
            self.host = str(device.get("host") or device.get("address") or self.host)[:MAX_STRING_LEN]
            self.name = str(device.get("name") or self.name)[:MAX_STRING_LEN]
            self.identifier = selected

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
        os.makedirs(self.cert_dir, mode=0o700, exist_ok=True)
        remote = self.make_remote(self.host or "127.0.0.1")
        await remote.async_generate_cert_if_missing()

    async def start(self) -> None:
        await self.ensure_cert()
        self.load_state()
        if not self.host:
            raise RuntimeError("no device selected — open Devices to scan or add a host")
        await self.connect(self.host, self.name)

    async def resolve_host(self, host: str) -> str:
        host = host.strip()[:MAX_STRING_LEN]
        if not host:
            raise RuntimeError("missing host")
        if ":" in host and host.count(":") == 1:
            host = host.split(":", 1)[0]
        try:
            socket.inet_aton(host)
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
            return str(addresses[0][4][0])

    async def connect(self, host: str, name: str = "") -> None:
        address = await self.resolve_host(host)
        await self.close_connection()

        self.host = host[:MAX_STRING_LEN]
        self.name = (name or self.name or "Android TV")[:MAX_STRING_LEN]
        remote = self.make_remote(address)
        try:
            await asyncio.wait_for(remote.async_connect(), timeout=NETWORK_TIMEOUT)
        except CannotConnect as error:
            remote.disconnect()
            raise RuntimeError(f"could not reach {self.name} at {address}") from error
        except ConnectionClosed as error:
            remote.disconnect()
            raise RuntimeError(f"connection closed by {self.name}") from error
        except InvalidAuth as error:
            remote.disconnect()
            raise RuntimeError(f"{self.name} is not paired — open Devices to pair") from error

        self.remote = remote
        self.connected = True
        identifier = getattr(remote.device_info, "model", "") or getattr(remote.device_info, "name", "") or address
        self.identifier = identifier[:MAX_STRING_LEN]
        device = {
            "identifier": self.identifier,
            "name": self.name,
            "host": self.host,
            "address": address,
            "paired": True,
            "online": True,
        }
        if len(self.discovered) < MAX_DEVICES:
            self.discovered[self.identifier] = device
        self.remember_device(device)

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

    def remember_device(self, device: dict[str, Any]) -> None:
        identifier = str(device.get("identifier") or "")[:MAX_STRING_LEN]
        if not identifier:
            return
        devices = self.state.setdefault("devices", {})
        if len(devices) >= MAX_DEVICES and identifier not in devices:
            # Drop oldest entry to enforce bound
            devices.pop(next(iter(devices)), None)

        stored = {
            "identifier": identifier,
            "name": str(device.get("name") or "Android TV")[:MAX_STRING_LEN],
            "host": str(device.get("host") or device.get("address") or "")[:MAX_STRING_LEN],
            "address": str(device.get("address") or device.get("host") or "")[:MAX_STRING_LEN],
            "paired": bool(device.get("paired")),
        }
        devices[identifier] = stored
        if stored["paired"]:
            self.state["selected"] = identifier
        self.save_state()

    async def remove_device(self, identifier: str) -> None:
        identifier = str(identifier or "").strip()[:MAX_STRING_LEN]
        if not identifier:
            raise RuntimeError("no device selected")

        stored = self.state.setdefault("devices", {}).pop(identifier, None)
        self.discovered.pop(identifier, None)
        if self.state.get("selected") == identifier:
            remaining = [
                device_id
                for device_id, device in self.state.get("devices", {}).items()
                if isinstance(device, dict) and device.get("paired")
            ]
            self.state["selected"] = remaining[0] if remaining else ""
        self.save_state()

        disconnected = identifier == self.identifier
        if disconnected:
            await self.close_connection()
            self.host = ""
            self.name = self.default_name
            self.identifier = ""

        emit(
            "removed",
            identifier=identifier,
            name=str((stored or {}).get("name") or "")[:MAX_STRING_LEN],
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

    async def avahi_records(self) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for service in MDNS_TYPES:
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    "avahi-browse",
                    "-rtpk",
                    service,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=AVAHI_TIMEOUT)
            except (FileNotFoundError, asyncio.TimeoutError):
                if process is not None:
                    try:
                        process.terminate()
                        await asyncio.sleep(0.05)
                        process.kill()
                    except ProcessLookupError:
                        pass
                continue

            for raw_line in stdout.decode(errors="replace").splitlines()[:MAX_DEVICES]:
                fields = raw_line.split(";")
                if len(fields) < 9 or fields[0] != "=" or fields[2] != "IPv4":
                    continue
                address = fields[7][:MAX_STRING_LEN]
                name = self.decode_avahi(fields[3])[:MAX_STRING_LEN]
                host = self.decode_avahi(fields[6])[:MAX_STRING_LEN]
                identifier = address
                if len(fields) >= 10:
                    for item in shlex.split(fields[9]):
                        key, separator, value = item.partition("=")
                        if separator and key.lower() in {"bt", "mac"}:
                            identifier = value[:MAX_STRING_LEN]
                            break
                if len(records) < MAX_DEVICES:
                    records[identifier] = {
                        "identifier": identifier,
                        "name": name,
                        "host": host,
                        "address": address,
                        "paired": identifier in self.state.get("devices", {})
                        and bool(self.state["devices"][identifier].get("paired")),
                        "online": True,
                    }
        return records

    async def probe_host(self, host: str, name: str = "") -> dict[str, Any]:
        address = await self.resolve_host(host)
        remote = self.make_remote(address)
        await remote.async_generate_cert_if_missing()
        try:
            cert_name, mac = await asyncio.wait_for(remote.async_get_name_and_mac(), timeout=NETWORK_TIMEOUT)
        except CannotConnect as error:
            raise RuntimeError(f"could not reach {name or host} at {address}") from error
        finally:
            remote.disconnect()
        identifier = (mac or address)[:MAX_STRING_LEN]
        stored = self.state.get("devices", {}).get(identifier, {})
        return {
            "identifier": identifier,
            "name": (name or cert_name or "Android TV")[:MAX_STRING_LEN],
            "host": address,
            "address": address,
            "paired": bool(isinstance(stored, dict) and stored.get("paired")),
            "online": True,
        }

    async def scan_devices(self) -> list[dict[str, Any]]:
        visible: dict[str, dict[str, Any]] = {}
        records = await self.avahi_records()
        for identifier, device in records.items():
            if len(self.discovered) < MAX_DEVICES:
                self.discovered[identifier] = device
            visible[identifier] = device
            if device.get("paired"):
                self.remember_device(device)

        for identifier, stored in self.state.get("devices", {}).items():
            if identifier not in visible and isinstance(stored, dict):
                visible[identifier] = {
                    "identifier": identifier,
                    "name": str(stored.get("name") or "Android TV")[:MAX_STRING_LEN],
                    "host": str(stored.get("host") or stored.get("address") or "")[:MAX_STRING_LEN],
                    "address": str(stored.get("address") or stored.get("host") or "")[:MAX_STRING_LEN],
                    "paired": bool(stored.get("paired")),
                    "online": False,
                }
            if len(visible) >= MAX_DEVICES:
                break

        return list(
            sorted(
                visible.values(),
                key=lambda item: (not item.get("paired"), not item.get("online"), str(item.get("name") or "")),
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

    async def switch_device(self, identifier: str) -> None:
        identifier = identifier[:MAX_STRING_LEN]
        if identifier == self.identifier and self.connected:
            emit("switched", **self.active_device())
            return

        device = self.discovered.get(identifier) or self.state.get("devices", {}).get(identifier)
        if not isinstance(device, dict):
            raise RuntimeError("that device is no longer available")
        if not device.get("paired"):
            raise RuntimeError(f"{device.get('name') or 'Android TV'} is not paired")

        await self.connect(
            str(device.get("host") or device.get("address") or ""),
            str(device.get("name") or ""),
        )
        emit("switched", **self.active_device())

    async def start_pairing(self, identifier: str) -> None:
        await self.close_pairing()
        identifier = identifier[:MAX_STRING_LEN]
        device = self.discovered.get(identifier) or self.state.get("devices", {}).get(identifier)
        if not isinstance(device, dict):
            raise RuntimeError("that device is no longer available")

        host = str(device.get("host") or device.get("address") or "")
        address = await self.resolve_host(host)
        remote = self.make_remote(address)
        await remote.async_generate_cert_if_missing()
        try:
            await asyncio.wait_for(remote.async_start_pairing(), timeout=NETWORK_TIMEOUT)
        except CannotConnect as error:
            remote.disconnect()
            raise RuntimeError(f"could not start pairing with {device.get('name') or host}") from error

        self.pairing = remote
        self.pairing_identifier = identifier
        emit(
            "pairing-pin",
            identifier=identifier,
            name=str(device.get("name") or "Android TV")[:MAX_STRING_LEN],
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

        device = self.discovered.get(identifier) or self.state.get("devices", {}).get(identifier) or {}
        await self.connect(host, str(device.get("name") or ""))
        emit("paired", **self.active_device())

    async def add_host(self, host: str, name: str = "") -> None:
        device = await self.probe_host(host, name)
        identifier = str(device["identifier"])
        if len(self.discovered) < MAX_DEVICES:
            self.discovered[identifier] = device
        self.remember_device(device)
        if device["paired"]:
            await self.connect(device["host"], device["name"])
            emit("switched", **self.active_device())
            return
        await self.start_pairing(identifier)

    async def handle_request(self, request: dict[str, Any]) -> None:
        operation = str(request.get("op", ""))[:MAX_STRING_LEN]
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

    async def dispatch(self, action: str) -> str:
        if not self.connected or self.remote is None:
            if not self.host:
                raise RuntimeError("no device selected — open Devices to scan or add a host")
            await self.connect(self.host, self.name)

        assert self.remote is not None
        try:
            # Verified power-off: only send command if currently awake or unknown
            if action == "power-off":
                if self.remote.is_on is False:
                    return "asleep"
                # If awake or unknown, send SLEEP command to power off safely
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
            await self.connect(self.host, self.name)
            return await self.dispatch(action)

    async def run(self) -> None:
        try:
            await self.start()
            emit("ready", status=self.power_status(), **self.active_device())
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
