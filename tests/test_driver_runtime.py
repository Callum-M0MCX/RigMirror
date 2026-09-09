import json
from pathlib import Path
import tempfile
import unittest

from driver_runtime import (
    driver_fingerprint, driver_label, driver_local_status, load_driver,
    test_driver_on_engine,
)
from errors import CATError
from mirror import MirrorCoordinator
from radio import DriverRadio, OperatingMode, RadioEndpoint, Receiver, VFO


DRIVER_PATH = Path(__file__).parents[1] / "drivers" / "Kenwood-TS-590SG.rmradio"
TS990_DRIVER_PATH = Path(__file__).parents[1] / "drivers" / "Kenwood-TS-990S.rmradio"
TS890_DRIVER_PATH = Path(__file__).parents[1] / "drivers" / "Kenwood-TS-890S.rmradio"
TS2000_DRIVER_PATH = Path(__file__).parents[1] / "drivers" / "Kenwood-TS-2000.rmradio"
TS480_DRIVER_PATH = Path(__file__).parents[1] / "drivers" / "Kenwood-TS-480.rmradio"


class FakeEngine:
    def __init__(self):
        reply = list("0" * 40)
        reply[0:2] = "IF"
        reply[28] = "0"
        reply[39] = ";"
        self.replies = {
            "ID;": "ID023;",
            "FR;": "FR0;",
            "FT;": "FT1;",
            "FA;": "FA00007138000;",
            "FB;": "FB00014195000;",
            "MD;": "MD2;",
            "IF;": "".join(reply),
            "AN;": "AN100;",
        }
        self.set_commands = []
        self.is_open = True

    def query(self, command):
        return self.replies[command]

    def set(self, command):
        self.set_commands.append(command)


class FakeTS990Engine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.replies.update({
            "ID;": "ID022;",
            "FA;": "FA00007138000;",
            "FB;": "FB00014195000;",
            "OM0;": "OM02;",
            "OM1;": "OM11;",
            "CB;": "CB1;",
            "AN00;": "AN00100;",
            "AN01;": "AN01100;",
        })


class FakeTS890Engine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.replies.update({
            "ID;": "ID024;", "FR;": "FR1;", "OM1;": "OM1D;",
            "AN;": "AN2101;",
        })


class FakeTS2000Engine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.replies.update({"ID;": "ID019;", "AN;": "AN0;"})


class FakeTS480Engine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.replies.update({"ID;": "ID020;", "AN;": "AN1;"})


class StrictTS590Engine(FakeEngine):
    """Stateful 590 model which reproduces the hardware's TX write rejection."""

    def __init__(self):
        super().__init__()
        self.receive_vfo = "0"
        self.transmit_vfo = "0"
        self.frequencies = {"A": 14_300_000, "B": 7_100_000}
        self.transmitting = False
        self.frequency_writes_while_tx = []

    def query(self, command):
        if command == "FR;":
            return f"FR{self.receive_vfo};"
        if command == "FT;":
            return f"FT{self.transmit_vfo};"
        if command in ("FA;", "FB;"):
            key = command[1]
            return f"F{key}{self.frequencies[key]:011d};"
        if command == "IF;":
            reply = list(super().query(command))
            reply[28] = "1" if self.transmitting else "0"
            return "".join(reply)
        return super().query(command)

    def set(self, command):
        if command.startswith(("FA", "FB")) and len(command) == 14:
            if self.transmitting:
                self.frequency_writes_while_tx.append(command)
                raise CATError("Radio rejected frequency write while transmitting: '?;'")
            self.frequencies[command[1]] = int(command[2:-1])
        elif command in ("FT0;", "FT1;"):
            self.transmit_vfo = command[2]
        super().set(command)


