#!/usr/bin/env python3

"""Backend for the Omarchy NVIDIA SHIELD Remote plugin."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import socket
import sys
import time
from pathlib import Path
from typing import Any

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth


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
    "wake": "WAKEUP",
    "sleep": "SLEEP",
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


class RemoteSession:
    def __init__(self, host: str, name: str) -> None:
        self.default_host = host.strip()
        self.default_name = name.strip() or "SHIELD"
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
        state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        self.cert_dir = data_home / "io.github.bjarkimg.shield-remote"
        self.certfile = str(self.cert_dir / "cert.pem")
        self.keyfile = str(self.cert_dir / "key.pem")
        self.state_path = state_home / "omarchy" / "settings" / "shield-remote.json"
        self.state: dict[str, Any] = {"selected": "", "devices": {}}

    def load_state(self) -> None:
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("devices"), dict):
                self.state = loaded
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

        selected = str(self.state.get("selected", ""))
        device = self.state.get("devices", {}).get(selected, {})
        if isinstance(device, dict):
            self.host = str(device.get("host") or device.get("address") or self.host)
            self.name = str(device.get("name") or self.name)
            self.identifier = selected

        if not self.host and self.default_host:
            self.host = self.default_host
            self.name = self.default_name

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.state_path)

    def make_remote(self, host: str) -> AndroidTVRemote:
        return AndroidTVRemote(
            client_name=CLIENT_NAME,
            certfile=self.certfile,
            keyfile=self.keyfile,
            host=host,
            loop=self.loop,
        )

    async def ensure_cert(self) -> None:
        self.cert_dir.mkdir(parents=True, exist_ok=True)
        remote = self.make_remote(self.host or "127.0.0.1")
        await remote.async_generate_cert_if_missing()

    async def start(self) -> None:
        await self.ensure_cert()
        self.load_state()
        if not self.host:
            raise RuntimeError("no SHIELD selected — open Devices to scan or add a host")
        await self.connect(self.host, self.name)

    async def resolve_host(self, host: str) -> str:
        host = host.strip()
        if not host:
            raise RuntimeError("missing host")
        if ":" in host and host.count(":") == 1:
            host = host.split(":", 1)[0]
        try:
            socket.inet_aton(host)
            return host
        except OSError:
            addresses = await self.loop.getaddrinfo(
                host,
                None,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
            if not addresses:
                raise RuntimeError(f"could not resolve {host}")
            return str(addresses[0][4][0])

    async def connect(self, host: str, name: str = "") -> None:
        address = await self.resolve_host(host)
        await self.close_connection()
        remote = self.make_remote(address)
        await remote.async_generate_cert_if_missing()
        try:
            name_from_cert, mac = await remote.async_get_name_and_mac()
        except CannotConnect as error:
            raise RuntimeError(f"could not reach {name or host} at {address}") from error

        display_name = name or name_from_cert or self.default_name
        identifier = mac or address
        try:
            await remote.async_connect()
        except InvalidAuth as error:
            remote.disconnect()
            self.host = address
            self.name = display_name
            self.identifier = identifier
            self.remember_device(
                {
                    "identifier": identifier,
                    "name": display_name,
                    "host": address,
                    "address": address,
                    "paired": False,
                    "online": True,
                }
            )
            raise RuntimeError(f"{display_name} needs pairing") from error

        remote.keep_reconnecting()
        self.remote = remote
        self.connected = True
        self.host = address
        self.name = display_name
        self.identifier = identifier
        device = {
            "identifier": identifier,
            "name": display_name,
            "host": address,
            "address": address,
            "paired": True,
            "online": True,
        }
        self.discovered[identifier] = device
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
        identifier = str(device.get("identifier") or "")
        if not identifier:
            return
        stored = {
            "identifier": identifier,
            "name": str(device.get("name") or "SHIELD"),
            "host": str(device.get("host") or device.get("address") or ""),
            "address": str(device.get("address") or device.get("host") or ""),
            "paired": bool(device.get("paired")),
        }
        self.state.setdefault("devices", {})[identifier] = stored
        if stored["paired"]:
            self.state["selected"] = identifier
        self.save_state()

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
            try:
                process = await asyncio.create_subprocess_exec(
                    "avahi-browse",
                    "-rtpk",
                    service,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except FileNotFoundError:
                return records

            stdout, _ = await process.communicate()
            for raw_line in stdout.decode(errors="replace").splitlines():
                fields = raw_line.split(";")
                if len(fields) < 9 or fields[0] != "=" or fields[2] != "IPv4":
                    continue
                address = fields[7]
                name = self.decode_avahi(fields[3])
                host = self.decode_avahi(fields[6])
                identifier = address
                if len(fields) >= 10:
                    for item in shlex.split(fields[9]):
                        key, separator, value = item.partition("=")
                        if separator and key.lower() in {"bt", "mac"}:
                            identifier = value
                            break
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
            cert_name, mac = await remote.async_get_name_and_mac()
        except CannotConnect as error:
            raise RuntimeError(f"could not reach {name or host} at {address}") from error
        finally:
            remote.disconnect()
        identifier = mac or address
        stored = self.state.get("devices", {}).get(identifier, {})
        return {
            "identifier": identifier,
            "name": name or cert_name or "SHIELD",
            "host": address,
            "address": address,
            "paired": bool(isinstance(stored, dict) and stored.get("paired")),
            "online": True,
        }

    async def scan_devices(self) -> list[dict[str, Any]]:
        visible: dict[str, dict[str, Any]] = {}
        records = await self.avahi_records()
        for identifier, device in records.items():
            self.discovered[identifier] = device
            visible[identifier] = device
            if device.get("paired"):
                self.remember_device(device)

        for identifier, stored in self.state.get("devices", {}).items():
            if identifier not in visible and isinstance(stored, dict):
                visible[identifier] = {
                    **stored,
                    "identifier": identifier,
                    "paired": bool(stored.get("paired")),
                    "online": False,
                }

        self.save_state()
        return sorted(
            visible.values(),
            key=lambda device: (
                not bool(device.get("paired")),
                not bool(device.get("online")),
                str(device.get("name", "")).lower(),
            ),
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
        if identifier == self.identifier and self.connected:
            emit("switched", **self.active_device())
            return

        device = self.discovered.get(identifier) or self.state.get("devices", {}).get(identifier)
        if not isinstance(device, dict):
            raise RuntimeError("that SHIELD is no longer available")
        if not device.get("paired"):
            raise RuntimeError(f"{device.get('name') or 'SHIELD'} is not paired")

        await self.connect(
            str(device.get("host") or device.get("address") or ""),
            str(device.get("name") or ""),
        )
        emit("switched", **self.active_device())

    async def start_pairing(self, identifier: str) -> None:
        await self.close_pairing()
        device = self.discovered.get(identifier) or self.state.get("devices", {}).get(identifier)
        if not isinstance(device, dict):
            raise RuntimeError("that SHIELD is no longer available")

        host = str(device.get("host") or device.get("address") or "")
        address = await self.resolve_host(host)
        remote = self.make_remote(address)
        await remote.async_generate_cert_if_missing()
        try:
            await remote.async_start_pairing()
        except CannotConnect as error:
            remote.disconnect()
            raise RuntimeError(f"could not start pairing with {device.get('name') or host}") from error

        self.pairing = remote
        self.pairing_identifier = identifier
        emit(
            "pairing-pin",
            identifier=identifier,
            name=str(device.get("name") or "SHIELD"),
        )

    async def finish_pairing(self, pin: str) -> None:
        if self.pairing is None:
            raise RuntimeError("pairing has not been started")
        code = pin.strip().upper()
        if not re.fullmatch(r"[0-9A-F]{6}", code):
            raise ValueError("enter the six-character code shown on the SHIELD")

        identifier = self.pairing_identifier
        host = self.pairing.host
        try:
            await self.pairing.async_finish_pairing(code)
        except InvalidAuth as error:
            raise RuntimeError("the SHIELD did not accept that code") from error
        finally:
            await self.close_pairing()

        device = self.discovered.get(identifier) or self.state.get("devices", {}).get(identifier) or {}
        await self.connect(host, str(device.get("name") or ""))
        emit("paired", **self.active_device())

    async def add_host(self, host: str, name: str = "") -> None:
        device = await self.probe_host(host, name)
        self.discovered[str(device["identifier"])] = device
        self.remember_device(device)
        if device["paired"]:
            await self.connect(device["host"], device["name"])
            emit("switched", **self.active_device())
            return
        await self.start_pairing(str(device["identifier"]))

    async def handle_request(self, request: dict[str, Any]) -> None:
        operation = str(request.get("op", ""))
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
        raise ValueError(f"unknown operation: {operation}")

    async def dispatch(self, action: str) -> str:
        if not self.connected or self.remote is None:
            if not self.host:
                raise RuntimeError("no SHIELD selected — open Devices to scan or add a host")
            await self.connect(self.host, self.name)

        assert self.remote is not None
        try:
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
            raise ValueError(f"unknown action: {action}")
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
            line = await asyncio.to_thread(sys.stdin.readline)
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
                    action=raw_command,
                    result=result,
                    elapsedMs=round((time.monotonic() - started) * 1000, 1),
                )
            except Exception as error:
                action = raw_command
                if raw_command.startswith("{"):
                    try:
                        action = str(json.loads(raw_command).get("op", "request"))
                    except json.JSONDecodeError:
                        action = "request"
                emit(
                    "error",
                    action=action,
                    message=str(error),
                    connected=self.connected,
                )


async def async_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="")
    parser.add_argument("--name", default="SHIELD")
    args = parser.parse_args()

    session = RemoteSession(args.host, args.name)
    try:
        await session.run()
    finally:
        await session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
