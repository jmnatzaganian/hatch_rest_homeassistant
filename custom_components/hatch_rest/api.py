"""pyhatchbabyrestasync.

Derived from kjoconnor's pyhatchbabyrest repo.
All rights reserved.
https://github.com/kjoconnor/pyhatchbabyrest/blob/master/LICENSE

"""

from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
import asyncio
from datetime import datetime
import logging
import struct
from time import monotonic

from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakConnectionError,
    establish_connection,
)

from .const import CHAR_FEEDBACK, CHAR_LIST, CHAR_TX, PyHatchBabyRestSound

_LOGGER = logging.getLogger(__name__)


class _SlotFetch:
    """Pairs a slot index with an asyncio.Event so the fetch loop can await the notification."""

    def __init__(self, slot: int) -> None:
        self.slot = slot
        self._event = asyncio.Event()

    def complete(self) -> None:
        self._event.set()

    async def wait(self, timeout: float = 1.0) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


class PyHatchBabyRestAsync:
    """An asynchronous interface to a Hatch Rest device using bleak."""

    def __init__(self, ble_device: BLEDevice) -> None:
        """Init PyHatchBabyRestAsync."""
        self.device = ble_device
        self.address = ble_device.address

        self._client: BleakClientWithServiceCache | None = None
        self._active_operations: int = 0

        # connection synchronization primitizes / state
        self._connection_cv = asyncio.Condition()
        self._connecting: bool = False
        self._send_lock = asyncio.Lock()
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._callbacks: set[Callable[[], None]] = set()
        self._is_notifying: bool = False

        # cached device state
        self.color: tuple[int, int, int] | None = None
        self.brightness: int | None = None
        self.sound: PyHatchBabyRestSound | None = None
        self.volume: int | None = None
        self.power: bool | None = None
        
        # timer state
        self.timer_total: int | None = None
        self._timer_expires_at: float | None = None

        # Favorites/schedules are fetched once per integration load (i.e. once per
        # PyHatchBabyRestAsync lifetime). Subsequent reconnects skip the sweep.
        # To pick up Hatch-app changes, reload the integration in HA.
        self._has_fetched_full: bool = False

        # Clock sync: written to the device at most once per calendar day, gated
        # to after 02:30 local time so DST transitions (which happen at 02:00) are
        # already reflected in datetime.now() before we read it.
        # The device clock only drives the onboard scheduler — HA scenes/automations
        # are unaffected by device clock accuracy.
        self._last_clock_sync_date: str | None = None

        # True while _send_commands holds _send_lock. Advertisement-sourced state
        # updates are suppressed during this window to prevent the device's in-flight
        # pre-command broadcast from briefly reverting an optimistic state update
        # (the "volume oscillation" symptom seen during scene playback).
        self._command_in_flight: bool = False

        self._init_collections()

    @property
    def timer_remaining(self) -> int | None:
        """Return the remaining timer in minutes, calculated locally."""
        if self._timer_expires_at is None:
            return None
        remaining = (self._timer_expires_at - monotonic()) / 60
        if remaining <= 0:
            return 0
        return int(remaining + 0.01) # Small epsilon to handle precision issues

    @timer_remaining.setter
    def timer_remaining(self, value: int | None) -> None:
        """Set the remaining timer in minutes and update expiration time."""
        if value is not None:
            # The device returns 65535 or 0xFFFF when no timer is active
            if value >= 0xFFFF:
                self._timer_expires_at = None
                return
            self._timer_expires_at = monotonic() + (value * 60)
        else:
            self._timer_expires_at = None

    def _init_collections(self) -> None:
        """Initialize collections."""
        self.favorites: dict[int, dict] = {}
        self.active_favorite: int | None = None
        self.schedules: dict[int, dict] = {}
        self._last_index_seen: int | None = None
        self._pending_slot: dict = {}  # buffered data before first index arrives
        self._pgb_fetch: _SlotFetch | None = None  # in-flight PGB fetch during initial sweep
        self._egb_fetch: _SlotFetch | None = None  # in-flight EGB fetch during initial sweep
        self._pending_pgb_slots: deque[int] = deque()  # slots queued from toggle_favorite
        self._pending_egb_slots: deque[int] = deque()  # slots queued from toggle_schedule
        self._pending_gf: bool = False  # True while waiting for a GF index response

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the BLE device."""
        self.device = ble_device
        self.address = ble_device.address

    def update_from_advertisement(self, service_info: "BluetoothServiceInfoBleak") -> None:
        """Update state from advertisement data."""
        from .const import MANUFACTURER_ID

        if MANUFACTURER_ID not in service_info.manufacturer_data:
            return

        # Always update the BLEDevice so the next connection uses the freshest
        # RSSI/address, even if we suppress the state parse below.
        self.update_ble_device(service_info.device)

        if self._command_in_flight:
            # The device broadcasts its current (pre-command) state while we are
            # mid-write. Parsing that advertisement would briefly revert the
            # optimistic state set by the command, causing visible oscillation
            # (e.g. volume flickering during scene playback). Skip the parse;
            # the notification response from the command will update state instead.
            _LOGGER.debug("Suppressing advertisement state update: command in flight")
            return

        data = service_info.manufacturer_data[MANUFACTURER_ID]
        _LOGGER.debug("Received advertisement data: %s", data.hex())
        self._parse_data(data)

    def register_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for when data is updated."""
        self._callbacks.add(callback)

    def remove_callback(self, callback: Callable[[], None]) -> None:
        """Remove a previously registered callback."""
        self._callbacks.discard(callback)

    def _notify_callbacks(self) -> None:
        """Notify all registered callbacks of an update."""
        for callback in self._callbacks:
            callback()

    @asynccontextmanager
    async def _active_operation(self):
        """Context manager that tracks an in-flight operation and schedules disconnect on exit."""
        self._active_operations += 1
        _LOGGER.debug("Active operations: %d", self._active_operations)
        await self._client_connect()
        try:
            yield self._client
        except Exception as e:
            _LOGGER.warning("Operation error: %r", e)
        finally:
            self._active_operations -= 1
            _LOGGER.debug("Active operations: %d", self._active_operations)
            self._schedule_disconnect()

    def _client_disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Callback for when the client disconnects."""
        _LOGGER.debug("API client has successfully disconnected")
        self._client = None
        self._is_notifying = False
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None

    async def _fetch_favorites(self, client: BleakClientWithServiceCache) -> None:
        """Fetch all 6 favorite slots, waiting for each PGB notification before continuing."""
        for slot in range(1, 7):
            self._pgb_fetch = _SlotFetch(slot)
            cmd = f"PGB{slot:02X}"
            _LOGGER.debug("Requesting %s", cmd)
            await client.write_gatt_char(CHAR_TX, bytearray(cmd, "utf-8"), response=False)
            if not await self._pgb_fetch.wait():
                _LOGGER.warning("Timeout waiting for %s response", cmd)
        self._pgb_fetch = None

    async def _fetch_schedules(self, client: BleakClientWithServiceCache) -> None:
        """Fetch all 10 schedule slots, waiting for each EGB notification before continuing."""
        for slot in range(1, 11):
            self._egb_fetch = _SlotFetch(slot)
            cmd = f"EGB{slot:02X}"
            _LOGGER.debug("Requesting %s", cmd)
            await client.write_gatt_char(CHAR_TX, bytearray(cmd, "utf-8"), response=False)
            if not await self._egb_fetch.wait():
                _LOGGER.warning("Timeout waiting for %s response", cmd)
        self._egb_fetch = None

    async def _client_connect(self) -> None:
        """Connect to the device."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None

        async with self._connection_cv:
            if self._client and self._client.is_connected:
                return

            if self._connecting:
                await self._connection_cv.wait()
                return

            self._connecting = True

        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                self.device,
                self.address,
                disconnected_callback=self._client_disconnected,
            )
            _LOGGER.debug("Client connected: %s", client.is_connected)

            # Reset per-connection parse state
            self._last_index_seen = None
            self._pending_slot = {}
            self._pgb_fetch = None
            self._egb_fetch = None
            self._pending_pgb_slots.clear()
            self._pending_egb_slots.clear()

            # Start notifications
            if client.is_connected:
                try:
                    # We subscribe to the CONFIG channel FIRST to ensure we don't miss the dump
                    _LOGGER.debug("Subscribing to config channel %s...", CHAR_LIST)
                    await client.start_notify(CHAR_LIST, self._notification_handler)
                    
                    _LOGGER.debug("Subscribing to feedback channel %s...", CHAR_FEEDBACK)
                    await client.start_notify(CHAR_FEEDBACK, self._notification_handler)
                    
                    self._is_notifying = True

                    # GF returns the active favorite index. Then PGB01-PGB06
                    # fetches each slot's config+name. Both confirmed via btsnoop.
                    _LOGGER.debug("Requesting active favorite index via 'GF'")
                    self._pending_gf = True
                    await client.write_gatt_char(
                        CHAR_TX, bytearray(b"GF"), response=False
                    )

                    # Sync device clock at most once per calendar day, but not before
                    # 02:30 local time. DST transitions happen at 02:00 — waiting until
                    # 02:30 ensures the OS has applied the new UTC offset before we read
                    # datetime.now(), so the timestamp written to the device is always
                    # correct for the current DST state.
                    # The device clock only drives the onboard scheduler; HA scenes and
                    # automations run on HA's clock and are unaffected by this value.
                    now = datetime.now()
                    today = now.strftime("%Y-%m-%d")
                    past_dst_window = now.hour > 2 or (now.hour == 2 and now.minute >= 30)
                    if self._last_clock_sync_date != today and past_dst_window:
                        clock_cmd = f"ST{now.strftime('%Y%m%d%H%M%S')}U"
                        _LOGGER.debug("Syncing device clock: %s", clock_cmd)
                        await client.write_gatt_char(
                            CHAR_TX, bytearray(clock_cmd, "utf-8"), response=False
                        )
                        self._last_clock_sync_date = today

                    if not self._has_fetched_full:
                        _LOGGER.debug("Fetching favorites and schedules (first connect this session)")
                        # Update in-place — don't clear, so toggle_favorite can still read
                        # existing cache while the fresh PGB/EGB responses arrive.
                        await self._fetch_favorites(client)
                        await self._fetch_schedules(client)
                        self._has_fetched_full = True
                    else:
                        _LOGGER.debug("Skipping favorites/schedules fetch (already fetched this session)")
                except Exception as e:
                    if "already notifying" not in str(e):
                        _LOGGER.warning("Notification error: %r", e)

        except Exception as e:
            _LOGGER.warning("Connect error: %r", e)
            client = None

        async with self._connection_cv:
            self._connecting = False
            self._client = client
            self._connection_cv.notify_all()

    def _schedule_disconnect(self) -> None:
        """Schedule a disconnection after a cooldown period."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()

        # _schedule_disconnect is only ever called from inside an active operation
        # (the _active_operation context manager), so a running loop always exists.
        self._disconnect_timer = asyncio.get_running_loop().call_later(
            10, lambda: asyncio.create_task(self._client_disconnect())
        )
        _LOGGER.debug("Scheduled disconnect in 10 seconds")

    async def _client_disconnect(self) -> None:
        """Disconnect from the device."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None

        if self._client and self._active_operations == 0:
            _LOGGER.debug(
                "self._client = %s and self._active_operations = %d, attempting to disconnect",
                self._client,
                self._active_operations,
            )
            try:
                if self._is_notifying:
                    try:
                        await self._client.stop_notify(CHAR_LIST)
                    except Exception as e:  # noqa: BLE001
                        _LOGGER.debug("stop_notify CHAR_LIST failed (harmless): %r", e)
                    try:
                        await self._client.stop_notify(CHAR_FEEDBACK)
                    except Exception as e:  # noqa: BLE001
                        _LOGGER.debug("stop_notify CHAR_FEEDBACK failed (harmless): %r", e)
                    self._is_notifying = False
                await self._client.disconnect()
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("Exception during _client_disconnect -- %r", e)
        else:
            _LOGGER.debug(
                "self._client = %s and self._active_operations = %d, cannot currently disconnect",
                self._client,
                self._active_operations,
            )

    async def _send_commands(
        self,
        commands: list[str],
        raw: bool = False,
        response: bool = True,
        spacing_delay: float = 0.1,
        pgb_slot: int | None = None,
        egb_slot: int | None = None,
    ):
        """Send a batch of commands to the device.

        :param commands: List of ASCII command strings.
        :param raw: If True, decode command as hex string and send raw bytes.
        :param response: If True, wait for GATT-level acknowledgment.
        :param spacing_delay: Seconds to wait between commands in a batch.
        :param pgb_slot: Favorite slot index expected in the PGB response (enqueued inside lock).
        :param egb_slot: Schedule slot index expected in the EGB response (enqueued inside lock).
        """
        async with self._send_lock:
            if log_timing := _LOGGER.isEnabledFor(logging.DEBUG):
                start = monotonic()
                _LOGGER.debug("Started batch _send_commands at %s", datetime.now().isoformat())

            self._command_in_flight = True
            try:
                async with self._active_operation() as client:
                    # Enqueue response slots AFTER connecting — connect resets the deques on reconnect.
                    if pgb_slot is not None:
                        self._pending_pgb_slots.append(pgb_slot)
                    if egb_slot is not None:
                        self._pending_egb_slots.append(egb_slot)

                    if client and client.is_connected:
                        for i, command in enumerate(commands):
                            data = bytearray.fromhex(command) if raw else bytearray(command, "utf-8")
                            _LOGGER.debug("Sending command '%s' (Hex: %s)", command, data.hex())
                            await client.write_gatt_char(char_specifier=CHAR_TX, data=data, response=response)
                            if i < len(commands) - 1 and spacing_delay > 0:
                                await asyncio.sleep(spacing_delay)
                    else:
                        _LOGGER.warning("Could not send commands: Not connected to device")
            finally:
                self._command_in_flight = False

            if log_timing:
                _LOGGER.debug(
                    "Finished batch _send_commands at %s (total of %.3f seconds)",
                    datetime.now().isoformat(),
                    monotonic() - start,  # pyright: ignore[reportPossiblyUnboundVariable]
                )

    async def _send_command(self, command: str, raw: bool = False, response: bool = True):
        """Send a single command to the device."""
        return await self._send_commands([command], raw=raw, response=response, spacing_delay=0)

    def _notification_handler(self, char_specifier, data: bytearray) -> None:
        """Handle incoming notifications from the device."""
        # char_specifier can be a BleakGATTCharacteristic (has .uuid) or an int handle
        char_uuid = getattr(char_specifier, "uuid", None)
        handle = char_specifier if isinstance(char_specifier, int) else getattr(char_specifier, "handle", None)
        
        _LOGGER.debug("[Notification] Handle: %s, UUID: %s, Data: %s", handle, char_uuid, data.hex())
        
        # Identify the source. 02240003 is often handle 19, 02260002 is handle 23.
        # We check UUID string first, then fallback to logic based on characteristic type
        is_config = False
        if char_uuid == CHAR_LIST:
            is_config = True
        elif isinstance(char_specifier, int) or char_uuid is None:
            # Fallback for handle-only notifications: config packets usually 
            # don't start with 'Ti' (0x54 0x69)
            if not data.startswith(b"Ti"):
                is_config = True

        if is_config:
            self._parse_config_data(data)
        else:
            self._parse_data(data)

    @staticmethod
    def _decode_config_block(data: bytearray) -> dict:
        """Decode a 15-byte PGB favorite block.

        Layout: [0x01] [sound] [volume] [00x6] [brightness] [B] [G] [R] [flags] [0x03]
        """
        sound_id = data[1]
        volume = data[2]
        brightness = data[9]
        blue, green, red = data[10], data[11], data[12]
        flags = data[13] if len(data) > 13 else 0
        try:
            sound = PyHatchBabyRestSound(sound_id)
        except ValueError:
            sound = sound_id
        return {
            "sound": sound,
            "volume": volume,
            "brightness": brightness,
            "color": (red, green, blue),
            "enabled": bool(flags & 0x80),  # bit 7: 0x96=enabled, 0x16=disabled (PSL C0 vs PSL 80)
            "raw": data.hex(),
        }

    @staticmethod
    def _decode_schedule_block(data: bytearray) -> dict:
        """Decode a 20-byte EGB schedule block.

        Layout (confirmed via live EGB responses):
          [0x01] [unix_ts LE x4] [sound] [volume] [hour] [minute]
          [00x4] [brightness] [B] [G] [R] [0x00] [days_bitmask] [flags]

        days_bitmask bits 1-5 = Mon-Fri (0x3e = Mon-Fri confirmed).
        """
        sound_id = data[5]
        volume = data[6]
        hour = data[7]
        minute = data[8]
        brightness = data[13]
        blue, green, red = data[14], data[15], data[16]
        days_mask = data[18] if len(data) > 18 else 0
        flags = data[19] if len(data) > 19 else 0
        modified_ts = struct.unpack_from("<I", data, 1)[0]
        days = {
            "mon": bool(days_mask & 0x02),
            "tue": bool(days_mask & 0x04),
            "wed": bool(days_mask & 0x08),
            "thu": bool(days_mask & 0x10),
            "fri": bool(days_mask & 0x20),
            "sat": bool(days_mask & 0x40),
            "sun": bool(days_mask & 0x01),
        }
        try:
            sound = PyHatchBabyRestSound(sound_id)
        except ValueError:
            sound = sound_id
        return {
            "sound": sound,
            "volume": volume,
            "hour": hour,
            "minute": minute,
            "brightness": brightness,
            "color": (red, green, blue),
            "days": days,
            "enabled": bool(flags & 0x40),
            "modified_ts": modified_ts,
            "raw": data.hex(),
        }

    def _flush_pending_slot(self, index: int) -> None:
        """Merge buffered config/name (received before first index) into favorites."""
        if self._pending_slot:
            _LOGGER.debug("Flushing pending slot data to index %d: %s", index, self._pending_slot)
            self.favorites.setdefault(index, {}).update(self._pending_slot)
            self._pending_slot = {}

    def _parse_config_data(self, data: bytearray) -> None:
        """Parse configuration data (names, indices, etc) from CHAR_LIST."""
        if not data:
            return

        _LOGGER.debug("[Config] Hex: %s | ASCII: %s", data.hex(), data.decode("utf-8", errors="ignore"))

        changed = False
        try:
            header = data[0]

            # GI response: "FF" = no timer active
            if data == b"FF":
                _LOGGER.debug("Timer: no timer active (GI=FF)")
                if self.timer_total is not None or self.timer_remaining is not None:
                    self.timer_total = None
                    self.timer_remaining = None
                    changed = True

            # GD response: 4-char ASCII hex = remaining minutes (e.g. "0076" = 118 min)
            elif len(data) == 4 and all(chr(b) in "0123456789ABCDEFabcdef" for b in data):
                try:
                    remaining = int(data.decode(), 16)
                    _LOGGER.debug("Timer remaining: %d min (GD)", remaining)
                    if self.timer_remaining != remaining:
                        self.timer_remaining = remaining
                        changed = True
                except (ValueError, UnicodeDecodeError):
                    pass

            # Header 0x30 is GF active-favorite index — only trusted when we sent GF
            elif self._pending_gf and header == 0x30 and len(data) >= 2:
                self._pending_gf = False
                index_str = data[1:2].decode("utf-8", errors="ignore")
                if index_str.isdigit():
                    index = int(index_str)
                    if self.active_favorite != index:
                        _LOGGER.debug("Active favorite (GF): %d", index)
                        self.active_favorite = index
                        changed = True
                    self._last_index_seen = index
                    self._flush_pending_slot(index)

            # Header 0x30 without _pending_gf = PSF/ESF slot-save confirmation — ignore
            elif header == 0x30 and len(data) >= 2:
                _LOGGER.debug("Slot-save confirmation (ignoring as active-favorite): %s", data.decode("utf-8", errors="ignore"))

            # Header 0x07 is an ASCII name block
            elif header == 0x07:
                # Some packets have a leading null or control byte before the text
                start_idx = 1
                while start_idx < len(data) and data[start_idx] < 32:
                    start_idx += 1

                name_bytes = data[start_idx:]
                if 0x00 in name_bytes:
                    name_bytes = name_bytes[:name_bytes.find(0x00)]

                name = name_bytes.decode("utf-8", errors="ignore").strip()
                if name:
                    if self._egb_fetch is not None:
                        slot_dict = self.schedules.setdefault(self._egb_fetch.slot, {})
                        if slot_dict.get("name") != name:
                            _LOGGER.debug("Schedule name '%s' (slot %d)", name, self._egb_fetch.slot)
                            slot_dict["name"] = name
                            changed = True
                    elif self._last_index_seen is None:
                        if self._pending_slot.get("name") != name:
                            _LOGGER.debug("Buffering name '%s' (index not yet known)", name)
                            self._pending_slot["name"] = name
                    else:
                        slot_dict = self.schedules.setdefault(self._last_index_seen, {})
                        if slot_dict.get("name") != name:
                            _LOGGER.debug("Captured name '%s' (index %d)", name, self._last_index_seen)
                            slot_dict["name"] = name
                            changed = True

            # Header 0x01 — 15-byte = favorite (PGB), 20-byte = schedule (EGB)
            elif header == 0x01 and len(data) >= 13:
                if len(data) >= 20:
                    parsed = self._decode_schedule_block(data)
                    if self._pending_egb_slots:
                        idx = self._pending_egb_slots.popleft()
                    else:
                        idx = self._egb_fetch.slot if self._egb_fetch else self._last_index_seen
                    if idx is not None:
                        slot_dict = self.schedules.setdefault(idx, {})
                        if any(slot_dict.get(k) != v for k, v in parsed.items()):
                            _LOGGER.debug("Schedule config slot %s: %s", idx, parsed)
                            slot_dict.update(parsed)
                            changed = True
                    if self._egb_fetch is not None:
                        self._egb_fetch.complete()
                else:
                    parsed = self._decode_config_block(data)
                    if self._pending_pgb_slots:
                        idx = self._pending_pgb_slots.popleft()
                    else:
                        idx = self._pgb_fetch.slot if self._pgb_fetch else self._last_index_seen
                    if idx is None:
                        if any(self._pending_slot.get(k) != v for k, v in parsed.items()):
                            self._pending_slot.update(parsed)
                    else:
                        slot_dict = self.favorites.setdefault(idx, {})
                        if any(slot_dict.get(k) != v for k, v in parsed.items()):
                            _LOGGER.debug("Favorite config slot %s: %s", idx, parsed)
                            slot_dict.update(parsed)
                            changed = True
                    if self._pgb_fetch is not None:
                        self._pgb_fetch.complete()

            # Header 0x4F ('OK')
            elif data.startswith(b"OK"):
                _LOGGER.debug("Device acknowledged command (OK)")

        except Exception as e:
            _LOGGER.debug("Failed to parse config notification: %r", e)
        
        if changed:
            self._notify_callbacks()

    def _parse_data(self, data: bytearray) -> None:
        """Parse raw device data and update state."""
        _LOGGER.debug("Parsing raw data: %s", data.hex())
        try:
            # Find Color Section 'C' (0x43)
            c_idx = data.find(0x43)
            if c_idx != -1 and len(data) >= c_idx + 5:
                red, green, blue, brightness = data[c_idx + 1 : c_idx + 5]
                new_color = (red, green, blue)
                new_brightness = brightness
            else:
                new_color = self.color
                new_brightness = self.brightness

            # Find Sound Section 'S' (0x53)
            s_idx = data.find(0x53)
            if s_idx != -1 and len(data) >= s_idx + 3:
                sound_id = data[s_idx + 1]
                try:
                    new_sound = PyHatchBabyRestSound(sound_id)
                except ValueError:
                    _LOGGER.debug("Unknown sound ID received: %d", sound_id)
                    new_sound = sound_id
                new_volume = data[s_idx + 2]
            else:
                new_sound = self.sound
                new_volume = self.volume

            # Find Power Section 'P' (0x50)
            p_idx = data.find(0x50)
            if p_idx != -1 and len(data) >= p_idx + 2:
                # We revert to the robust power logic: bits 7 & 6 OFF means device is ON.
                # Standard Hatch: 0xC0=OFF (Manual), 0x80=OFF (Favorite?), 0x00=ON (Manual), 0x01=ON (Fav1).
                # User fix: bool(power_byte & 0x40) or (power_byte == 0x1f)
                # Reverting to the logic from ~2 commits ago as requested:
                power_byte = data[p_idx + 1]
                new_power = not bool(int("11000000", 2) & power_byte) or (power_byte == 0x1f)
                
                # Active favorite index is bits 0-5
                new_favorite = power_byte & 0x3F
                if new_favorite == 0x3F or new_favorite == 31:
                    new_favorite = None
            else:
                new_power = self.power
                new_favorite = self.active_favorite

            if (
                new_color == self.color
                and new_brightness == self.brightness
                and new_sound == self.sound
                and new_volume == self.volume
                and new_power == self.power
                and new_favorite == self.active_favorite
            ):
                return

            self.color = new_color
            self.brightness = new_brightness
            self.sound = new_sound
            self.volume = new_volume
            self.power = new_power
            self.active_favorite = new_favorite

            _LOGGER.debug(
                "Parsed state (CHANGED): power=%s, brightness=%s, color=%s, sound=%s, volume=%s, active_favorite=%s",
                self.power, self.brightness, self.color, self.sound, self.volume, self.active_favorite,
            )
            self._notify_callbacks()
        except (ValueError, IndexError) as e:
            _LOGGER.warning("Failed to parse device data: %r", e)

    async def refresh_data(self):
        """Refresh data from Hatch Rest device."""
        async with self._send_lock:
            if log_timing := _LOGGER.isEnabledFor(logging.DEBUG):
                start = monotonic()
                _LOGGER.debug("Started refresh_data at %s", datetime.now().isoformat())

            async with self._active_operation() as client:
                if client and client.is_connected:
                    await client.write_gatt_char(CHAR_TX, bytearray(b"GI"), response=True)
                    await asyncio.sleep(0.1)
                    await client.write_gatt_char(CHAR_TX, bytearray(b"GD"), response=True)
                    await asyncio.sleep(0.1)
                    raw_char_read = await client.read_gatt_char(CHAR_FEEDBACK)
                    _LOGGER.debug("Raw char read from refresh_data: %s", raw_char_read)
                    self._parse_data(raw_char_read)
                else:
                    _LOGGER.warning("Could not refresh data: Not connected to device")

        if log_timing:
            _LOGGER.debug(
                "Finished refresh_data at %s (total of %.3f seconds)",
                datetime.now().isoformat(),
                monotonic() - start,  # pyright: ignore[reportPossiblyUnboundVariable]
            )

    async def select_favorite(self, index: int):
        """Select a favorite by index (confirmed via btsnoop: SP activates, PSB only edits)."""
        command = f"SP{index:02X}"
        _LOGGER.debug("API command: select_favorite %d", index)
        return await self._send_command(command, response=True)

    @staticmethod
    def _build_ps_commands(
        index: int,
        flag: str,
        color: tuple[int, int, int],
        brightness: int,
        sound: PyHatchBabyRestSound | int,
        volume: int,
    ) -> list[str]:
        """Build the PSB→PSC→PSN→PSV→PSL→PSF command sequence for a favorite slot."""
        r, g, b = color
        sound_val = sound.value if isinstance(sound, PyHatchBabyRestSound) else int(sound)
        return [
            f"PSB{index:02X}",
            f"PSC{r:02X}{g:02X}{b:02X}{brightness:02X}",
            f"PSN{sound_val:02X}",
            f"PSV{volume:02X}",
            f"PSL{flag}",
            "PSF",
        ]

    async def save_to_favorite(self, index: int) -> None:
        """Save current device state to a favorite slot."""
        _LOGGER.debug("API command: save_to_favorite slot=%d", index)
        commands = self._build_ps_commands(
            index, "C0",
            self.color or (0, 0, 0),
            self.brightness or 0,
            self.sound or PyHatchBabyRestSound.none,
            self.volume or 0,
        )
        await self._send_commands(commands)

    async def get_timer(self) -> None:
        """Get the current timer state."""
        _LOGGER.debug("API command: get_timer (GI)")
        await self._send_command("GI")

    async def set_timer(self, seconds: int) -> None:
        """Set a timer for the specified number of seconds."""
        command = f"SD{seconds:04X}"
        _LOGGER.debug("API command: set_timer %s (%d sec)", command, seconds)
        self.timer_total = seconds // 60
        self._timer_expires_at = monotonic() + seconds
        await self._send_command(command)

    async def get_timer_remaining(self) -> None:
        """Get the remaining time on the current timer."""
        _LOGGER.debug("API command: get_timer_remaining (GD)")
        await self._send_command("GD")

    async def toggle_favorite(self, index: int, enable: bool) -> None:
        """Enable or disable a favorite slot.

        PSF commits ALL fields — must resend existing data alongside the new flag
        or the slot gets wiped to defaults. We connect first so the initial PGB
        fetch has run and self.favorites is populated before we read it.
        """
        flag = "C0" if enable else "80"
        _LOGGER.debug("API command: toggle_favorite slot=%d enable=%s", index, enable)
        if index not in self.favorites:
            _LOGGER.warning("toggle_favorite: no cached data for slot %d — skipping to avoid overwriting slot with zeros", index)
            return
        cached = self.favorites[index]
        commands = self._build_ps_commands(
            index, flag,
            cached.get("color") or (0, 0, 0),
            cached.get("brightness") or 0,
            cached.get("sound") or PyHatchBabyRestSound.none,
            cached.get("volume") or 0,
        ) + [f"PGB{index:02X}"]
        await self._send_commands(commands, pgb_slot=index)

    async def toggle_schedule(self, index: int, enable: bool) -> None:
        """Enable or disable a schedule slot."""
        flag = "C0" if enable else "80"
        _LOGGER.debug("API command: toggle_schedule slot=%d enable=%s", index, enable)
        await self._send_commands(
            [f"ESB{index:02X}", f"ESL{flag}", "ESF", f"EGB{index:02X}"],
            egb_slot=index,
        )

    async def turn_power_on(self):
        """Power on the Hatch Rest device."""
        command = f"SI{1:02x}"
        _LOGGER.debug("API command: turn_power_on")
        self.power = True
        await self._send_command(command, response=True)

    async def turn_power_off(self):
        """Power off the Hatch Rest device."""
        command = f"SI{0:02x}"
        _LOGGER.debug("API command: turn_power_off")
        self.power = False
        await self._send_command(command, response=True)

    async def set_sound(self, sound: int):
        """Set the sound of the Hatch Rest device."""
        command = f"SN{sound:02x}"
        _LOGGER.debug("API command: set_sound to %s", command)
        self.sound = PyHatchBabyRestSound(sound)
        return await self._send_command(command, response=True)

    async def set_volume(self, volume: int):
        """Set the volume of the Hatch Rest device."""
        command = f"SV{volume:02x}"
        _LOGGER.debug("API command: set_volume to %s", command)
        self.volume = volume
        return await self._send_command(command, response=True)

    async def set_color(self, red: int, green: int, blue: int):
        """Set the color of the Hatch Rest device."""
        return await self.set_light_state(color=(red, green, blue))

    async def set_brightness(self, brightness: int):
        """Set the brightness of the Hatch Rest device."""
        return await self.set_light_state(brightness=brightness)

    async def set_light_state(
        self,
        brightness: int | None = None,
        color: tuple[int, int, int] | None = None,
    ):
        """Set the light state (color and brightness) in one command."""
        if brightness is not None:
            self.brightness = brightness
        if color is not None:
            self.color = color

        if self.color is not None and self.brightness is not None:
            command = f"SC{self.color[0]:02x}{self.color[1]:02x}{self.color[2]:02x}{self.brightness:02x}"
            _LOGGER.debug("API command: set_light_state to %s", command)
            return await self._send_command(command, response=True)
        return None

    @property
    def name(self):
        """Return the name of the Hatch Rest device."""
        return self.device.name