class DriverRuntimeTests(unittest.TestCase):
    def test_driver_radio_matches_endpoint_contract(self):
        driver = load_driver(DRIVER_PATH)
        engine = FakeEngine()
        radio = DriverRadio(engine, driver, DRIVER_PATH)
        endpoint = RadioEndpoint(radio, None, "Sub")

        self.assertEqual(radio.identify().model, "Kenwood TS-590SG")
        self.assertEqual(radio.read_active_vfo(), VFO.A)
        self.assertEqual(endpoint.read_frequency(), 7_138_000)
        endpoint.set_frequency(7_138_000)
        self.assertEqual(endpoint.read_mode(), OperatingMode.USB)
        endpoint.set_mode(OperatingMode.USB)
        self.assertFalse(endpoint.read_transmitting())
        self.assertEqual(endpoint.read_antenna_state().active_source, "ANT1")
        endpoint.set_receive_source("RX ANT")
        self.assertIn("FA00007138000;", engine.set_commands)
        self.assertIn("MD2;", engine.set_commands)
        self.assertIn("AN919;", engine.set_commands)

    def test_ts590_driver_accepts_six_metres_while_mirror_policy_remains_separate(self):
        driver = load_driver(DRIVER_PATH)
        operation = driver["capabilities"]["frequency_write"]
        self.assertEqual(operation["input"]["maximum"], 60_000_000)
        endpoint = RadioEndpoint(DriverRadio(FakeEngine(), driver, DRIVER_PATH), None, "Sub")
        endpoint.set_frequency(50_144_000)

    def test_commissioning_suite_and_exact_file_local_status(self):
        driver = load_driver(DRIVER_PATH)
        engine = FakeEngine()
        results = test_driver_on_engine(driver, engine)
        self.assertEqual(len(results), 9)
        self.assertIn("Frequency write/restore: PASS", results[3])

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.rmradio"
            path.write_text(json.dumps(driver), encoding="utf-8")
            fingerprint = driver_fingerprint(path)
            history = {fingerprint: {"status": "TESTED", "results": results}}
            self.assertEqual(driver_local_status(path, history), "TESTED")
            path.write_text(json.dumps(driver, indent=2), encoding="utf-8")
            self.assertEqual(driver_local_status(path, history), "NOT TESTED")

    def test_all_bundled_outboard_drivers_validate(self):
        expected = {
            TS990_DRIVER_PATH: "Kenwood TS-990S",
            DRIVER_PATH: "Kenwood TS-590SG",
            TS890_DRIVER_PATH: "Kenwood TS-890S",
            TS2000_DRIVER_PATH: "Kenwood TS-2000",
            TS480_DRIVER_PATH: "Kenwood TS-480HX / TS-480SAT",
        }
        for path, display_name in expected.items():
            with self.subTest(path=path.name):
                self.assertEqual(driver_label(load_driver(path)), display_name)

        bundled = list((Path(__file__).parents[1] / "drivers").glob("*.rmradio"))
        self.assertEqual(len(bundled), 25)
        for path in bundled:
            with self.subTest(schema=path.name):
                driver = load_driver(path)
                self.assertNotIn("rigmirror_validation", driver)
                self.assertEqual(driver["metadata"].get("release_channel"), "CANDIDATE")

    def test_legacy_antenna_numbers_can_map_to_common_driver_state(self):
        cases = (
            (TS2000_DRIVER_PATH, "AN0;", "ANT1"),
            (TS2000_DRIVER_PATH, "AN1;", "ANT2"),
            (TS480_DRIVER_PATH, "AN1;", "ANT1"),
            (TS480_DRIVER_PATH, "AN2;", "ANT2"),
        )
        for path, reply, expected in cases:
            with self.subTest(driver=path.name, reply=reply):
                engine = FakeEngine()
                engine.replies["AN;"] = reply
                radio = DriverRadio(engine, load_driver(path), path)
                self.assertEqual(radio.read_antenna_state().active_source, expected)

    def test_new_outboard_kenwoods_complete_commissioning_suite(self):
        cases = (
            (TS890_DRIVER_PATH, FakeTS890Engine()),
            (TS2000_DRIVER_PATH, FakeTS2000Engine()),
            (TS480_DRIVER_PATH, FakeTS480Engine()),
        )
        for path, engine in cases:
            with self.subTest(driver=path.name):
                results = test_driver_on_engine(load_driver(path), engine)
                self.assertEqual(len(results), 9)
                self.assertIn("Frequency write/restore: PASS", results[3])

    def test_dual_receiver_driver_uses_main_sub_selectors_and_preserves_control_band(self):
        driver = load_driver(TS990_DRIVER_PATH)
        engine = FakeTS990Engine()
        radio = DriverRadio(engine, driver, TS990_DRIVER_PATH)
        main = RadioEndpoint(radio, Receiver.MAIN, "Main")
        sub = RadioEndpoint(radio, Receiver.SUB, "Sub")

        self.assertTrue(radio.is_dual_receiver)
        self.assertEqual(main.read_frequency(), 7_138_000)
        self.assertEqual(sub.read_frequency(), 14_195_000)
        main.set_frequency(7_138_000)
        self.assertEqual(main.read_mode(), OperatingMode.USB)
        main.set_mode(OperatingMode.USB)
        self.assertEqual(main.read_antenna_state().active_source, "ANT1")
        main.set_receive_source("RX ANT")

        self.assertIn("FA00007138000;", engine.set_commands)
        self.assertIn("CB0;", engine.set_commands)
        self.assertIn("OM02;", engine.set_commands)
        self.assertIn("CB1;", engine.set_commands)
        self.assertIn("AN00919;", engine.set_commands)

    def test_dual_receiver_driver_commissioning_suite(self):
        driver = load_driver(TS990_DRIVER_PATH)
        results = test_driver_on_engine(driver, FakeTS990Engine())
        self.assertEqual(len(results), 9)
        self.assertIn("Receiver under test: MAIN", results[1])

    def test_ts590_m_uses_the_actual_transmit_vfo(self):
        driver = load_driver(DRIVER_PATH)
        engine = FakeEngine()
        radio = DriverRadio(engine, driver, DRIVER_PATH)
        endpoint = RadioEndpoint(radio, None, "Master")
        self.assertEqual(endpoint.read_transmit_frequency(), 14_195_000)
        endpoint.set_transmit_frequency(1_850_000)
        self.assertIn("FB00001850000;", engine.set_commands)

    def test_ts590_m_is_prepared_before_tx_and_never_writes_frequency_while_tx(self):
        engine = StrictTS590Engine()
        radio = DriverRadio(engine, load_driver(DRIVER_PATH), DRIVER_PATH)
        master = RadioEndpoint(radio, None, "Master")

        class Listener:
            frequency = 14_300_000
            mode = OperatingMode.USB
            def read_frequency(self): return self.frequency
            def set_frequency(self, value): self.frequency = value
            def read_mode(self): return self.mode
            def set_mode(self, value): self.mode = value
            def read_transmitting(self): return None

        listener = Listener()
        failures = []
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None,
            lambda index, message: failures.append((index, message)),
            poll_interval=0.005, tx_action="PARKING FREQUENCY",
            parking_frequency=1_850_000,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            coordinator.arm_memory(14_300_000)
            self.assertTrue(self._wait_for(lambda: engine.transmit_vfo == "1"))
            self.assertEqual(engine.frequencies["B"], 14_300_000)
            engine.frequencies["A"] = 14_299_980
            self.assertTrue(self._wait_for(lambda: coordinator._known[0] == 14_299_980))
            writes_before_tx = list(engine.set_commands)
            engine.transmitting = True
            self.assertTrue(self._wait_for(lambda: coordinator._transmitting))
            self.assertTrue(self._wait_for(lambda: listener.frequency == 1_850_000))
            self.assertEqual(engine.frequency_writes_while_tx, [])
            self.assertEqual(engine.frequencies["B"], 14_300_000)
            self.assertEqual(writes_before_tx, engine.set_commands)
            engine.transmitting = False
            self.assertTrue(self._wait_for(lambda: not coordinator._transmitting))
            self.assertTrue(self._wait_for(lambda: listener.frequency == 14_299_980))
        finally:
            coordinator.stop()
        self.assertEqual(failures, [])
        self.assertEqual(engine.transmit_vfo, "0")
        self.assertEqual(engine.frequencies["B"], 7_100_000)

    @staticmethod
    def _wait_for(predicate, timeout=1.0):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())


if __name__ == "__main__":
    unittest.main()
