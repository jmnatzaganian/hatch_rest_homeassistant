# Hatch Rest BLE Protocol

Reverse-engineered from btsnoop captures of the official Hatch app communicating with a 1st-generation Hatch Rest.

---

## GATT Characteristics

| Name | UUID | Direction | Purpose |
|---|---|---|---|
| `CHAR_TX` | `02240002-5efd-47eb-9c1a-de53f7a2b232` | Write | Send commands to device |
| `CHAR_LIST` | `02240003-5efd-47eb-9c1a-de53f7a2b232` | Notify | Config channel — favorites, schedules, timer, GF index |
| `CHAR_FEEDBACK` | `02260002-5efd-47eb-9c1a-de53f7a2b232` | Notify / Read | State channel — power, color, sound, volume |

`CHAR_TX` accepts both GATT write modes. The integration sends **user command writes with response** (`response=True`) — power, sound, volume, color, favorite select, timer (`SI`/`SN`/`SV`/`SC`/`SP`/`SD`), plus the `GI`/`GD` timer queries in `refresh_data` — and sends the **query/sweep path without response** (`response=False`): the `GF` index query, the once-per-session `PGB`/`EGB` slot fetches, and the clock-sync `ST` write. This split is the integration's convention, not a device constraint; the code in `api.py` is the source of truth for which mode each write uses.

---

## Manufacturer Advertisement Data

Manufacturer ID: `1076` (0x0434)

The advertisement payload uses the same tagged format as `CHAR_FEEDBACK`. Power state, color, sound, and volume are broadcast passively — no connection required for basic state monitoring.

---

## State Channel (`CHAR_FEEDBACK`)

Responses use a tagged, variable-length format. Each section is identified by a marker byte:

| Marker | Hex | Fields |
|---|---|---|
| `C` | `0x43` | R, G, B, brightness (1 byte each) |
| `S` | `0x53` | sound ID, volume (1 byte each) |
| `P` | `0x50` | power byte |

**Power byte encoding:**

- `0x00` — ON, no favorite active
- `0x01`–`0x06` — ON, favorite N active (bits 0–5 = index)
- `0x80` — OFF (favorite mode)
- `0xC0` — OFF (manual mode)
- `0x1F` — ON (special case)

Rule: `power = not (byte & 0xC0) or byte == 0x1F`; `active_favorite = byte & 0x3F` (treat `0x3F`/`0x1F` as None)

**Sound IDs:**

| ID | Sound |
|---|---|
| 0 | None |
| 2 | Stream |
| 3 | White noise |
| 4 | Dryer |
| 5 | Ocean |
| 6 | Wind |
| 7 | Rain |
| 9 | Bird |
| 10 | Crickets |
| 11 | Brahms |
| 13 | Twinkle |
| 14 | Rockabye |

---

## Commands (written to `CHAR_TX`)

### Power

| Command | Description |
|---|---|
| `SI01` | Turn on |
| `SI00` | Turn off |

### Light

| Command | Description |
|---|---|
| `SC{RR}{GG}{BB}{LL}` | Set color (hex RGB) and brightness (hex). e.g. `SCff804064` |

Setting color to `FEFEFE` activates gradient/rainbow mode.

### Sound

| Command | Description |
|---|---|
| `SN{NN}` | Set sound by ID (hex). e.g. `SN07` = rain |
| `SV{VV}` | Set volume (hex 0–FF). e.g. `SV80` = 128 |

### Timer

| Command | Description |
|---|---|
| `SD{SSSS}` | Set timer — `SSSS` is duration in **seconds**, 4-digit hex. e.g. `SD0384` = 900s = 15 min |
| `GI` | Query timer total — device replies `"FF"` (no timer) on `CHAR_LIST` |
| `GD` | Query timer remaining — device replies 4-char ASCII hex **minutes** on `CHAR_LIST`. e.g. `"0076"` = 118 min |

### Favorites

