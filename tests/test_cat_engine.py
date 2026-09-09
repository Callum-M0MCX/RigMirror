import sys
import types
import unittest

if "serial" not in sys.modules:
    serial_stub = types.ModuleType("serial")
    serial_stub.SerialException = OSError
    serial_stub.SerialTimeoutException = TimeoutError
    serial_stub.Serial = object
    serial_stub.EIGHTBITS = 8
    serial_stub.PARITY_NONE = "N"
    serial_stub.STOPBITS_ONE = 1
    serial_stub.STOPBITS_TWO = 2
    sys.modules["serial"] = serial_stub

from cat_engine import CATEngine, SerialSettings


class FakeSerial:
    def __init__(self, incoming: bytes, response_on_write: bytes = b""):
        self.incoming = bytearray(incoming)
        self.response_on_write = response_on_write
        self.written = bytearray()
        self.is_open = True

    @property
    def in_waiting(self):
        return len(self.incoming)

    def write(self, data):
        self.written.extend(data)
        if self.response_on_write:
            self.incoming.extend(self.response_on_write)
            self.response_on_write = b""

    def flush(self):
        pass

    def read(self, _size):
        if not self.incoming:
            return b""
        value = self.incoming[:1]
        del self.incoming[:1]
        return bytes(value)


class CATEngineTests(unittest.TestCase):
    def test_query_dispatches_auto_information_before_expected_reply(self):
        information = []
        engine = CATEngine(information_callback=information.append)
        engine._serial = FakeSerial(b"TX0;", b"FA00007151000;")
        engine.settings = SerialSettings("COM4", 115200, timeout_seconds=0.05)

        reply = engine.query("FA;")

        self.assertEqual(reply, "FA00007151000;")
        self.assertEqual(information, ["TX0;"])
        self.assertEqual(engine._serial.written, b"FA;")

    def test_query_drains_stale_same_prefix_before_requesting_fresh_value(self):
        information = []
        engine = CATEngine(information_callback=information.append)
        engine._serial = FakeSerial(
            b"FA00003749990;TX0;", b"FA00003750000;"
        )
        engine.settings = SerialSettings("COM4", 115200, timeout_seconds=0.05)

        reply = engine.query("FA;")

        self.assertEqual(reply, "FA00003750000;")
        self.assertEqual(information, ["FA00003749990;", "TX0;"])


if __name__ == "__main__":
    unittest.main()
