"""Regression tests: async_write_registers_multi must be atomic.

A single tuple's encoding failure must abort the whole write rather than
silently sending a shorter, misaligned register list — a burst like the
Gen3 remote-control command (enable flag + active power + reactive power)
is meant to land as one consecutive block, and dropping registers from the
middle shifts everything after them to the wrong address on the wire.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.solax_modbus import SolaXModbusHub
from custom_components.solax_modbus.const import REGISTER_S32, REGISTER_U16


class _FakePlugin:
    order32 = "big"


class _FakeHub:
    """Minimal duck-typed stand-in for the attributes async_write_registers_multi touches."""

    def __init__(self) -> None:
        self._name = "test"
        self.plugin = _FakePlugin()
        self.writeLocals: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self.slowdown = 1
        self._client = AsyncMock()
        self._client.write_registers = AsyncMock(return_value="ok")

    async def is_online(self) -> bool:
        return True

    async def _track_task(self, coro: Any) -> Any:
        return await coro


@pytest.mark.asyncio
async def test_write_registers_multi_writes_when_all_tuples_encode_cleanly() -> None:
    hub = _FakeHub()

    resp = await SolaXModbusHub.async_write_registers_multi(
        hub,
        unit=1,
        address=0x7C,
        payload=[
            (REGISTER_U16, 1),
            (REGISTER_S32, -1000),
            (REGISTER_S32, 0),
        ],
    )

    assert resp == "ok"
    hub._client.write_registers.assert_awaited_once()
    _, kwargs = hub._client.write_registers.call_args
    # 1 register for U16 + 2 + 2 for the two S32 values = 5 registers, not fewer.
    assert len(kwargs["values"]) == 5


@pytest.mark.asyncio
async def test_write_registers_multi_aborts_on_cast_failure_instead_of_truncating() -> None:
    hub = _FakeHub()

    resp = await SolaXModbusHub.async_write_registers_multi(
        hub,
        unit=1,
        address=0x7C,
        payload=[
            (REGISTER_U16, 1),
            (REGISTER_S32, "not-a-number"),  # fails int() cast
            (REGISTER_S32, 0),
        ],
    )

    assert resp is None
    hub._client.write_registers.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_registers_multi_aborts_on_encoding_failure_instead_of_truncating() -> None:
    hub = _FakeHub()

    resp = await SolaXModbusHub.async_write_registers_multi(
        hub,
        unit=1,
        address=0x7C,
        payload=[
            (REGISTER_U16, 1),
            (REGISTER_S32, 2**40),  # out of int32 range: struct.error inside convert_to_registers
            (REGISTER_S32, 0),
        ],
    )

    assert resp is None
    hub._client.write_registers.assert_not_awaited()
