"""Protocol fixtures use manufacturer wire examples, not radio hardware."""
import unittest
from pathlib import Path
from test_cat_engine import FakeSerial
from cat_engine import CATEngine, SerialSettings
from driver_runtime import OperationRunner, load_driver, test_driver_on_engine
from errors import CATError, CATTimeoutError
from radio import DriverRadio, RadioEndpoint, Receiver, OperatingMode
from mirror import MirrorCoordinator

ROOT = Path(__file__).parents[1] / "drivers"

class CIVTests(unittest.TestCase):
    def engine(self, response, address=0x94):
        engine = CATEngine()
        engine.settings = SerialSettings("COM1", 19200, 0.005, protocol="icom_civ", civ_address=address)
        engine._serial = FakeSerial(b"", bytes.fromhex(response))
        return engine

    def test_echo_broadcast_wrong_address_and_fragmented_reply(self):
        engine = self.engine("FE FE 94 E0 25 00 FD FE FE 00 94 00 00 FD FE FE E0 98 25 00 00 00 10 07 00 FD FE FE E0 94 25 00 00 00 10 07 00 FD")
        self.assertEqual(engine.query("2500"), "25000000100700")
        self.assertEqual(engine._serial.written, bytes.fromhex("FE FE 94 E0 25 00 FD"))

    def test_override_address(self):
        engine = self.engine("FE FE E0 70 19 00 94 FD", 0x70)
        self.assertEqual(engine.query("1900"), "190094")
        self.assertEqual(engine._serial.written[2], 0x70)

    def test_ack_write(self):
        self.engine("FE FE E0 94 FB FD").set("25000000100700")

    def test_nak(self):
        with self.assertRaisesRegex(CATError, "NAK"):
            self.engine("FE FE E0 94 FA FD").query("2500")

    def test_missing_ack(self):
        with self.assertRaises(CATTimeoutError):
            self.engine("FE FE 94 E0 25 00 FD").set("2500")

    def test_bcd_fixture_and_bad_nibble(self):
        d = load_driver(ROOT / "Icom-IC-7300.rmradio")
        class Engine:
            def query(self, cmd): return "25000000140700"
        self.assertEqual(OperationRunner(d, Engine()).run("frequency_read")[0], 7140000)
        self.assertEqual(OperationRunner._convert_input({"type":"bcd_le"}, 7140000), "0000140700")
        with self.assertRaises(CATError):
            OperationRunner._extract({"type":"constant", "value":"FA", "decode":"bcd_le"}, "", None)

class RadioSimulator:
    is_open = True
    def __init__(self, model):
        self.model = model
        self.writes = []
        self.frequency = [7100000, 14200000]
        self.modes = ["01", "00"] if model.startswith("IC") else ["2", "1"]
    def query(self, cmd):
        if cmd == "ID;": return "ID0682;" if self.model == "FTDX101MP" else "ID0362;"
        if cmd == "1900": return "190094" if self.model == "IC-7300" else "190098"
        if cmd == "TX;": return "TX0;"
        if cmd == "1C00": return "1C0000"
        if cmd == "AN0;": return "AN01;"
        if cmd == "12": return "1200"
        if cmd in ("FA;", "FB;"):
            width = 9 if self.model == "FTDX101MP" else 8
            return cmd[:2] + f"{self.frequency[cmd == 'FB;']:0{width}d};"
        if cmd.startswith("MD"): return cmd[:3] + self.modes[int(cmd[2])] + ";"
        side = int(cmd[2:4], 16)
        if cmd.startswith("25"):
            return cmd + OperationRunner._convert_input({"type":"bcd_le"}, self.frequency[side])
        if cmd.startswith("26"): return cmd + self.modes[side] + "0102"
        raise AssertionError(cmd)
    def set(self, cmd):
        self.writes.append(cmd)
        if cmd.startswith(("FA", "FB")):
            self.frequency[cmd.startswith("FB")] = int(cmd[2:-1])
        elif cmd.startswith("MD"):
            self.modes[int(cmd[2])] = cmd[3]
        elif cmd.startswith("25"):
            self.frequency[int(cmd[2:4], 16)] = int(bytes.fromhex(cmd[4:])[::-1].hex())
        elif cmd.startswith("26"):
            self.modes[int(cmd[2:4], 16)] = cmd[4:6]

