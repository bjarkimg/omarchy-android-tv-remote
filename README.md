# SHIELD Remote for Omarchy

A keyboard-first NVIDIA SHIELD remote for the Omarchy Quattro bar. It also
works with other Android TV / Google TV boxes.

Uses the Android TV Remote protocol — the same one as the Google TV phone app —
so there is no ADB or developer mode. Pair with the six-character code shown on
the television.

## Features

- Directional navigation, Select, Back, Home, and Menu
- Play/pause, rewind, fast-forward, and volume
- Wake, sleep, and power status
- Local-network discovery, PIN pairing, and device switching
- Shortcuts for Plex, Netflix, and YouTube
- Mouse and keyboard operation from a theme-aware bar panel

## Requirements

- Omarchy Quattro
- Python 3.9 or newer with `venv` support
- A SHIELD (or other Android TV) on the same local network
- Internet access on first launch to install the pinned
  [`androidtvremote2`](https://github.com/tronikos/androidtvremote2) dependency
  into an isolated environment
- Optional: Avahi's `avahi-browse` for faster discovery; you can always add a
  host by IP

The plugin runs unsandboxed inside `omarchy-shell`. Review the repository
before installing it.

## Install

```bash
omarchy plugin add https://github.com/bjarkimg/omarchy-shield-remote.git --enable
```

The first launch creates an isolated Python environment under
`${XDG_DATA_HOME:-$HOME/.local/share}/io.github.bjarkimg.shield-remote/` and
installs `androidtvremote2==0.3.1`. This can take a moment; the panel will
reconnect automatically when setup finishes.

Open the bar widget, choose **Devices**, scan or enter the SHIELD's IP, and
type the six-character PIN shown on the television.

## Controls

| Key | Action |
| --- | --- |
| Arrow keys or H/J/K/L | Navigate |
| Enter | Select / OK |
| B | Back |
| G | Home |
| M | Menu |
| P | Play/pause |
| R / F | Rewind / fast-forward |
| - / + | Volume |
| W | Wake |
| 1 / 2 / 3 | Plex / Netflix / YouTube |
| D | Devices |
| Escape or Q | Close |

## Update

```bash
omarchy plugin update io.github.bjarkimg.shield-remote
```

## Remove

```bash
omarchy plugin remove io.github.bjarkimg.shield-remote
```

Optionally remove its Python environment, pairing certs, and remembered devices:

```bash
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/io.github.bjarkimg.shield-remote"
rm -f "${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/settings/shield-remote.json"
```

## Development

```bash
omarchy plugin validate .
```

## Credits

Panel and session design follows Thomas Evans'
[Apple TV Remote for Omarchy](https://github.com/teevans/omarchy-apple-tv-remote).

NVIDIA, SHIELD, Plex, Netflix, and YouTube are trademarks of their owners.

## License

[MIT](LICENSE)
