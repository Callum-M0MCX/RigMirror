from pathlib import Path
import unittest

from driver_runtime import load_driver

from radio import (
    DriverRadio, OperatingMode, RadioEndpoint, Receiver, TS2000Radio, TS590SGRadio,
    TS890Radio, TS990Radio, VFO, detect_radio, directional_step,
    format_frequency,
)


DRIVERS = Path(__file__).parents[1] / "drivers"


class FakeEngine:
    def __init__(self):
        self.replies = {
            "ID;": "ID022;", "FA;": "FA00014195000;", "FB;": "FB00007151480;",
            "OM0;": "OM01;", "OM1;": "OM12;", "CB;": "CB0;",
            "AN00;": "AN00100;", "AN01;": "AN01100;",
        }
        self.set_commands = []
        self.query_commands = []

    def query(self, command):
        self.query_commands.append(command)
        return self.replies[command]

    def set(self, command):
        self.set_commands.append(command)


class Fake590Engine(FakeEngine):
    def __init__(self):
        super().__init__()
        if_reply = list("0" * 40)
        if_reply[0:2] = "IF"
        if_reply[28] = "1"
        if_reply[39] = ";"
        self.replies.update({
            "ID;": "ID023;", "FR;": "FR1;", "IF;": "".join(if_reply),
            "MD;": "MD1;", "AN;": "AN100;",
        })


class Fake2000Engine(FakeEngine):
    def __init__(self):
        super().__init__()
        if_reply = list("0" * 38)
        if_reply[0:2] = "IF"
        if_reply[28] = "1"
        if_reply[37] = ";"
        self.replies.update({
            "ID;": "ID019;", "FR;": "FR0;", "MD;": "MD2;",
            "IF;": "".join(if_reply), "AN;": "AN0;",
        })


class Fake890Engine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.is_open = True
        self.replies.update({
            "ID;": "ID024;", "FR;": "FR1;", "OM1;": "OM1D;",
            "AN;": "AN2101;",
        })


