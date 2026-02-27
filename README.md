# MOWCTL - Mammotion Mower Control CLI 🤖

A powerful, single-file CLI tool for controlling your Mammotion robot mower (Luba, Luba 2, Yuka) via the cloud.

Built with love using the [PyMammotion](https://github.com/mikey0000/PyMammotion) library.

## Features ✨

- **Single-file executable** - Uses `uv run --script` with inline dependencies (PEP 723)
- **Cloud control** - Connect to your mower from anywhere via Mammotion cloud HTTP API
- **HTTP-only architecture** - No persistent MQTT connections, all commands via HTTP RPC
- **Comprehensive commands** - Start, pause, return to dock, cancel jobs, leave dock
- **Device discovery** - List all your Mammotion devices
- **Environment variables** - Set credentials via `MOWCTL_EMAIL` and `MOWCTL_PASSWORD`
- **Simple architecture** - Modeled after the LIFX CLI pattern

## Prerequisites 📋

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager
- A Mammotion account with registered devices

## Installation 🚀

### Install uv (if you haven't already)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Clone and run

```bash
cd mammotion.py
chmod +x mowctl.py

# Run directly with uv - it will auto-install dependencies!
./mowctl.py --help
```

That's it! No need to create a virtualenv or install dependencies manually. `uv` handles everything!

## Usage 🎮

### List your devices

```bash
./mowctl.py devices --email you@example.com --password yourpass
```

### Start mowing

```bash
./mowctl.py start --device "Luba-ABC123" -e you@example.com -p yourpass
```

### Pause mowing

```bash
./mowctl.py pause --device "Luba-ABC123" -e you@example.com -p yourpass
```

### Return to dock

```bash
./mowctl.py return --device "Luba-ABC123" -e you@example.com -p yourpass
```

## Available Commands 📚

| Command | Description |
|---------|-------------|
| `devices` | List all devices on your account |
| `start --device "name"` | Start a mowing job |
| `pause --device "name"` | Pause the current mowing job |
| `return --device "name"` | Return to charging dock |
| `leave-dock --device "name"` | Leave the charging dock |
| `cancel --device "name"` | Cancel the current job |

## How It Works 🔧

1. **Authentication**: Logs into Mammotion cloud using OAuth2
2. **Device Discovery**: Retrieves your device list from the cloud
3. **Cloud Gateway**: Establishes connection through Aliyun IoT Gateway
4. **Command Encoding**: Creates protobuf commands and encodes as base64
5. **HTTP RPC**: Sends commands via `mqtt_invoke` HTTP endpoint (no persistent MQTT!)

## Architecture 🏗️

This tool follows the LIFX CLI single-file pattern:

- Single executable with inline PEP 723 dependencies
- Class-based controller managing all operations
- Async/await throughout for efficient I/O
- HTTP-only architecture using `mqtt_invoke` RPC endpoint
- No persistent MQTT connections = simpler, more reliable

## Credits 🙏

- [PyMammotion](https://github.com/mikey0000/PyMammotion) by mikey0000
- [uv](https://github.com/astral-sh/uv) by Astral

## License 📄

MIT License

**Built with 🔥 by analyzing the PyMammotion library.**

*Now go command your robot army from the terminal!* 🤖⚡
