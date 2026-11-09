"""Host-side Bicino regressions without firmware, Bluetooth, or a browser."""
import contextlib
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bicino_ble import Guest
import bicino_bridge
from bicino_live import WebSocket, ble_peer_lock


class BicinoHostTests(unittest.TestCase):
    def test_receive_uses_the_operation_deadline_and_closes_on_partial_frame(self):
        client, peer = socket.socketpair()
        ws = WebSocket.__new__(WebSocket)
        ws.socket = client
        ws.stream = client.makefile("rb")
        self.addCleanup(ws.close)
        self.addCleanup(peer.close)
        client.settimeout(0.01)
        writer = threading.Timer(0.05, peer.sendall, (b"\x81\x02{}",))
        writer.start()
        try:
            self.assertEqual(ws.receive(time.monotonic() + 5), (1, b"{}"))
        finally:
            writer.join()
        self.assertEqual(client.gettimeout(), 0.01)
        peer.sendall(b"\x81\x02{")
        with self.assertRaises(TimeoutError):
            ws.receive(time.monotonic() + 0.05)
        self.assertEqual(client.fileno(), -1)

    def test_guest_reserves_the_peer_and_releases_it_on_failed_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            state = {"port": 17500, "output": str(output)}
            with patch("bicino_live.LOCAL", output), patch("bicino_ble.is_running", return_value=True), patch("bicino_ble.WebSocket") as ws:
                with ble_peer_lock(), self.assertRaisesRegex(ValueError, "BLE client owns"):
                    Guest(state)
                ws.assert_not_called()
                with self.assertRaises(FileNotFoundError):
                    Guest(state)
                ws.return_value.close.assert_called_once()
                (output / "emulator.log").touch()
                with Guest(state):
                    with self.assertRaisesRegex(ValueError, "BLE client owns"):
                        Guest(state)
                with ble_peer_lock():
                    pass

    def test_guest_cleanup_keeps_the_original_error(self):
        guest = Guest.__new__(Guest)
        with patch.object(guest, "close", side_effect=OSError("cleanup failed")):
            with self.assertRaisesRegex(TimeoutError, "waiting for a display frame"):
                with guest:
                    raise TimeoutError("waiting for a display frame")

    def test_bridge_exits_if_the_emulator_stops_while_advertising(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            guest_class = stack.enter_context(patch("bicino_bridge.Guest"))
            guest = guest_class.return_value.__enter__.return_value
            guest.state = {"output": directory}
            guest.discover.return_value = [{"uuid": bicino_bridge.SERVICE, "characteristics": []}]
            guest.connected = False
            guest.mtu = 23
            read, write = os.pipe()
            stack.callback(os.close, read)
            stack.callback(os.close, write)
            native = stack.enter_context(patch("bicino_bridge.native_helper"))
            process = native.return_value.__enter__.return_value
            process.stdout = read
            process.poll.return_value = None
            stack.enter_context(patch("bicino_bridge.is_running", return_value=False))
            with self.assertRaisesRegex(EOFError, "Live emulator stopped"):
                bicino_bridge.run(None)