class RadioTests(unittest.TestCase):
    def test_endpoint_reads_and_writes_selected_receiver(self):
        engine = FakeEngine()
        radio = TS990Radio(engine)
        endpoint = RadioEndpoint(radio, Receiver.SUB, "Radio 2")
        self.assertEqual(radio.identify().model, "Kenwood TS-990S")
        self.assertEqual(endpoint.read_frequency(), 7_151_480)
        endpoint.set_frequency(14_195_000)
        self.assertEqual(engine.set_commands, ["FB00014195000;"])

    def test_display_format(self):
        self.assertEqual(format_frequency(14_195_000), "14.195.000")
        self.assertEqual(format_frequency(7_151_480), "7.151.480")

    def test_ts590sg_follows_active_vfo_for_read_and_write(self):
        engine = Fake590Engine()
        radio = detect_radio(engine)
        self.assertIsInstance(radio, TS590SGRadio)
        endpoint = RadioEndpoint(radio, None, "Radio 2")
        self.assertEqual(endpoint.read_frequency(), 7_151_480)
        endpoint.set_frequency(14_195_003)
        self.assertEqual(engine.set_commands, ["FB00014195003;"])
        self.assertEqual(radio.read_active_vfo(), VFO.B)
        self.assertTrue(radio.read_transmitting())
        self.assertEqual(endpoint.read_mode(), OperatingMode.LSB)
        endpoint.set_mode(OperatingMode.USB)
        self.assertEqual(engine.set_commands[-1], "MD2;")

    def test_default_fallback_accepts_only_ts590sg(self):
        with self.assertRaisesRegex(Exception, "Load the correct .rmradio driver"):
            detect_radio(FakeEngine())

    def test_ts2000_outboard_driver_follows_main_active_vfo(self):
        engine = Fake2000Engine()
        path = DRIVERS / "Kenwood-TS-2000.rmradio"
        radio = DriverRadio(engine, load_driver(path), path)
        radio.identify()
        self.assertEqual(radio.identity.model, "Kenwood TS-2000")
        endpoint = RadioEndpoint(radio, None, "Sub")
        self.assertEqual(endpoint.read_frequency(), 14_195_000)
        endpoint.set_frequency(7_151_480)
        self.assertEqual(engine.set_commands, ["FA00007151480;"])
        self.assertEqual(endpoint.read_mode(), OperatingMode.USB)
        endpoint.set_mode(OperatingMode.LSB)
        self.assertEqual(engine.set_commands[-1], "MD1;")
        self.assertTrue(endpoint.read_transmitting())

    def test_ts2000_uses_zero_based_ant1_ant2_mapping_and_restores(self):
        engine = Fake2000Engine()
        endpoint = RadioEndpoint(TS2000Radio(engine), None, "Sub")
        self.assertEqual(endpoint.supported_receive_sources(), ("ANT1", "ANT2"))
        original = endpoint.read_antenna_state()
        self.assertEqual(original.active_source, "ANT1")
        endpoint.set_receive_source("ANT2")
        endpoint.restore_antenna_state(original)
        self.assertEqual(engine.set_commands, ["AN1;", "AN0;"])

    def test_ts2000_rejects_memory_or_call_instead_of_tuning_wrong_vfo(self):
        engine = Fake2000Engine()
        endpoint = RadioEndpoint(TS2000Radio(engine), None, "Master")
        for response in ("FR2;", "FR3;"):
            with self.subTest(response=response):
                engine.replies["FR;"] = response
                with self.assertRaisesRegex(Exception, "not in VFO mode"):
                    endpoint.read_frequency()

    def test_ts990_mode_targets_receiver_and_restores_control_band(self):
        engine = FakeEngine()
        endpoint = RadioEndpoint(TS990Radio(engine), Receiver.SUB, "Sub")
        self.assertEqual(endpoint.read_mode(), OperatingMode.USB)
        endpoint.set_mode(OperatingMode.USB)
        self.assertEqual(engine.set_commands, ["CB1;", "OM12;", "CB0;"])

    def test_ts590_listener_route_preserves_auxiliary_state(self):
        engine = Fake590Engine()
        endpoint = RadioEndpoint(TS590SGRadio(engine), None, "Sub")
        original = endpoint.read_antenna_state()
        self.assertEqual(original.active_source, "ANT1")
        endpoint.set_receive_source("ANT2")
        endpoint.restore_antenna_state(original)
        self.assertEqual(
            engine.set_commands,
            ["AN299;", "AN909;", "AN199;", "AN909;"],
        )

    def test_ts590_restore_uses_independent_proven_setter_forms(self):
        engine = Fake590Engine()
        endpoint = RadioEndpoint(TS590SGRadio(engine), None, "Sub")

        endpoint.restore_antenna_state(endpoint.read_antenna_state())
        self.assertEqual(engine.set_commands, ["AN199;", "AN909;"])

        engine.set_commands.clear()
        engine.replies["AN;"] = "AN210;"
        endpoint.restore_antenna_state(endpoint.read_antenna_state())
        self.assertEqual(engine.set_commands, ["AN299;", "AN919;"])

    def test_ts590_listener_route_uses_proven_one_based_antenna_mapping(self):
        engine = Fake590Engine()
        endpoint = RadioEndpoint(TS590SGRadio(engine), None, "Sub")
        endpoint.set_receive_source("ANT1")
        endpoint.set_receive_source("ANT2")
        endpoint.set_receive_source("RX ANT")
        self.assertEqual(
            engine.set_commands,
            ["AN199;", "AN909;", "AN299;", "AN909;", "AN919;"],
        )

        engine.replies["AN;"] = "AN200;"
        self.assertEqual(endpoint.read_antenna_state().active_source, "ANT2")
        engine.replies["AN;"] = "AN110;"
        self.assertEqual(endpoint.read_antenna_state().active_source, "RX ANT")

    def test_ts590_antenna_connector_diversion_explicitly_disables_rx_antenna(self):
        engine = Fake590Engine()
        engine.replies["AN;"] = "AN110;"
        endpoint = RadioEndpoint(TS590SGRadio(engine), None, "Sub")

        endpoint.set_receive_source("ANT2")
        self.assertEqual(engine.set_commands, ["AN299;", "AN909;"])

    def test_ts890_outboard_driver_follows_active_vfo_and_uses_om_mode(self):
        engine = Fake890Engine()
        path = DRIVERS / "Kenwood-TS-890S.rmradio"
        radio = DriverRadio(engine, load_driver(path), path)
        radio.identify()
        endpoint = RadioEndpoint(radio, None, "Master")
        self.assertEqual(endpoint.read_frequency(), 7_151_480)
        endpoint.set_frequency(14_195_003)
        self.assertEqual(endpoint.read_mode(), OperatingMode.USB)
        endpoint.set_mode(OperatingMode.LSB)
        self.assertEqual(engine.set_commands, ["FB00014195003;", "OM01;"])

    def test_ts890_auto_information_and_antenna_routing(self):
        engine = Fake890Engine()
        radio = TS890Radio(engine)
        radio.enable_auto_information()
        endpoint = RadioEndpoint(radio, None, "Sub")
        original = endpoint.read_antenna_state()
        self.assertEqual(original.active_source, "RX ANT")
        self.assertEqual(original.auxiliary, "01")
        endpoint.set_receive_source("ANT1")
        endpoint.set_receive_source("ANT2")
        endpoint.set_receive_source("RX ANT")
        endpoint.restore_antenna_state(original)
        radio.disable_auto_information()
        self.assertEqual(
            engine.set_commands,
            ["AI2;", "AN1099;", "AN2099;", "AN9199;", "AN2101;", "AI0;"],
        )

    def test_physical_ts990_sub_route_queries_its_main_receiver_directly(self):
        engine = FakeEngine()
        endpoint = RadioEndpoint(TS990Radio(engine), Receiver.MAIN, "Sub")
        original = endpoint.read_antenna_state()
        self.assertEqual(original.active_source, "ANT1")
        endpoint.set_receive_source("RX ANT")
        endpoint.restore_antenna_state(original)
        self.assertEqual(
            engine.set_commands,
            ["AN00919;", "AN00100;"],
        )

    def test_ts990_sub_receiver_uses_an01_without_control_band_switching(self):
        engine = FakeEngine()
        endpoint = RadioEndpoint(TS990Radio(engine), Receiver.SUB, "Sub")
        self.assertEqual(endpoint.read_antenna_state().active_source, "ANT1")
        self.assertEqual(engine.set_commands, [])

    def test_all_directional_steps(self):
        cases = (
            (1000, 7_151_000, 7_152_000),
            (500, 7_151_000, 7_151_500),
            (100, 7_151_400, 7_151_500),
            (10, 7_151_470, 7_151_490),
        )
        for step, down, up in cases:
            with self.subTest(step=step):
                self.assertEqual(directional_step(7_151_480, step, -1), down)
                self.assertEqual(directional_step(7_151_480, step, 1), up)
        self.assertEqual(directional_step(7_151_485, 10, -1), 7_151_480)
        self.assertEqual(directional_step(7_151_485, 10, 1), 7_151_490)


if __name__ == "__main__":
    unittest.main()
