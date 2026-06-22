# Hatch Rest – Home Assistant Integration

A custom Home Assistant integration for controlling the **Hatch Rest** (1st-generation) nightlight and sound machine over Bluetooth Low Energy (BLE).

Fully asynchronous and locally controlled — no cloud required. Built on a rewritten BLE API derived from the original work by **kjoconnor** in the `pyhatchbabyrest` project.

## ✨ Features

* **Local BLE control** — no cloud, no account required
* **Real-time updates** — state changes are picked up instantly via BLE advertisements and GATT notifications; no polling required for basic state
* **Efficient connections** — commands are batched into a single BLE session; the connection is held open for 10 seconds of idle time before disconnecting, preventing radio thrash during rapid changes
* **Full favorites support** — reads all 6 favorite slots on connect, enables/disables slots without overwriting their content, and exposes only enabled favorites in the selector
* **Schedule support** — reads all 10 schedule slots and exposes enable/disable toggles for each
* **Timer control** — set and monitor a sleep timer; the remaining time is shown as a live-countdown sensor
* **Configurable scan interval** — adjust the background state-poll interval via the options flow (default: 60 minutes). State arrives primarily from advertisements/commands, so this poll is a slow safety net rather than the primary state source.

## 📦 Installation

### HACS

1. Add this repository as a **Custom Repository** *(HACS → Integrations → Custom Repositories)*
2. Search for **Hatch Rest**
3. Install → Restart Home Assistant

## 🔍 Adding the Device

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Hatch Rest**
3. Choose your discovered device from the list
4. Configure the **Scan Interval** (default: 60 minutes)
5. Done!

## 🧩 Entities

| Platform | Entity | Description |
|---|---|---|
| **Switch** | Power | Master on/off |
| **Switch** | Favorite *N* Enabled | Enable or disable a favorite slot (1–6) |
| **Switch** | Schedule *N* Enabled | Enable or disable a schedule slot (1–10) |
| **Light** | Light | RGB color and brightness |
| **Media Player** | Media Player | Sound selection and volume |
| **Select** | Favorite | Switch between enabled favorites |
| **Number** | Set Timer | Set a sleep timer (in minutes) |
| **Sensor** | Timer Remaining | Countdown timestamp showing when the timer expires |

## 📡 Bluetooth Requirements

* A compatible Home Assistant Bluetooth controller is required
* This integration uses `bleak_retry_connector` for robust, retry-aware connection management

## 🧪 Contributing

Issues and PRs are welcome!
