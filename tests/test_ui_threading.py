import ast
from collections import deque
from pathlib import Path
import sys
import types
import unittest

# The production package installs pyserial.  The test suite uses a tiny serial
# stub so the GUI state tests remain independent of host serial hardware.
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
if "serial.tools" not in sys.modules:
    tools_stub = types.ModuleType("serial.tools")
    list_ports_stub = types.ModuleType("serial.tools.list_ports")
    list_ports_stub.comports = lambda: []
    tools_stub.list_ports = list_ports_stub
    sys.modules["serial.tools"] = tools_stub
    sys.modules["serial.tools.list_ports"] = list_ports_stub

import rigmirror
from radio import TS2000Radio
from rigmirror import (
    DEFAULT_BAUD, DEFAULT_LAYOUT, RigMirrorApp, natural_port_key, tuning_band,
)


class DummyVar:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class DummyCoordinator:
    def __init__(self):
        self.requested = []

    def request_tune(self, frequency_hz):
        self.requested.append(frequency_hz)


class DummyCombo(dict):
    def configure(self, **values):
        self.update(values)


class DummyPort:
    def __init__(self, device):
        self.device = device

class UIThreadingTests(unittest.TestCase):
    def test_outboard_driver_commissioning_is_exposed_and_persisted(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn('text="LOAD MASTER RADIO…"', methods["_config_radio_card"])
        self.assertIn('"LOAD SUB / SLAVE RADIO…"', methods["_config_radio_card"])
        self.assertIn('text="TEST DRIVER"', methods["_config_radio_card"])
        self.assertIn("test_driver_on_engine", methods["_test_driver"])
        self.assertIn("driver_fingerprint", methods["_driver_test_finished"])
        self.assertIn("_driver_test_history", methods["_driver_test_finished"])
        self.assertIn('"drivers":', methods["_save_config"])
        self.assertIn('"driver_test_history":', methods["_save_config"])
        self.assertIn('data.get("drivers"', methods["_load_config"])

    def test_driver_commissioning_is_optional_for_connect_and_apply(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertNotIn("driver_local_status", methods["_connect"])
        self.assertNotIn("driver_local_status", methods["_apply_config"])
        self.assertIn("detect_radio", methods["_connect"])

    def test_outboard_driver_can_be_cleared_per_endpoint(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        clear_source = methods["_clear_driver"]
        self.assertIn("self._driver_paths[index] = None", clear_source)
        self.assertIn("self._driver_data[index] = None", clear_source)
        self.assertIn("self._save_config()", clear_source)

    def test_driver_controls_lock_only_the_affected_endpoint(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("self._connected[index]", methods["_choose_driver"])
        self.assertNotIn("any(self._connected)", methods["_choose_driver"])
        self.assertIn("self._connected[index]", methods["_test_driver"])
        self.assertNotIn("any(self._connected)", methods["_test_driver"])

    def test_port_access_test_is_optional_and_lives_in_diagnostics(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertNotIn('text="TEST PORT"', methods["_config_radio_card"])
        self.assertIn('text="TEST MASTER PORT"', methods["_show_diagnostics"])
        self.assertIn('text="TEST SUB PORT"', methods["_show_diagnostics"])
        self.assertIn("engine.open", methods["_test_port"])
        self.assertNotIn("engine.query", methods["_test_port"])
        self.assertIn("write_test_report", methods["_write_failure_report"])

    def test_worker_callbacks_never_schedule_tk_directly(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        after_callers = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if (isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "after"):
                    after_callers.add(node.name)
        self.assertEqual(after_callers, {"__init__", "_drain_ui_events"})

    def test_worker_callbacks_post_through_queue(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        worker_methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name.endswith("_from_thread")
        }
        self.assertTrue(worker_methods)
        for name, method_source in worker_methods.items():
            if name == "_information_from_thread":
                self.assertNotIn("self.after", method_source)
                self.assertNotIn("_var.get", method_source)
            else:
                self.assertIn("_post_ui", method_source, name)

    def test_start_and_connect_never_enter_micro_automatically(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertNotIn('_set_skin("micro"', methods["_connect_finished"])
        self.assertNotIn('_set_skin("micro"', methods["_toggle_mirror"])

    def test_start_mirror_offers_quick_connect_when_radios_are_disconnected(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("_quick_connect_and_start", methods["_toggle_mirror"])
        self.assertIn("self._connect(index)", methods["_quick_connect_and_start"])
        self.assertIn("self._show_config()", methods["_quick_connect_and_start"])
        self.assertIn("_quick_start_pending", methods["_connect_finished"])
        self.assertIn("_update_mirror_availability", methods["_connect_finished"])

    def test_split_button_is_fixed_and_reuses_same_control_for_return(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn('text="SPLIT +5"', methods["_build_ui"])
        self.assertIn('text="RETURN"', methods["_update_split_display"])
        self.assertIn("split_buttons = (self.split_button, self.micro_split_button)", methods["_update_split_display"])
        self.assertIn("micro_split_button", methods["_build_micro_ui"])
        self.assertIn("self.micro_split_button", methods["_update_split_display"])
        self.assertNotIn("destroy", methods["_update_split_display"])

    def test_windows_and_mac_launchers_are_bundled(self):
        root = Path(__file__).parents[1]
        self.assertTrue((root / "RUN_RIGMIRROR.bat").is_file())
        self.assertTrue((root / "INSTALL_REQUIREMENTS.bat").is_file())
        run_mac = (root / "RUN_RIGMIRROR.command").read_text(encoding="utf-8")
        install_mac = (root / "INSTALL_REQUIREMENTS.command").read_text(encoding="utf-8")
        self.assertIn("python3 rigmirror.py", run_mac)
        self.assertIn("python3 -m pip install -r requirements.txt", install_mac)

    def test_split_and_memory_are_mutually_exclusive(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("_abandon_memory_at", methods["_toggle_split"])
        self.assertIn("self._split_active", methods["_toggle_memory"])
        self.assertIn('self.memory_button.configure(state="disabled")', methods["_update_split_display"])

    def test_fresh_install_defaults_to_two_radios_at_57600(self):
        self.assertEqual(DEFAULT_LAYOUT, "two")
        self.assertEqual(DEFAULT_BAUD, 57_600)

    def test_normal_and_fast_polling_are_exposed_and_persisted(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        self.assertIn('values=("NORMAL • 100 ms", "FAST • 50 ms")', source)
        self.assertIn('"polling": self.polling_var.get()', source)
        self.assertIn("Fast may overload slower or older CAT interfaces.", source)

    def test_shared_layout_disables_routing_and_hides_its_config_panel(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn('routing_enabled = self.layout_var.get() == "two"', methods["_try_start_coordinator"])
        self.assertIn("routing_enabled=routing_enabled", methods["_try_start_coordinator"])
        self.assertIn("self.routing_frame.grid_remove()", methods["_layout_changed"])
        self.assertIn('self.role_vars[index].set("MASTER" if is_master else "SUB")', methods["_update_roles"])

    def test_config_lower_panels_are_equal_and_display_precedes_routing(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        build = source[source.index("options = ttk.Frame"):source.index("def _config_radio_card")]
        self.assertIn('options.columnconfigure(0, weight=1, uniform="options")', build)
        self.assertIn('options.columnconfigure(1, weight=1, uniform="options")', build)
        self.assertLess(build.index("display = ttk.Frame"), build.index("routing = ttk.Frame"))
        self.assertIn('display.grid(row=0, column=0', build)
        self.assertIn('routing.grid(row=0, column=1', build)

    def test_micro_skin_uses_compact_geometry(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_set_skin")
        segment = ast.get_source_segment(source, method) or ""
        self.assertIn('self.geometry("450x126")', segment)
        self.assertIn('self.root_frame.configure(padding=4)', segment)

    def test_micro_skin_omits_redundant_direction_arrow(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_build_micro_ui"
        )
        segment = ast.get_source_segment(source, method) or ""
        self.assertNotIn("→", segment)

    def test_sub_alignment_has_separate_config_screen_and_persistence(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn('text="SUB ALIGNMENT…"', source)
        self.assertIn('text="SUB VFO ALIGNMENT"', source)
        self.assertIn("_build_alignment_ui", methods)
        self.assertIn("request_sub_frequency", methods["_preview_alignment_on_sub"])
        self.assertIn('"alignment_profiles": self._alignment_profiles', methods["_save_config"])
        self.assertIn('data.get("alignment_profiles", {})', methods["_load_config"])
        self.assertIn(
            "Select the required band on the MASTER radio before adjusting alignment.",
            methods["_build_alignment_ui"],
        )

    def test_alignment_action_is_in_config_header_not_display_panel(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        build = source[source.index("def _build_config_ui"):source.index("def _build_alignment_ui")]
        alignment = build.index('text="SUB ALIGNMENT…"')
        diagnostics = build.index('text="DIAGNOSTICS"')
        display = build.index("display = ttk.Frame")
        self.assertLess(alignment, diagnostics)
        self.assertLess(alignment, display)

    def test_sub_routing_choices_follow_detected_sub_without_coordinator(self):
        app = RigMirrorApp.__new__(RigMirrorApp)
        app.layout_var = DummyVar("two")
        app.radios = [None, TS2000Radio(None)]
        app.config_tx_combo = DummyCombo()
        app.config_action_combo = DummyCombo()
        app.config_tx_var = DummyVar("RX ANT")
        app.config_action_var = DummyVar("SWITCH ANTENNA / RX PORT")
        app.tx_source_var = DummyVar("RX ANT")
        app.tx_action_var = DummyVar("SWITCH ANTENNA / RX PORT")
        app.safety_ack_var = DummyVar(True)
        app._protection_action_changed = lambda: None

        app._refresh_routing_choices()

        self.assertEqual(app.config_tx_combo["values"], ("ANT1", "ANT2"))
        self.assertEqual(
            app.config_action_combo["values"],
            ("NO CHANGE", "SWITCH ANTENNA / RX PORT", "PARKING FREQUENCY"),
        )
        self.assertEqual(app.config_tx_var.get(), "ANT1")
        self.assertEqual(app.tx_source_var.get(), "RX ANT")

    def test_port_order_is_numeric_for_default_left_and_right_selection(self):
        ports = ["COM10", "COM2", "COM8"]
        self.assertEqual(sorted(ports, key=natural_port_key), ["COM2", "COM8", "COM10"])

    def test_refresh_assigns_lowest_two_ports_without_overwriting_saved_port(self):
        original = rigmirror.list_ports.comports
        rigmirror.list_ports.comports = lambda: [DummyPort("COM10"), DummyPort("COM8"), DummyPort("COM2")]
        try:
            app = RigMirrorApp.__new__(RigMirrorApp)
            app.cards = [{"port": DummyCombo()}, {"port": DummyCombo()}]
            app.port_vars = [DummyVar(""), DummyVar("")]
            app._refresh_ports()
            self.assertEqual([value.get() for value in app.port_vars], ["COM2", "COM8"])

            app.port_vars = [DummyVar("COM10"), DummyVar("")]
            app._refresh_ports()
            self.assertEqual([value.get() for value in app.port_vars], ["COM10", "COM2"])
        finally:
            rigmirror.list_ports.comports = original

    def test_operating_card_has_no_connection_controls(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertNotIn("Combobox", methods["_radio_card"])
        self.assertIn("Combobox", methods["_config_radio_card"])
        self.assertNotIn('text="DIAGNOSTICS"', methods["_build_ui"])
        self.assertNotIn('text="OPTIONS"', methods["_build_ui"])
        self.assertIn('text="CONFIG"', methods["_build_ui"])

    def test_diagnostics_is_a_separate_bounded_capture_window(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("Toplevel", methods["_show_diagnostics"])
        self.assertIn("asksaveasfilename", methods["_save_diagnostics"])
        self.assertNotIn("self.console", methods["_build_config_ui"])

        app = RigMirrorApp.__new__(RigMirrorApp)
        app._diagnostics_lines = deque(maxlen=3)
        app._diagnostics_window = None
        for line in ("one\n", "two\n", "three\n", "four\n"):
            app._append_console(line)
        self.assertEqual(list(app._diagnostics_lines), ["two\n", "three\n", "four\n"])

    def test_config_connection_cards_have_live_status_lamps(self):
        source = (Path(__file__).parents[1] / "rigmirror.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("config_lamp", methods["_config_radio_card"])
        self.assertIn("config_lamp", methods["_set_lamp"])

    def test_authoritative_master_frequency_replaces_stale_wheel_base(self):
        app = RigMirrorApp.__new__(RigMirrorApp)
        app._frequency_hz = [3_784_980, 3_784_980]
        app._wheel_frequency = 3_784_980
        app._wheel_request_pending = None
        app._memory_armed = False
        app._transmitting = False
        app.master_var = DummyVar(0)
        app.freq_vars = [DummyVar(), DummyVar()]
        app.step_var = DummyVar(10)
        app.coordinator = DummyCoordinator()
        app._connected = [True, True]

        app._frequency_update(0, 14_200_000)
        self.assertEqual(app._wheel_frequency, 14_200_000)
        app._tune_from_wheel(1, 0)
        self.assertEqual(app.coordinator.requested, [14_200_010])

    def test_band_change_abandons_old_memory_without_returning_home(self):
        app = RigMirrorApp.__new__(RigMirrorApp)
        app._frequency_hz = [3_784_980, 3_784_980]
        app._wheel_frequency = 3_784_980
        app._wheel_request_pending = None
        app._memory_armed = True
        app._transmitting = False
        app.master_var = DummyVar(0)
        app.freq_vars = [DummyVar(), DummyVar()]
        abandoned = []
        app._abandon_memory_at = abandoned.append

        app._frequency_update(0, 14_200_000)
        self.assertEqual(abandoned, [14_200_000])

    def test_small_same_band_change_keeps_memory_armed(self):
        self.assertEqual(tuning_band(3_784_980), tuning_band(3_785_000))
        self.assertNotEqual(tuning_band(3_784_980), tuning_band(14_200_000))


if __name__ == "__main__":
    unittest.main()