class ProfilesTests(unittest.TestCase):
    def test_ic7300mk2_rx_antenna_command_is_distinct_from_antenna_two(self):
        path = ROOT / "Icom-IC-7300MK2.rmradio"
        driver = load_driver(path)
        class Engine:
            is_open = True
            def __init__(self): self.writes = []
            def query(self, command):
                if command == "1200": return "120001"
                raise AssertionError(command)
            def set(self, command): self.writes.append(command)
        engine = Engine()
        radio = DriverRadio(engine, driver, path)
        endpoint = RadioEndpoint(radio, None, "Sub")
        self.assertEqual(endpoint.supported_receive_sources(), ("ANT1", "RX ANT"))
        self.assertEqual(endpoint.read_antenna_state().active_source, "RX ANT")
        endpoint.set_receive_source("ANT1")
        self.assertEqual(engine.writes, ["120000"])

    def test_cross_manufacturer_pair_without_antenna_routing(self):
        endpoints = []
        for make, model in (("Yaesu", "FTDX101MP"), ("Icom", "IC-7300")):
            path = ROOT / f"{make}-{model}.rmradio"
            radio = DriverRadio(RadioSimulator(model), load_driver(path), path)
            endpoints.append(RadioEndpoint(radio, Receiver.MAIN if radio.is_dual_receiver else None, model))
        coordinator = MirrorCoordinator(tuple(endpoints), lambda *args: None, self.fail)
        coordinator._activate_listener_route(endpoints[1])
        self.assertIsNone(coordinator._listener_pre_mirror_state)
        coordinator._set_both(0, 1853000)
        self.assertEqual([e.read_frequency() for e in endpoints], [1853000, 1853000])
        endpoints[1].set_mode(endpoints[0].read_mode())
        self.assertEqual(endpoints[0].read_mode(), endpoints[1].read_mode())
        coordinator._deactivate_listener_route(endpoints[1])

    def test_all_four_commission_and_tune(self):
        cases = (
            ("Yaesu", "FTDX101MP", "Yaesu-FTDX101MP.rmradio"),
            ("Yaesu", "FTDX5000 / D / MP", "Yaesu-FTDX5000-D-MP.rmradio"),
            ("Icom", "IC-7300", "Icom-IC-7300.rmradio"),
            ("Icom", "IC-7610", "Icom-IC-7610.rmradio"),
        )
        for make, model, filename in cases:
            with self.subTest(model=model):
                path = ROOT / filename
                d = load_driver(path)
                sim = RadioSimulator(model)
                results = test_driver_on_engine(d, sim)
                self.assertTrue(any("Frequency write" in x for x in results))
                radio = DriverRadio(sim, d, path)
                selections = (Receiver.MAIN, Receiver.SUB) if radio.is_dual_receiver else (None,)
                for selection in selections:
                    endpoint = RadioEndpoint(radio, selection, "test")
                    endpoint.set_frequency(1853000)
                    self.assertEqual(endpoint.read_frequency(), 1853000)
                    endpoint.set_mode(OperatingMode.CW)
                    self.assertEqual(endpoint.read_mode(), OperatingMode.CW)
                    self.assertFalse(endpoint.read_transmitting())
                if make == "Icom":
                    mode_writes = [x for x in sim.writes if x.startswith("26")]
                    self.assertTrue(mode_writes[0].endswith("0102"))
                    self.assertTrue(mode_writes[-1].endswith("0002"))

if __name__ == "__main__": unittest.main()
