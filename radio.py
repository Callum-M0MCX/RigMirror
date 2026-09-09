"""Built-in compatibility radios plus data-driven endpoint helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING, Union

from driver_runtime import OperationRunner, driver_label
from errors import CATError

if TYPE_CHECKING:
    from cat_engine import CATEngine


TS990_ID = "ID022;"
TS590SG_ID = "ID023;"
TS890_ID = "ID024;"
TS2000_ID = "ID019;"
MIN_FREQUENCY_HZ = 30_000
MAX_FREQUENCY_HZ = 60_000_000


class Receiver(str, Enum):
    MAIN = "MAIN"
    SUB = "SUB"

    @property
    def command(self) -> str:
        return "FA" if self is Receiver.MAIN else "FB"


class VFO(str, Enum):
    A = "VFO A"
    B = "VFO B"

    @property
    def command(self) -> str:
        return "FA" if self is VFO.A else "FB"


class OperatingMode(str, Enum):
    LSB = "1"
    USB = "2"
    CW = "3"
    FM = "4"
    AM = "5"
    FSK = "6"
    CW_R = "7"
    FSK_R = "9"

    @property
    def label(self) -> str:
        return {
            "1": "LSB", "2": "USB", "3": "CW", "4": "FM",
            "5": "AM", "6": "FSK", "7": "CW-R", "9": "FSK-R",
        }[self.value]


def _common_mode(code: str) -> OperatingMode:
    """Reduce TS-990 data/PSK variants to a mode both radios understand."""
    aliases = {
        "A": "2", "B": "2",  # PSK / PSK-R: best common listening mode
        "C": "1", "G": "1", "K": "1",  # LSB-D1/D2/D3
        "D": "2", "H": "2", "L": "2",  # USB-D1/D2/D3
        "E": "4", "I": "4", "M": "4",  # FM-D1/D2/D3
        "F": "5", "J": "5", "N": "5",  # AM-D1/D2/D3
    }
    try:
        return OperatingMode(aliases.get(code, code))
    except ValueError as exc:
        raise CATError(f"Unsupported operating mode code {code!r}.") from exc


@dataclass(frozen=True, slots=True)
class RadioIdentity:
    model: str
    cat_id: str


@dataclass(frozen=True, slots=True)
class AntennaState:
    connector: str
    rx_antenna: bool
    auxiliary: str

    @property
    def active_source(self) -> str:
        return "RX ANT" if self.rx_antenna else self.connector


@dataclass(frozen=True, slots=True)
class MemoryTransmitPlan:
    """Saved radio state while M uses a separate receive/transmit VFO pair."""

    receive_vfo: VFO
    original_transmit_vfo: VFO
    prepared_transmit_vfo: VFO
    original_prepared_frequency: int


def _parse_frequency(reply: str, prefix: str) -> int:
    if not reply.startswith(prefix) or not reply.endswith(";"):
        raise CATError(f"Unexpected {prefix} response: {reply!r}")
    digits = reply[2:-1]
    if len(digits) != 11 or not digits.isdigit():
        raise CATError(f"Invalid {prefix} frequency response: {reply!r}")
    return int(digits)


def _validate_frequency(frequency_hz: int, model: str) -> None:
    if not MIN_FREQUENCY_HZ <= frequency_hz <= MAX_FREQUENCY_HZ:
        raise CATError(f"Frequency {frequency_hz} Hz is outside {model} range.")


class TS990Radio:
    identity = RadioIdentity("Kenwood TS-990S", TS990_ID)

    def __init__(self, engine: CATEngine) -> None:
        self.engine = engine

    def identify(self) -> RadioIdentity:
        reply = self.engine.query("ID;")
        if reply != TS990_ID:
            raise CATError(f"Expected TS-990S ({TS990_ID}), received {reply!r}.")
        return self.identity

    def enable_auto_information(self) -> None:
        self.engine.set("AI2;")

    def disable_auto_information(self) -> None:
        if self.engine.is_open:
            self.engine.set("AI0;")

    def read_frequency(self, receiver: Receiver) -> int:
        prefix = receiver.command
        return _parse_frequency(self.engine.query(prefix + ";"), prefix)

    def set_frequency(self, receiver: Receiver, frequency_hz: int) -> None:
        _validate_frequency(frequency_hz, "TS-990S")
        self.engine.set(f"{receiver.command}{frequency_hz:011d};")

    def read_mode(self, receiver: Receiver) -> OperatingMode:
        band = "0" if receiver is Receiver.MAIN else "1"
        reply = self.engine.query(f"OM{band};")
        if len(reply) != 5 or not reply.startswith(f"OM{band}") or not reply.endswith(";"):
            raise CATError(f"Unexpected TS-990S OM response: {reply!r}")
        return _common_mode(reply[3])

    def set_mode(self, receiver: Receiver, mode: OperatingMode) -> None:
        """OM sets the current operating band, so select and restore it."""
        target = "0" if receiver is Receiver.MAIN else "1"
        reply = self.engine.query("CB;")
        if reply not in ("CB0;", "CB1;"):
            raise CATError(f"Unexpected TS-990S CB response: {reply!r}")
        original = reply[2]
        if original != target:
            self.engine.set(f"CB{target};")
        try:
            self.engine.set(f"OM{target}{mode.value};")
        finally:
            if original != target:
                self.engine.set(f"CB{original};")

    def read_antenna_state(self, receiver: Receiver) -> AntennaState:
        target = "0" if receiver is Receiver.MAIN else "1"
        reply = self.engine.query(f"AN0{target};")
        if len(reply) != 8 or not reply.startswith("AN0") or not reply.endswith(";"):
            raise CATError(f"Unexpected TS-990S AN0 response: {reply!r}")
        if reply[3] not in "01" or reply[4] not in "1234" or reply[5] not in "01":
            raise CATError(f"Invalid TS-990S antenna state: {reply!r}")
        if reply[3] != target:
            raise CATError(f"TS-990S returned antenna state for the wrong receiver: {reply!r}")
        return AntennaState(f"ANT{reply[4]}", reply[5] == "1", reply[6])

    def set_receive_source(self, receiver: Receiver, source: str) -> None:
        band = "0" if receiver is Receiver.MAIN else "1"
        if source == "RX ANT":
            self.engine.set(f"AN0{band}919;")
            return
        if source not in ("ANT1", "ANT2", "ANT3", "ANT4"):
            raise CATError(f"Unsupported TS-990S receive source {source!r}.")
        self.engine.set(f"AN0{band}{source[-1]}09;")

    def restore_antenna_state(self, receiver: Receiver, state: AntennaState) -> None:
        band = "0" if receiver is Receiver.MAIN else "1"
        rx = "1" if state.rx_antenna else "0"
        self.engine.set(f"AN0{band}{state.connector[-1]}{rx}{state.auxiliary};")


class TS2000Radio:
    """TS-2000 main HF/6 m receiver and its active VFO A/B."""

    identity = RadioIdentity("Kenwood TS-2000", TS2000_ID)

    def __init__(self, engine: CATEngine) -> None:
        self.engine = engine

    def identify(self) -> RadioIdentity:
        reply = self.engine.query("ID;")
        if reply != TS2000_ID:
            raise CATError(f"Expected TS-2000 ({TS2000_ID}), received {reply!r}.")
        return self.identity

    def enable_auto_information(self) -> None:
        # Poll the required state explicitly.  AI2 on a TS-2000 can produce
        # substantial unsolicited traffic which is unnecessary here.
        self.engine.set("AI0;")

    def disable_auto_information(self) -> None:
        if self.engine.is_open:
            self.engine.set("AI0;")

    def read_active_vfo(self) -> VFO:
        reply = self.engine.query("FR;")
        if reply == "FR0;":
            return VFO.A
        if reply == "FR1;":
            return VFO.B
        if reply in ("FR2;", "FR3;"):
            raise CATError("TS-2000 is not in VFO mode; select VFO A or VFO B on Main.")
        raise CATError(f"Unexpected TS-2000 FR response: {reply!r}")

    def read_frequency(self, vfo: Optional[VFO] = None) -> tuple[int, VFO]:
        active = vfo or self.read_active_vfo()
        return _parse_frequency(self.engine.query(active.command + ";"), active.command), active

    def set_frequency(self, frequency_hz: int) -> VFO:
        _validate_frequency(frequency_hz, "TS-2000")
        active = self.read_active_vfo()
        self.engine.set(f"{active.command}{frequency_hz:011d};")
        return active

    def read_mode(self) -> OperatingMode:
        reply = self.engine.query("MD;")
        if len(reply) != 4 or not reply.startswith("MD") or not reply.endswith(";"):
            raise CATError(f"Unexpected TS-2000 MD response: {reply!r}")
        return _common_mode(reply[2])

    def set_mode(self, mode: OperatingMode) -> None:
        self.engine.set(f"MD{mode.value};")

    def read_transmitting(self) -> bool:
        """Read Main PTT state from the standard P8 field in IF."""
        reply = self.engine.query("IF;")
        if not reply.startswith("IF") or not reply.endswith(";") or len(reply) < 30:
            raise CATError(f"Unexpected TS-2000 IF response: {reply!r}")
        state = reply[28]
        if state not in "01":
            raise CATError(f"Invalid TS-2000 TX state in IF response: {reply!r}")
        return state == "1"

    def read_antenna_state(self) -> AntennaState:
        reply = self.engine.query("AN;")
        if reply == "AN0;":
            return AntennaState("ANT1", False, "0")
        if reply == "AN1;":
            return AntennaState("ANT2", False, "0")
        raise CATError(f"Unexpected TS-2000 AN response: {reply!r}")

    def set_receive_source(self, source: str) -> None:
        if source == "ANT1":
            self.engine.set("AN0;")
        elif source == "ANT2":
            self.engine.set("AN1;")
        else:
            raise CATError(f"Unsupported TS-2000 receive source {source!r}.")

    def restore_antenna_state(self, state: AntennaState) -> None:
        if state.rx_antenna or state.connector not in ("ANT1", "ANT2"):
            raise CATError(f"Invalid saved TS-2000 antenna state: {state!r}")
        self.engine.set("AN0;" if state.connector == "ANT1" else "AN1;")


class TS590SGRadio:
    identity = RadioIdentity("Kenwood TS-590SG", TS590SG_ID)

    def __init__(self, engine: CATEngine) -> None:
        self.engine = engine

    def identify(self) -> RadioIdentity:
        reply = self.engine.query("ID;")
        if reply != TS590SG_ID:
            raise CATError(f"Expected TS-590SG ({TS590SG_ID}), received {reply!r}.")
        return self.identity

    def enable_auto_information(self) -> None:
        # Active VFO/frequency and PTT are explicitly polled.  Leaving AI on
        # would add redundant replies to the serial queue after every write.
        self.engine.set("AI0;")

    def disable_auto_information(self) -> None:
        if self.engine.is_open:
            self.engine.set("AI0;")

    def read_active_vfo(self) -> VFO:
        reply = self.engine.query("FR;")
        if reply == "FR0;":
            return VFO.A
        if reply == "FR1;":
            return VFO.B
        if reply == "FR2;":
            raise CATError("TS-590SG is in Memory mode; select VFO A or VFO B.")
        raise CATError(f"Unexpected TS-590SG FR response: {reply!r}")

    def read_frequency(self, vfo: Optional[VFO] = None) -> tuple[int, VFO]:
        active = vfo or self.read_active_vfo()
        return _parse_frequency(self.engine.query(active.command + ";"), active.command), active

    def set_frequency(self, frequency_hz: int) -> VFO:
        _validate_frequency(frequency_hz, "TS-590SG")
        active = self.read_active_vfo()
        self.engine.set(f"{active.command}{frequency_hz:011d};")
        return active

    def read_mode(self) -> OperatingMode:
        reply = self.engine.query("MD;")
        if len(reply) != 4 or not reply.startswith("MD") or not reply.endswith(";"):
            raise CATError(f"Unexpected TS-590SG MD response: {reply!r}")
        return _common_mode(reply[2])

    def set_mode(self, mode: OperatingMode) -> None:
        self.engine.set(f"MD{mode.value};")

    def read_transmitting(self) -> bool:
        """Read PTT state from P8 in the TS-590SG IF response."""
        reply = self.engine.query("IF;")
        if not reply.startswith("IF") or not reply.endswith(";") or len(reply) < 30:
            raise CATError(f"Unexpected TS-590SG IF response: {reply!r}")
        state = reply[28]
        if state not in "01":
            raise CATError(f"Invalid TS-590SG TX state in IF response: {reply!r}")
        return state == "1"

    def read_antenna_state(self) -> AntennaState:
        reply = self.engine.query("AN;")
        if len(reply) != 6 or not reply.startswith("AN") or not reply.endswith(";"):
            raise CATError(f"Unexpected TS-590SG AN response: {reply!r}")
        if reply[2] not in "12" or reply[3] not in "01":
            raise CATError(f"Invalid TS-590SG antenna state: {reply!r}")
        return AntennaState(f"ANT{reply[2]}", reply[3] == "1", reply[4])

    def set_receive_source(self, source: str) -> None:
        if source == "RX ANT":
            self.engine.set("AN919;")
        elif source == "ANT1":
            self.engine.set("AN199;")
            self.engine.set("AN909;")
        elif source == "ANT2":
            self.engine.set("AN299;")
            self.engine.set("AN909;")
        else:
            raise CATError(f"Unsupported TS-590SG receive source {source!r}.")

    def restore_antenna_state(self, state: AntennaState) -> None:
        connector = state.connector[-1]
        if connector not in "12" or state.auxiliary not in "01":
            raise CATError(f"Invalid saved TS-590SG antenna state: {state!r}")

        # The TS-590SG reports the complete state as (for example) AN100,
        # but its reliable setter forms use 9 as "leave unchanged".  Restore
        # the physical connector and RX ANT flag independently using the forms
        # proven on real hardware.  Sending the query-shaped AN100/AN200 form
        # can leave ANT2 selected after an RX ANT diversion.
        self.engine.set(f"AN{connector}99;")
        self.engine.set("AN919;" if state.rx_antenna else "AN909;")


class TS890Radio:
    """TS-890S driver: single receiver, active VFO, AI2 PTT reporting."""

    identity = RadioIdentity("Kenwood TS-890S", TS890_ID)

    def __init__(self, engine: CATEngine) -> None:
        self.engine = engine

    def identify(self) -> RadioIdentity:
        reply = self.engine.query("ID;")
        if reply != TS890_ID:
            raise CATError(f"Expected TS-890S ({TS890_ID}), received {reply!r}.")
        return self.identity

    def enable_auto_information(self) -> None:
        # AI2 reports TX0/TX1/TX2 and RX transitions without retaining the
        # setting after power-off, matching the TS-990 behaviour we rely on.
        self.engine.set("AI2;")

    def disable_auto_information(self) -> None:
        if self.engine.is_open:
            self.engine.set("AI0;")

    def read_active_vfo(self) -> VFO:
        reply = self.engine.query("FR;")
        if reply == "FR0;":
            return VFO.A
        if reply == "FR1;":
            return VFO.B
        if reply == "FR3;":
            raise CATError("TS-890S is in Memory mode; select VFO A or VFO B.")
        raise CATError(f"Unexpected TS-890S FR response: {reply!r}")

    def read_frequency(self, vfo: Optional[VFO] = None) -> tuple[int, VFO]:
        active = vfo or self.read_active_vfo()
        return _parse_frequency(self.engine.query(active.command + ";"), active.command), active

    def set_frequency(self, frequency_hz: int) -> VFO:
        _validate_frequency(frequency_hz, "TS-890S")
        active = self.read_active_vfo()
        self.engine.set(f"{active.command}{frequency_hz:011d};")
        return active

    def read_mode(self) -> OperatingMode:
        active = self.read_active_vfo()
        display = "0" if active is VFO.A else "1"
        reply = self.engine.query(f"OM{display};")
        if len(reply) != 5 or not reply.startswith(f"OM{display}") or not reply.endswith(";"):
            raise CATError(f"Unexpected TS-890S OM response: {reply!r}")
        return _common_mode(reply[3])

    def set_mode(self, mode: OperatingMode) -> None:
        # The TS-890 command guide specifies that P1 is ignored when setting
        # OM; while receiving, the currently selected receive mode is changed.
        self.engine.set(f"OM0{mode.value};")

    def read_antenna_state(self) -> AntennaState:
        reply = self.engine.query("AN;")
        if len(reply) != 7 or not reply.startswith("AN") or not reply.endswith(";"):
            raise CATError(f"Unexpected TS-890S AN response: {reply!r}")
        if reply[2] not in "12" or any(value not in "01" for value in reply[3:6]):
            raise CATError(f"Invalid TS-890S antenna state: {reply!r}")
        return AntennaState(f"ANT{reply[2]}", reply[3] == "1", reply[4:6])

    def set_receive_source(self, source: str) -> None:
        # 9 means "do not change" for the remaining AN parameters.
        if source == "RX ANT":
            self.engine.set("AN9199;")
        elif source == "ANT1":
            self.engine.set("AN1099;")
        elif source == "ANT2":
            self.engine.set("AN2099;")
        else:
            raise CATError(f"Unsupported TS-890S receive source {source!r}.")

    def restore_antenna_state(self, state: AntennaState) -> None:
        connector = state.connector[-1]
        rx = "1" if state.rx_antenna else "0"
        if connector not in "12" or len(state.auxiliary) != 2 or any(
            value not in "01" for value in state.auxiliary
        ):
            raise CATError(f"Invalid saved TS-890S antenna state: {state!r}")
        self.engine.set(f"AN{connector}{rx}{state.auxiliary};")


class DriverRadio:
    """A radio whose CAT vocabulary and receiver topology come from .rmradio data."""

    def __init__(self, engine: CATEngine, driver: dict[str, Any], path: str | Path) -> None:
        self.engine = engine
        self.driver = driver
        self.driver_path = Path(path)
        self.runner = OperationRunner(driver, engine)
        metadata = driver["metadata"]
        expected = driver["capabilities"]["identify"].get("response_equals", "")
        self.identity = RadioIdentity(driver_label(driver), expected)
        self.topology = driver.get("topology", {"type": "active_vfo"})

    @property
    def is_dual_receiver(self) -> bool:
        return self.topology.get("type", "active_vfo") == "dual_receiver"

    def identify(self) -> RadioIdentity:
        model, _reply = self.runner.run("identify")
        if str(model) != self.identity.model:
            raise CATError(
                f"Driver expected {self.identity.model!r}, interpreted {model!r}."
            )
        return self.identity

    def enable_auto_information(self) -> None:
        for command in self.driver["transport"].get("initialise_commands", ["AI0;"]):
            self.engine.set(command)

    def disable_auto_information(self) -> None:
        if self.engine.is_open:
            for command in self.driver["transport"].get("shutdown_commands", ["AI0;"]):
                self.engine.set(command)

    def read_active_vfo(self) -> VFO:
        if self.topology.get("type") == "selected_vfo":
            return VFO.A  # Internal endpoint token; no claim about physical A/B.
        selected, _reply = self.runner.run("active_vfo")
        if selected == VFO.A.value:
            return VFO.A
        if selected == VFO.B.value:
            return VFO.B
        raise CATError(f"Radio is not in VFO A or VFO B; driver reports {selected!r}.")

    def read_frequency(self, selection: Optional[VFO | Receiver] = None):
        if self.is_dual_receiver:
            receiver = selection if isinstance(selection, Receiver) else Receiver.MAIN
            frequency, _reply = self.runner.run(
                "frequency_read", selector_values={"receiver": receiver.value}
            )
            return int(frequency)
        vfo = selection if isinstance(selection, VFO) else None
        active = vfo or self.read_active_vfo()
        frequency, _reply = self.runner.run(
            "frequency_read", selector_values={"active_vfo": active.value}
        )
        return int(frequency), active

    def set_frequency(self, *args):
        if self.is_dual_receiver:
            if len(args) != 2 or not isinstance(args[0], Receiver):
                raise CATError("Dual-receiver driver requires receiver and frequency.")
            receiver, frequency_hz = args
            _validate_frequency(int(frequency_hz), self.identity.model)
            self.runner.run(
                "frequency_write", int(frequency_hz), {"receiver": receiver.value}
            )
            return receiver
        if len(args) != 1:
            raise CATError("Active-VFO driver requires one frequency.")
        frequency_hz = int(args[0])
        _validate_frequency(frequency_hz, self.identity.model)
        active = self.read_active_vfo()
        self.runner.run(
            "frequency_write", frequency_hz, {"active_vfo": active.value}
        )
        return active

    def read_transmit_frequency(self, receiver: Optional[Receiver] = None) -> int:
        if "tx_frequency_read" not in self.driver["capabilities"]:
            value = self.read_frequency(receiver)
            return int(value[0] if isinstance(value, tuple) else value)
        selectors = {"receiver": (receiver or Receiver.MAIN).value} if self.is_dual_receiver else None
        frequency, _reply = self.runner.run("tx_frequency_read", selector_values=selectors)
        return int(frequency)

    def set_transmit_frequency(self, frequency_hz: int, receiver: Optional[Receiver] = None) -> None:
        _validate_frequency(int(frequency_hz), self.identity.model)
        if "tx_frequency_write" not in self.driver["capabilities"]:
            if self.is_dual_receiver:
                self.set_frequency(receiver or Receiver.MAIN, frequency_hz)
            else:
                self.set_frequency(frequency_hz)
            return
        selectors = {"receiver": (receiver or Receiver.MAIN).value} if self.is_dual_receiver else None
        self.runner.run("tx_frequency_write", int(frequency_hz), selectors)

    def prepare_memory_transmit(self, frequency_hz: int) -> Optional[MemoryTransmitPlan]:
        """Prepare an inactive VFO for TX before PTT, where the driver supports it."""
        capabilities = self.driver["capabilities"]
        if (self.is_dual_receiver or self.topology.get("type") != "active_vfo"
                or "transmit_vfo" not in capabilities
                or "transmit_vfo_write" not in capabilities):
            return None
        _validate_frequency(int(frequency_hz), self.identity.model)
        receive_vfo = self.read_active_vfo()
        transmit_label, _reply = self.runner.run("transmit_vfo")
        try:
            original_transmit_vfo = VFO(str(transmit_label))
        except ValueError as exc:
            raise CATError(f"Driver returned invalid transmit VFO {transmit_label!r}.") from exc
        prepared_vfo = VFO.B if receive_vfo is VFO.A else VFO.A
        original_frequency, _reply = self.runner.run(
            "frequency_read", selector_values={"active_vfo": prepared_vfo.value}
        )
        try:
            self.runner.run(
                "frequency_write", int(frequency_hz), {"active_vfo": prepared_vfo.value}
            )
            self.runner.run("transmit_vfo_write", prepared_vfo.value)
            confirmed_vfo, _reply = self.runner.run("transmit_vfo")
            confirmed_frequency, _reply = self.runner.run(
                "frequency_read", selector_values={"active_vfo": prepared_vfo.value}
            )
            if confirmed_vfo != prepared_vfo.value or int(confirmed_frequency) != int(frequency_hz):
                raise CATError(
                    f"Radio did not confirm prepared M transmit VFO at {frequency_hz} Hz."
                )
        except Exception:
            try:
                self.runner.run("transmit_vfo_write", original_transmit_vfo.value)
                self.runner.run(
                    "frequency_write", int(original_frequency),
                    {"active_vfo": prepared_vfo.value},
                )
            except Exception:
                pass
            raise
        return MemoryTransmitPlan(
            receive_vfo=receive_vfo,
            original_transmit_vfo=original_transmit_vfo,
            prepared_transmit_vfo=prepared_vfo,
            original_prepared_frequency=int(original_frequency),
        )

    def restore_memory_transmit(self, plan: MemoryTransmitPlan) -> None:
        self.runner.run("transmit_vfo_write", plan.original_transmit_vfo.value)
        self.runner.run(
            "frequency_write", plan.original_prepared_frequency,
            {"active_vfo": plan.prepared_transmit_vfo.value},
        )

    def read_mode(self, receiver: Optional[Receiver] = None) -> OperatingMode:
        selectors = {"receiver": (receiver or Receiver.MAIN).value} if self.is_dual_receiver else None
        label, _reply = self.runner.run("mode_read", selector_values=selectors)
        for mode in OperatingMode:
            if mode.label == label:
                return mode
        raise CATError(f"Driver returned unsupported common mode {label!r}.")

    def set_mode(self, *args) -> None:
        if self.is_dual_receiver:
            if len(args) != 2 or not isinstance(args[0], Receiver):
                raise CATError("Dual-receiver driver requires receiver and mode.")
            receiver, mode = args
            self.runner.run("mode_write", mode.label, {"receiver": receiver.value})
            return
        if len(args) != 1:
            raise CATError("Active-VFO driver requires one mode.")
        self.runner.run("mode_write", args[0].label)

    def read_transmitting(self) -> Optional[bool]:
        if "tx_state" not in self.driver["capabilities"]:
            return None
        state, _reply = self.runner.run("tx_state")
        if state not in ("RX", "TX"):
            raise CATError(f"Driver returned invalid RX/TX state {state!r}.")
        return state == "TX"

    def supported_receive_sources(self, receiver: Optional[Receiver] = None) -> tuple[str, ...]:
        if "antenna_select" not in self.driver["capabilities"]:
            return ()
        operation = self.driver["capabilities"]["antenna_select"]
        if self.is_dual_receiver:
            selected = (receiver or Receiver.MAIN).value
            choices = operation.get("choices_by_selector", {}).get(selected, {})
        else:
            choices = operation.get("choices", {})
        return tuple(str(choice) for choice in choices)

    def read_antenna_state(self, receiver: Optional[Receiver] = None) -> AntennaState:
        if "antenna_read" not in self.driver["capabilities"]:
            raise CATError("Antenna read is not supported by this driver.")
        selectors = {"receiver": (receiver or Receiver.MAIN).value} if self.is_dual_receiver else None
        raw, _reply = self.runner.run("antenna_read", selector_values=selectors)
        text = str(raw)
        if text in self.supported_receive_sources(receiver):
            return AntennaState(
                "ANT1" if text == "RX ANT" else text,
                text == "RX ANT",
                "",
            )
        if len(text) < 3 or text[0] not in "1234" or text[1] not in "01":
            raise CATError(f"Driver returned invalid antenna state {raw!r}.")
        return AntennaState(f"ANT{text[0]}", text[1] == "1", text[2:])

    def set_receive_source(self, *args) -> None:
        if self.is_dual_receiver:
            if len(args) != 2 or not isinstance(args[0], Receiver):
                raise CATError("Dual-receiver driver requires receiver and receive source.")
            self.runner.run("antenna_select", args[1], {"receiver": args[0].value})
            return
        self.runner.run("antenna_select", args[0])

    def restore_antenna_state(self, *args) -> None:
        if "antenna_select" not in self.driver["capabilities"]:
            return
        if self.is_dual_receiver:
            receiver, state = args
            self.set_receive_source(receiver, state.active_source)
            return
        self.set_receive_source(args[0].active_source)


KenwoodRadio = Union[TS990Radio, TS2000Radio, TS590SGRadio, TS890Radio]
SupportedRadio = Union[TS990Radio, TS2000Radio, TS590SGRadio, TS890Radio, DriverRadio]
SelectionCallback = Callable[[str], None]


def is_dual_receiver_radio(radio: object) -> bool:
    return isinstance(radio, TS990Radio) or (
        isinstance(radio, DriverRadio) and radio.is_dual_receiver
    )


def detect_radio(
    engine: CATEngine,
    driver: Optional[dict[str, Any]] = None,
    driver_path: Optional[str | Path] = None,
) -> SupportedRadio:
    if driver is not None:
        radio = DriverRadio(engine, driver, driver_path or "")
        radio.identify()
        return radio
    reply = engine.query("ID;")
    if reply == TS590SG_ID:
        return TS590SGRadio(engine)
    raise CATError(
        f"No outboard driver is selected and the default TS-590SG fallback "
        f"expected {TS590SG_ID!r}; radio replied {reply!r}. Load the correct .rmradio driver."
    )


@dataclass(frozen=True, slots=True)
class RadioEndpoint:
    """One controllable receiver or currently active VFO."""

    radio: SupportedRadio
    receiver: Optional[Receiver]
    name: str
    selection_callback: Optional[SelectionCallback] = None

    def _report_selection(self, value: str) -> None:
        if self.selection_callback is not None:
            self.selection_callback(value)

    def _uses_receiver(self) -> bool:
        return is_dual_receiver_radio(self.radio)

    def read_frequency(self) -> int:
        if self._uses_receiver():
            if self.receiver is None:
                raise CATError("TS-990S endpoint has no Main/Sub receiver selected.")
            self._report_selection(self.receiver.value)
            return self.radio.read_frequency(self.receiver)
        frequency_hz, vfo = self.radio.read_frequency()
        self._report_selection("SELECTED VFO" if isinstance(self.radio, DriverRadio) and self.radio.topology.get("type") == "selected_vfo" else vfo.value)
        return frequency_hz

    def set_frequency(self, frequency_hz: int) -> None:
        if self._uses_receiver():
            if self.receiver is None:
                raise CATError("TS-990S endpoint has no Main/Sub receiver selected.")
            self.radio.set_frequency(self.receiver, frequency_hz)
            self._report_selection(self.receiver.value)
            return
        vfo = self.radio.set_frequency(frequency_hz)
        self._report_selection("SELECTED VFO" if isinstance(self.radio, DriverRadio) and self.radio.topology.get("type") == "selected_vfo" else vfo.value)

    def read_transmit_frequency(self) -> int:
        if isinstance(self.radio, DriverRadio):
            return self.radio.read_transmit_frequency(self.receiver)
        if isinstance(self.radio, (TS590SGRadio, TS2000Radio)):
            reply = self.radio.engine.query("FT;")
            if reply not in ("FT0;", "FT1;"):
                raise CATError(f"Unexpected transmit VFO response: {reply!r}")
            prefix = "FA" if reply == "FT0;" else "FB"
            return _parse_frequency(self.radio.engine.query(prefix + ";"), prefix)
        return self.read_frequency()

    def set_transmit_frequency(self, frequency_hz: int) -> None:
        if isinstance(self.radio, DriverRadio):
            self.radio.set_transmit_frequency(frequency_hz, self.receiver)
            return
        if isinstance(self.radio, (TS590SGRadio, TS2000Radio)):
            _validate_frequency(frequency_hz, self.radio.identity.model)
            reply = self.radio.engine.query("FT;")
            if reply not in ("FT0;", "FT1;"):
                raise CATError(f"Unexpected transmit VFO response: {reply!r}")
            prefix = "FA" if reply == "FT0;" else "FB"
            self.radio.engine.set(f"{prefix}{frequency_hz:011d};")
            return
        self.set_frequency(frequency_hz)

    def prepare_memory_transmit(self, frequency_hz: int) -> Optional[MemoryTransmitPlan]:
        """Prepare M before TX when this radio can dedicate the other VFO to transmit."""
        if isinstance(self.radio, DriverRadio):
            return self.radio.prepare_memory_transmit(frequency_hz)
        if not isinstance(self.radio, (TS590SGRadio, TS2000Radio)):
            return None
        _validate_frequency(frequency_hz, self.radio.identity.model)
        engine = self.radio.engine
        receive_reply = engine.query("FR;")
        transmit_reply = engine.query("FT;")
        if receive_reply not in ("FR0;", "FR1;") or transmit_reply not in ("FT0;", "FT1;"):
            raise CATError(
                f"Cannot prepare M from VFO replies {receive_reply!r}, {transmit_reply!r}."
            )
        receive_vfo = VFO.A if receive_reply == "FR0;" else VFO.B
        original_transmit_vfo = VFO.A if transmit_reply == "FT0;" else VFO.B
        prepared_vfo = VFO.B if receive_vfo is VFO.A else VFO.A
        original_frequency = _parse_frequency(
            engine.query(prepared_vfo.command + ";"), prepared_vfo.command
        )
        try:
            engine.set(f"{prepared_vfo.command}{frequency_hz:011d};")
            engine.set("FT1;" if prepared_vfo is VFO.B else "FT0;")
            if engine.query("FT;") != ("FT1;" if prepared_vfo is VFO.B else "FT0;"):
                raise CATError("Radio did not confirm the prepared M transmit VFO.")
            if _parse_frequency(
                engine.query(prepared_vfo.command + ";"), prepared_vfo.command
            ) != frequency_hz:
                raise CATError("Radio did not confirm the prepared M transmit frequency.")
        except Exception:
            try:
                engine.set("FT1;" if original_transmit_vfo is VFO.B else "FT0;")
                engine.set(f"{prepared_vfo.command}{original_frequency:011d};")
            except Exception:
                pass
            raise
        return MemoryTransmitPlan(
            receive_vfo, original_transmit_vfo, prepared_vfo, original_frequency
        )

    def restore_memory_transmit(self, plan: MemoryTransmitPlan) -> None:
        if isinstance(self.radio, DriverRadio):
            self.radio.restore_memory_transmit(plan)
            return
        if isinstance(self.radio, (TS590SGRadio, TS2000Radio)):
            engine = self.radio.engine
            engine.set("FT1;" if plan.original_transmit_vfo is VFO.B else "FT0;")
            engine.set(
                f"{plan.prepared_transmit_vfo.command}"
                f"{plan.original_prepared_frequency:011d};"
            )

    def read_transmitting(self) -> Optional[bool]:
        if isinstance(self.radio, (TS2000Radio, TS590SGRadio, DriverRadio)):
            return self.radio.read_transmitting()
        return None

    def read_mode(self) -> OperatingMode:
        if self._uses_receiver():
            if self.receiver is None:
                raise CATError("TS-990S endpoint has no Main/Sub receiver selected.")
            return self.radio.read_mode(self.receiver)
        return self.radio.read_mode()

    def set_mode(self, mode: OperatingMode) -> None:
        if self._uses_receiver():
            if self.receiver is None:
                raise CATError("TS-990S endpoint has no Main/Sub receiver selected.")
            self.radio.set_mode(self.receiver, mode)
            return
        self.radio.set_mode(mode)

    def supported_receive_sources(self) -> tuple[str, ...]:
        if self._uses_receiver():
            if isinstance(self.radio, DriverRadio):
                return self.radio.supported_receive_sources(self.receiver)
            return "ANT1", "ANT2", "ANT3", "ANT4", "RX ANT"
        if isinstance(self.radio, TS2000Radio):
            return "ANT1", "ANT2"
        if isinstance(self.radio, DriverRadio):
            return self.radio.supported_receive_sources()
        return "ANT1", "ANT2", "RX ANT"

    def read_antenna_state(self) -> AntennaState:
        if self._uses_receiver():
            if self.receiver is None:
                raise CATError("TS-990S endpoint has no Main/Sub receiver selected.")
            return self.radio.read_antenna_state(self.receiver)
        return self.radio.read_antenna_state()

    def set_receive_source(self, source: str) -> None:
        if self._uses_receiver():
            if self.receiver is None:
                raise CATError("TS-990S endpoint has no Main/Sub receiver selected.")
            self.radio.set_receive_source(self.receiver, source)
            return
        self.radio.set_receive_source(source)

    def restore_antenna_state(self, state: AntennaState) -> None:
        if self._uses_receiver():
            if self.receiver is None:
                raise CATError("TS-990S endpoint has no Main/Sub receiver selected.")
            self.radio.restore_antenna_state(self.receiver, state)
            return
        self.radio.restore_antenna_state(state)


def format_frequency(frequency_hz: int) -> str:
    """Format Hz as MHz.kHz.Hz, e.g. 14.195.000."""
    raw = f"{frequency_hz:09d}"
    return f"{int(raw[:-6])}.{raw[-6:-3]}.{raw[-3:]}"


def directional_step(frequency_hz: int, step_hz: int, direction: int) -> int:
    """Move to the next boundary in the requested wheel direction."""
    if step_hz <= 0:
        raise ValueError("step_hz must be positive")
    if direction > 0:
        return ((frequency_hz // step_hz) + 1) * step_hz
    if direction < 0:
        if frequency_hz % step_hz:
            return (frequency_hz // step_hz) * step_hz
        return frequency_hz - step_hz
    return frequency_hz
