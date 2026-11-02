"""One-fresh-read KScreen inventory tests."""

from __future__ import annotations

import asyncio
import json

from airplay_linux.capture.physical import KscreenConnectorInventory
from airplay_linux.display.inventory import KScreenInventory, MonitoredOutput
from airplay_linux.domain.ports import CommandOutput, CommandSpec
from airplay_linux.domain.resources import Err, Ok


class _Process:
    def __init__(self, documents):
        self.documents = documents if isinstance(documents, list) else [documents]
        self.calls = []

    async def run(self, spec: CommandSpec, timeout: float, stdin=None):
        self.calls.append((spec, timeout))
        if not self.documents:
            raise AssertionError("inventory performed more than one read")
        return Ok(value=CommandOutput(
            exit_code=0, stdout=json.dumps(self.documents.pop(0)).encode(), stderr=b"",
        ))


def test_single_fresh_inventory_supplies_geometry_origin_and_connector_projection():
    process = _Process({"outputs": [
        {"id": 1, "name": "DP-1", "enabled": True, "connected": True,
         "type": 14, "pos": {"x": -1920, "y": 0},
         "size": {"width": 1920, "height": 1080}, "scale": 1},
        {"id": 2, "name": "Virtual-1", "enabled": True, "connected": True,
         "type": 0, "pos": {"x": 0, "y": 0},
         "size": {"width": 1920, "height": 1080}, "scale": 1},
        {"id": 3, "name": "HDMI-1", "enabled": False, "connected": True,
         "type": 6},
    ]})
    result = asyncio.run(KScreenInventory(process).observe())
    assert isinstance(result, Ok)
    assert len(result.value) == 3
    assert result.value[0].name == "DP-1"
    assert result.value[0].connector == "DP-1"
    assert result.value[0].virtual is False
    assert result.value[2].size is None
    assert len(process.calls) == 1
    assert process.calls[0][0].argv == ("kscreen-doctor", "-j")

    connector_process = _Process({"outputs": [
        {"id": 1, "name": "DP-1", "enabled": True, "connected": True,
         "type": 14, "pos": {"x": -1920, "y": 0},
         "size": {"width": 1920, "height": 1080}, "scale": 1},
        {"id": 2, "name": "Virtual-1", "enabled": True, "connected": True,
         "type": 0, "pos": {"x": 0, "y": 0},
         "size": {"width": 1920, "height": 1080}, "scale": 1},
    ]})
    projected = asyncio.run(KscreenConnectorInventory(
        KScreenInventory(connector_process),
    )())
    assert projected == ("DP-1",)
    assert len(connector_process.calls) == 1


def test_inventory_refuses_enabled_output_with_unreadable_geometry():
    process = _Process({"outputs": [{
        "id": 1, "name": "DP-1", "enabled": True, "connected": True, "type": 14,
    }]})
    result = asyncio.run(KScreenInventory(process).observe())
    assert isinstance(result, Err)