| Command | Description |
|---|---|
| `GF` | Query active favorite index — device replies `"0{N}"` on `CHAR_LIST` |
| `SP{NN}` | Activate favorite slot N (hex 01–06). `SP00` = deselect |
| `PGB{NN}` | Read favorite slot N — device sends a 15-byte PGB block on `CHAR_LIST` |
| `PSB{NN}` | Select slot to write |
| `PSC{RR}{GG}{BB}{LL}` | Set slot color + brightness |
| `PSN{NN}` | Set slot sound ID |
| `PSV{VV}` | Set slot volume |
| `PSL{FF}` | Set slot flags (`C0` = enabled, `80` = disabled) |
| `PSF` | Commit — writes ALL fields atomically. **Must send PSB→PSC→PSN→PSV→PSL first.** |

**PSF confirmation:** After `PSF`, the device sends `"01"` on `CHAR_LIST` — identical in format to a GF response. Gate GF parsing on a `_pending_gf` flag to avoid misreading this as active_favorite = 1.

#### PGB Block Layout (15 bytes)

```
[0x01] [sound] [volume] [00 00 00 00 00 00] [brightness] [B] [G] [R] [flags] [0x03]
  0       1       2       3  4  5  6  7  8       9         10  11  12    13     14
```

- `flags & 0x80` = enabled (`0x96` = enabled, `0x16` = disabled)
- Color stored as BGR

### Schedules

| Command | Description |
|---|---|
| `EGB{NN}` | Read schedule slot N (hex 01–0A) — device sends a 20-byte EGB block on `CHAR_LIST` |
| `ESB{NN}` | Select schedule slot to write |
| `ESL{FF}` | Set schedule flags (`C0` = enabled, `80` = disabled) |
| `ESF` | Commit schedule slot |

#### EGB Block Layout (20 bytes)

```
[0x01] [unix_ts LE x4] [sound] [volume] [hour] [minute]
  0       1  2  3  4     5       6        7       8

[00 00 00 00] [brightness] [B] [G] [R] [0x00] [days] [flags]
  9 10 11 12      13        14  15  16    17     18     19
```

- `flags & 0x40` = enabled
- `days` bitmask: bit 0 = Sun, bit 1 = Mon, bit 2 = Tue, bit 3 = Wed, bit 4 = Thu, bit 5 = Fri, bit 6 = Sat

### Clock Sync

| Command | Description |
|---|---|
| `ST{YYYYMMDDHHmmss}U` | Set device clock. e.g. `ST20260420100000U` |

Sent at most once per calendar day, and only on a connection after 02:30 local time (the 02:30 gate keeps DST transitions, which occur at 02:00, from being written with the wrong UTC offset). The device clock only drives the onboard scheduler; HA scenes and automations are unaffected by it.

### Miscellaneous

| Command | Description |
|---|---|
| `GF` | Get active favorite index |

---

## Config Channel Notification Dispatch (`CHAR_LIST`)

| Pattern | Meaning |
|---|---|
| `data == b"FF"` (2 bytes) | GI response — no timer active |
| `len == 4`, all hex chars | GD response — remaining minutes (parse as `int(data, 16)`) |
| `header == 0x30`, `_pending_gf` set | GF response — active favorite index is `data[1]` |
| `header == 0x30`, `_pending_gf` not set | PSF/ESF save confirmation — ignore |
| `header == 0x07` | ASCII name block for current slot |
| `header == 0x01`, `len >= 20` | EGB schedule block |
| `header == 0x01`, `13 <= len < 20` | PGB favorite block |
| `data.startswith(b"OK")` | Device acknowledged command |

---

## Connection & Batching

- Commands are batched per 10-second idle window using a `_send_lock`.
- The connection is held open while `_active_operations > 0`; a 10-second disconnect timer fires after all operations complete.
- On connect: subscribe to `CHAR_LIST` first (to avoid missing the initial dump), then `CHAR_FEEDBACK`; send `GF`; sync clock (subject to the once-per-day / after-02:30 gate above); and, **only on the first connect of the integration load**, fetch PGB01–PGB06 and EGB01–EGB0A. The favorites/schedules sweep runs once per `PyHatchBabyRestAsync` lifetime (`_has_fetched_full`), not on a stale-cache TTL — reload the integration to re-fetch after editing favorites/schedules in the Hatch app.
- Favorites cache is updated **in-place** on reconnect — never cleared — so toggle commands can read existing slot data even while fresh PGB responses are still arriving.
- Background polling defaults to **60 minutes** (`DEFAULT_SCAN_INTERVAL`); state is intended to arrive primarily via BLE advertisements and optimistic command updates, with the poll as a slow safety net.
