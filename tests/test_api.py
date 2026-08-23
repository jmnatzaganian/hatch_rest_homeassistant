"""Tests for Hatch Rest API."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakConnectionError

from custom_components.hatch_rest.api import PyHatchBabyRestAsync
from custom_components.hatch_rest.const import CHAR_TX, PyHatchBabyRestSound


def _assert_value(check_val: list[str], index: int, assert_val: str):
    if check_val[index] != assert_val:
        raise ValueError(f'response[{index}] "{check_val[index]}" != "{assert_val}"')


class TestAssertValue:
    """Tests for _assert_value helper."""

    def test_assert_value_passes(self):
        values = ["0x00", "0x43", "0x53"]
        _assert_value(values, 1, "0x43")

    def test_assert_value_fails(self):
        values = ["0x00", "0x43", "0x53"]
        with pytest.raises(ValueError, match='response\\[1\\] "0x43" != "0x99"'):
            _assert_value(values, 1, "0x99")


class TestPyHatchBabyRestAsync:
    """Tests for PyHatchBabyRestAsync."""

    @pytest.fixture
    def api(self, mock_ble_device: BLEDevice) -> PyHatchBabyRestAsync:
        """Create API instance."""
        api = PyHatchBabyRestAsync(mock_ble_device)
        yield api
        if api._disconnect_timer:
            api._disconnect_timer.cancel()
            api._disconnect_timer = None

    def test_init(self, api: PyHatchBabyRestAsync, mock_ble_device: BLEDevice):
        """Test API initialization."""
        assert api.device == mock_ble_device
        assert api.address == mock_ble_device.address
        assert api.color is None
        assert api.brightness is None
        assert api.sound is None
        assert api.volume is None
        assert api.power is None

    def test_name_property(self, api: PyHatchBabyRestAsync):
        """Test name property returns device name."""
        assert api.name == "Hatch Rest"

    @pytest.mark.asyncio
    async def test_client_connect_success(self, api: PyHatchBabyRestAsync):
        """Test successful client connection."""
        mock_client = MagicMock()
        mock_client.is_connected = True

        with patch(
            "custom_components.hatch_rest.api.establish_connection",
            new_callable=AsyncMock,
            return_value=mock_client,
        ):
            with patch.object(api, "_fetch_favorites", new_callable=AsyncMock):
                with patch.object(api, "_fetch_schedules", new_callable=AsyncMock):
                    await api._client_connect()
                    assert api._client == mock_client

    @pytest.mark.asyncio
    async def test_client_connect_failure(self, api: PyHatchBabyRestAsync):
        """Test client connection failure is handled."""
        with patch(
            "custom_components.hatch_rest.api.establish_connection",
            new_callable=AsyncMock,
            side_effect=BleakConnectionError("Connection failed"),
        ):
            await api._client_connect()
            assert api._client is None

    @pytest.mark.asyncio
    async def test_client_disconnect_when_idle(self, api: PyHatchBabyRestAsync):
        """Test client disconnects when no active operations."""
        mock_client = AsyncMock()
        mock_client.disconnect = AsyncMock()
        api._client = mock_client
        api._active_operations = 0

        await api._client_disconnect()
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_client_no_disconnect_when_busy(self, api: PyHatchBabyRestAsync):
        """Test client doesn't disconnect with active operations."""
        mock_client = AsyncMock()
        mock_client.disconnect = AsyncMock()
        api._client = mock_client
        api._active_operations = 1

        await api._client_disconnect()
        mock_client.disconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_data_parses_response(self, api: PyHatchBabyRestAsync):
        """Test refresh_data correctly parses device response."""
        # Simulated raw response from device
        # Format: [..., 0x43, R, G, B, brightness, 0x53, sound, volume, 0x50, power_byte]
        raw_response = bytearray(
            [
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,  # padding (indices 0-4)
                0x43,  # color marker (index 5)
                0xFF,
                0x80,
                0x40,
                0x64,  # R, G, B, brightness (indices 6-9)
                0x53,  # audio marker (index 10)
                0x05,
                0x64,  # sound (ocean=5), volume (100) (indices 11-12)
                0x50,  # power marker (index 13)
                0x00,  # power byte (0x00 = ON, 0xC0 = OFF) (index 14)
            ]
        )

        mock_client = AsyncMock()
        mock_client.read_gatt_char = AsyncMock(return_value=raw_response)
        api._client = mock_client

        with patch.object(api, "_client_connect", new_callable=AsyncMock):
            with patch.object(api, "_client_disconnect", new_callable=AsyncMock):
                await api.refresh_data()

        assert api.color == (255, 128, 64)
        assert api.brightness == 100
        assert api.sound == PyHatchBabyRestSound.ocean
        assert api.volume == 100
        assert api.power is True
        assert api.active_favorite == 0

    @pytest.mark.asyncio
    async def test_refresh_data_sends_timer_commands(self, api: PyHatchBabyRestAsync):
        """Test refresh_data sends GI and GD commands."""
        raw_response = bytearray(20)

        mock_client = AsyncMock()
        mock_client.read_gatt_char = AsyncMock(return_value=raw_response)
        mock_client.write_gatt_char = AsyncMock()
        api._client = mock_client

        with patch.object(api, "_client_connect", new_callable=AsyncMock):
            with patch.object(api, "_client_disconnect", new_callable=AsyncMock):
                await api.refresh_data()

        mock_client.write_gatt_char.assert_any_call(CHAR_TX, bytearray(b"GI"), response=True)
        mock_client.write_gatt_char.assert_any_call(CHAR_TX, bytearray(b"GD"), response=True)

    def test_parse_config_data_gd_sets_timer_remaining(self, api: PyHatchBabyRestAsync):
        """Test GD notification updates timer_remaining in seconds."""
        api._parse_config_data(bytearray(b"0078"))  # 0x78 = 120 minutes
        assert api.timer_remaining == 120

    def test_parse_config_data_gi_ff_clears_timer(self, api: PyHatchBabyRestAsync):
        """Test GI FF notification clears timer state."""
        api.timer_total = 300
        api.timer_remaining = 120
        api._parse_config_data(bytearray(b"FF"))
        assert api.timer_total is None
        assert api.timer_remaining is None

    @pytest.mark.asyncio
    async def test_turn_power_on(self, api: PyHatchBabyRestAsync):
        """Test turn_power_on sends correct command."""
        with patch.object(api, "_send_command", new_callable=AsyncMock) as mock_send:
            await api.turn_power_on()
            mock_send.assert_called_once_with("SI01", response=True)

    @pytest.mark.asyncio
    async def test_turn_power_off(self, api: PyHatchBabyRestAsync):
        """Test turn_power_off sends correct command."""
        with patch.object(api, "_send_command", new_callable=AsyncMock) as mock_send:
            await api.turn_power_off()
            mock_send.assert_called_once_with("SI00", response=True)

    @pytest.mark.asyncio
    async def test_set_sound(self, api: PyHatchBabyRestAsync):
        """Test set_sound sends correct command."""
        with patch.object(api, "_send_command", new_callable=AsyncMock) as mock_send:
            await api.set_sound(PyHatchBabyRestSound.rain)
            mock_send.assert_called_once_with("SN07", response=True)  # rain = 7

    @pytest.mark.asyncio
    async def test_set_volume(self, api: PyHatchBabyRestAsync):
        """Test set_volume sends correct command."""
        with patch.object(api, "_send_command", new_callable=AsyncMock) as mock_send:
            await api.set_volume(128)
            mock_send.assert_called_once_with("SV80", response=True)  # 128 in hex

    @pytest.mark.asyncio
    async def test_set_color(self, api: PyHatchBabyRestAsync):
        """Test set_color sends correct command."""
        api.brightness = 100
        with patch.object(api, "_send_command", new_callable=AsyncMock) as mock_send:
            await api.set_color(255, 128, 64)
            mock_send.assert_called_once_with("SCff804064", response=True)

    @pytest.mark.asyncio
    async def test_set_brightness(self, api: PyHatchBabyRestAsync):
        """Test set_brightness sends correct command."""
        api.color = (255, 128, 64)
        with patch.object(api, "_send_command", new_callable=AsyncMock) as mock_send:
            await api.set_brightness(200)
            mock_send.assert_called_once_with("SCff8040c8", response=True)  # 200 in hex = c8

    @pytest.mark.asyncio
    async def test_send_command_writes_to_characteristic(
        self, api: PyHatchBabyRestAsync
    ):
        """Test _send_command writes to correct characteristic."""
        mock_client = AsyncMock()
        mock_client.write_gatt_char = AsyncMock()
        api._client = mock_client

        with patch.object(api, "_client_connect", new_callable=AsyncMock):
            with patch.object(api, "refresh_data", new_callable=AsyncMock):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await api._send_command("SI01")

        mock_client.write_gatt_char.assert_called_once_with(
            char_specifier=CHAR_TX,
            data=bytearray("SI01", "utf-8"),
            response=True,
        )

    @pytest.mark.asyncio
    async def test_select_favorite(self, api: PyHatchBabyRestAsync):
        """Test select_favorite sends correct command."""
        with patch.object(api, "_send_command", new_callable=AsyncMock) as mock_send:
            await api.select_favorite(2)
            mock_send.assert_called_once_with("SP02", response=True)

    @pytest.mark.asyncio
    async def test_get_timer(self, api: PyHatchBabyRestAsync):
        """Test get_timer sends correct command."""
        with patch.object(api, "_send_command", new_callable=AsyncMock) as mock_send:
            await api.get_timer()
            mock_send.assert_called_once_with("GI")

    @pytest.mark.asyncio
    async def test_toggle_favorite(self, api: PyHatchBabyRestAsync):
        """Test toggle_favorite sends correct commands."""
        api.favorites[2] = {}  # seed cache so guard passes
        with patch.object(api, "_send_commands", new_callable=AsyncMock) as mock_send:
            await api.toggle_favorite(2, True)
            mock_send.assert_called_once_with(
                ["PSB02", "PSC00000000", "PSN00", "PSV00", "PSLC0", "PSF", "PGB02"],
                pgb_slot=2,
            )

    @staticmethod
    def _clock_writes(mock_client: AsyncMock) -> list:
        """Return any CHAR_TX writes that are ST clock-sync commands."""
        return [
            call
            for call in mock_client.write_gatt_char.call_args_list
            if call.args
            and call.args[0] == CHAR_TX
            and bytes(call.args[1]).startswith(b"ST")
        ]

    @pytest.mark.asyncio
    async def test_client_connect_syncs_clock_once_per_day(
        self, api: PyHatchBabyRestAsync
    ):
        """Clock syncs on the first connect of the day, then not again that day."""
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.start_notify = AsyncMock()
        mock_client.write_gatt_char = AsyncMock()

        # 10:00 is past the 02:30 DST-safe gate, so a sync is allowed.
        fixed_now = datetime(2026, 4, 20, 10, 0, 0)
        with patch("custom_components.hatch_rest.api.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now

            with patch(
                "custom_components.hatch_rest.api.establish_connection",
                new_callable=AsyncMock,
                return_value=mock_client,
            ), patch.object(
                api, "_fetch_favorites", new_callable=AsyncMock
            ), patch.object(api, "_fetch_schedules", new_callable=AsyncMock):
                # First connect of the day: clock IS synced.
                await api._client_connect()
                mock_client.write_gatt_char.assert_any_call(
                    CHAR_TX, bytearray(b"ST20260420100000U"), response=False
                )
                assert api._last_clock_sync_date == "2026-04-20"

                # Same-day reconnect: clock is NOT synced again.
                mock_client.write_gatt_char.reset_mock()
                api._client = None  # reset to force a fresh connect
                await api._client_connect()
                assert self._clock_writes(mock_client) == []

    @pytest.mark.asyncio
    async def test_client_connect_skips_clock_before_dst_window(
        self, api: PyHatchBabyRestAsync
    ):
        """No clock sync before 02:30 local time (DST guard)."""
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.start_notify = AsyncMock()
        mock_client.write_gatt_char = AsyncMock()

        fixed_now = datetime(2026, 4, 20, 2, 15, 0)  # before the 02:30 gate
        with patch("custom_components.hatch_rest.api.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now

            with patch(
                "custom_components.hatch_rest.api.establish_connection",
                new_callable=AsyncMock,
                return_value=mock_client,
            ), patch.object(
                api, "_fetch_favorites", new_callable=AsyncMock
            ), patch.object(api, "_fetch_schedules", new_callable=AsyncMock):
                await api._client_connect()
                assert self._clock_writes(mock_client) == []
                assert api._last_clock_sync_date is None

    def test_active_operations_starts_at_zero(self, api: PyHatchBabyRestAsync):
        """Test active operations counter initializes to zero."""
        assert api._active_operations == 0


class TestConnectionDeadlocks:
    """Regression tests for the unbounded waits in the connect/send paths.

    A wedged wait here has no user-visible timeout: an HA service call blocks
    forever, and a `mode: single` automation that made the call stays "running"
    permanently, silently skipping every subsequent trigger.
    """

    @pytest.fixture
    def api(self, mock_ble_device: BLEDevice) -> PyHatchBabyRestAsync:
        """Create API instance."""
        api = PyHatchBabyRestAsync(mock_ble_device)
        yield api
        if api._disconnect_timer:
            api._disconnect_timer.cancel()
            api._disconnect_timer = None

    @pytest.mark.asyncio
    async def test_cancelled_connect_clears_connecting_flag(
        self, api: PyHatchBabyRestAsync
    ):
        """A cancelled connect must still clear _connecting and notify waiters.

        asyncio.CancelledError is a BaseException, so `except Exception` misses
        it; only a `finally` keeps the flag from sticking True forever.
        """
        started = asyncio.Event()

        async def _hang(*args, **kwargs):
            started.set()
            await asyncio.sleep(3600)

        with patch("custom_components.hatch_rest.api.establish_connection", new=_hang):
            task = asyncio.create_task(api._client_connect())
            await asyncio.wait_for(started.wait(), timeout=5)
            assert api._connecting is True

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert api._connecting is False
        assert api._client is None

    @pytest.mark.asyncio
    async def test_waiter_does_not_block_forever_on_stuck_flag(
        self, api: PyHatchBabyRestAsync, monkeypatch
    ):
        """A caller must give up rather than wait on a notify that never comes."""
        monkeypatch.setattr(
            "custom_components.hatch_rest.api.CONNECT_WAIT_TIMEOUT", 0.01
        )
        api._connecting = True  # a connecting task that will never notify

        await asyncio.wait_for(api._client_connect(), timeout=5)

        # Flag cleared so the next caller retries instead of wedging too.
        assert api._connecting is False

    @pytest.mark.asyncio
    async def test_send_lock_guard_yields_false_on_timeout(
        self, api: PyHatchBabyRestAsync, monkeypatch
    ):
        """The guard reports failure instead of queueing behind a wedged holder."""
        monkeypatch.setattr("custom_components.hatch_rest.api.SEND_LOCK_TIMEOUT", 0.01)
        await api._send_lock.acquire()
        try:
            async with api._send_lock_guard("test") as acquired:
                assert acquired is False
        finally:
            api._send_lock.release()

    @pytest.mark.asyncio
    async def test_send_lock_guard_releases_lock_on_success(
        self, api: PyHatchBabyRestAsync
    ):
        """The happy path still acquires and releases normally."""
        async with api._send_lock_guard("test") as acquired:
            assert acquired is True
            assert api._send_lock.locked()
        assert not api._send_lock.locked()

    @pytest.mark.asyncio
    async def test_send_commands_skips_when_lock_wedged(
        self, api: PyHatchBabyRestAsync, monkeypatch
    ):
        """_send_commands returns instead of hanging the calling service call."""
        monkeypatch.setattr("custom_components.hatch_rest.api.SEND_LOCK_TIMEOUT", 0.01)
        await api._send_lock.acquire()
        try:
            with patch.object(
                api, "_client_connect", new_callable=AsyncMock
            ) as mock_connect:
                await asyncio.wait_for(api._send_commands(["SI"]), timeout=5)
                mock_connect.assert_not_called()
        finally:
            api._send_lock.release()

    @pytest.mark.asyncio
    async def test_active_operations_released_when_connect_cancelled(
        self, api: PyHatchBabyRestAsync
    ):
        """A cancelled connect inside _active_operation must not leak the counter."""

        async def _hang():
            await asyncio.sleep(3600)

        with patch.object(api, "_client_connect", side_effect=_hang):

            async def _run():
                async with api._active_operation():
                    pass

            task = asyncio.create_task(_run())
            await asyncio.sleep(0.05)
            assert api._active_operations == 1

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert api._active_operations == 0
