"""Load and execute data-only RigMirror radio drivers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from errors import CATError


DRIVER_FORMAT = "RigMirror Radio Driver"
SCHEMA_VERSION = 1


class DriverError(CATError):
    """The selected radio driver is missing or cannot be used."""


def load_driver(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        driver = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverError(f"Could not read radio driver: {exc}") from exc
    validate_driver(driver)
    return driver


def validate_driver(driver: Any) -> None:
    if not isinstance(driver, dict) or driver.get("format") != DRIVER_FORMAT:
        raise DriverError(f"This is not a {DRIVER_FORMAT} file.")
    if driver.get("schema_version") != SCHEMA_VERSION:
        raise DriverError(
            f"Unsupported driver schema {driver.get('schema_version')!r}; "
            f"RigMirror v0.2 supports schema {SCHEMA_VERSION}."
        )
    for section in ("metadata", "transport", "capabilities"):
        if not isinstance(driver.get(section), dict):
            raise DriverError(f"Driver section {section!r} is missing or invalid.")
    transport = driver["transport"]
    if transport.get("protocol") not in ("semicolon_ascii", "icom_civ"):
        raise DriverError("Unsupported radio protocol.")
    topology = driver.get("topology", {"type": "active_vfo"})
    if not isinstance(topology, dict) or topology.get("type", "active_vfo") not in (
        "active_vfo", "dual_receiver", "selected_vfo",
    ):
        raise DriverError("Driver topology must be active_vfo or dual_receiver.")
    required = {
        "identify", "frequency_read", "frequency_write",
        "mode_read", "mode_write",
    }
    if topology.get("type", "active_vfo") == "active_vfo":
        required.add("active_vfo")
    missing = sorted(required.difference(driver["capabilities"]))
    if missing:
        raise DriverError("Driver lacks required RigMirror functions: " + ", ".join(missing))


def driver_label(driver: dict[str, Any]) -> str:
    metadata = driver["metadata"]
    return str(metadata.get("display_name") or metadata.get("model") or "Unnamed radio")


def driver_fingerprint(path: str | Path) -> str:
    """Identify the exact driver bytes, including unversioned user edits."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise DriverError(f"Could not fingerprint radio driver: {exc}") from exc


def driver_local_status(path: str | Path, test_history: dict[str, Any]) -> str:
    record = test_history.get(driver_fingerprint(path), {})
    status = str(record.get("status", "")).upper()
    return status if status in ("TESTED", "FAILED") else "NOT TESTED"


class EngineTransport:
    def __init__(self, engine) -> None:
        self.engine = engine

    def send(self, command: str, expect_reply: bool) -> str:
        return self.engine.query(command) if expect_reply else (self.engine.set(command) or "")


class OperationRunner:
    def __init__(self, driver: dict[str, Any], engine) -> None:
        self.driver = driver
        self.transport = EngineTransport(engine)

    def run(
        self,
        capability_id: str,
        input_value: Any = None,
        selector_values: dict[str, Any] | None = None,
    ) -> tuple[Any, str]:
        try:
            operation = self.driver["capabilities"][capability_id]
        except KeyError as exc:
            raise DriverError(f"Driver has no {capability_id!r} function.") from exc
        kind = operation.get("kind")
        if kind == "constant":
            return operation["value"], "driver-declared constant"
        if kind == "query":
            return self._query(operation, selector_values)
        if kind == "set":
            return self._set(operation, input_value, selector_values)
        if kind == "choice_set":
            return self._choice(operation, input_value, selector_values)
        raise DriverError(f"Unsupported driver function kind {kind!r}.")

    def _query(
        self, operation: dict[str, Any], selector_values: dict[str, Any] | None
    ) -> tuple[Any, str]:
        command = self._command(operation, selector_values)
        reply = self.transport.send(command, True)
        if "response_equals" in operation:
            expected = operation["response_equals"]
            if reply != expected:
                raise DriverError(f"Expected {expected!r}; radio replied {reply!r}.")
            match = re.fullmatch(re.escape(expected), reply)
        else:
            pattern = operation.get("response_regex")
            match = re.fullmatch(pattern, reply) if isinstance(pattern, str) else None
            if match is None:
                raise DriverError(f"Reply {reply!r} did not match {pattern!r}.")
        return self._extract(operation.get("extract", {"type": "reply"}), reply, match), reply

    def _set(
        self,
        operation: dict[str, Any],
        input_value: Any,
        selector_values: dict[str, Any] | None,
    ) -> tuple[Any, str]:
        value = self._convert_input(operation.get("input", {}), input_value)
        preserve = operation.get("preserve_wire_value_from")
        if preserve:
            existing, reply = self.run(preserve, selector_values=selector_values)
            if existing == input_value:
                read_op = self.driver["capabilities"][preserve]
                value = re.fullmatch(read_op["response_regex"], reply).group("value")
        try:
            command = self._command(operation, selector_values).format(value=value)
        except (KeyError, ValueError) as exc:
            raise DriverError(f"Could not format driver command: {exc}") from exc
        if operation.get("preserve_mode_suffix"):
            prefix = command[:4]
            previous = self.transport.send(prefix, True)
            if not re.fullmatch(re.escape(prefix) + r"[0-9A-F]{4}0[123]", previous):
                raise DriverError(f"Invalid CI-V mode/filter reply: {previous!r}")
            command += previous[6:] if previous[4:6] == value else "00" + previous[8:]
        context = operation.get("preserve_context")
        if not isinstance(context, dict):
            self.transport.send(command, False)
            return input_value, command

        query_command = str(context["query_command"])
        original_reply = self.transport.send(query_command, True)
        match = re.fullmatch(str(context["response_regex"]), original_reply)
        if match is None:
            raise DriverError(
                f"Context reply {original_reply!r} did not match {context['response_regex']!r}."
            )
        original = match.group(str(context.get("group", "value")))
        selector = str(operation.get("selector", ""))
        selected = (selector_values or {}).get(selector)
        try:
            target = str(context["target_by_selector"][str(selected)])
        except KeyError as exc:
            raise DriverError(f"No context target exists for selector {selected!r}.") from exc
        changed = original != target
        if changed:
            self.transport.send(str(context["select_command"]).format(value=target), False)
        try:
            self.transport.send(command, False)
        finally:
            if changed:
                self.transport.send(str(context["restore_command"]).format(value=original), False)
        return input_value, command

    def _choice(
        self,
        operation: dict[str, Any],
        input_value: Any,
        selector_values: dict[str, Any] | None,
    ) -> tuple[Any, str]:
        choice = str(input_value)
        try:
            if "choices_by_selector" in operation:
                selector = str(operation.get("selector", ""))
                selected = (selector_values or {}).get(selector)
                commands = operation["choices_by_selector"][str(selected)][choice]
            else:
                commands = operation["choices"][choice]
        except KeyError as exc:
            raise DriverError(f"Driver cannot select {choice!r}.") from exc
        for command in commands:
            self.transport.send(command, False)
        return choice, " ".join(commands)

    def _command(
        self, operation: dict[str, Any], selector_values: dict[str, Any] | None = None
    ) -> str:
        if isinstance(operation.get("command"), str):
            return operation["command"]
        selector = operation.get("selector")
        if selector_values is not None and selector in selector_values:
            selected = selector_values[selector]
        else:
            selected, _ = self.run(selector)
        try:
            return operation["command_by_selector"][str(selected)]
        except KeyError as exc:
            raise DriverError(f"No command exists for selector result {selected!r}.") from exc

    @staticmethod
    def _extract(spec: dict[str, Any], reply: str, match: re.Match[str]) -> Any:
        kind = spec.get("type", "reply")
        if kind == "reply":
            value: Any = reply
        elif kind == "constant":
            value = spec.get("value")
        elif kind == "regex_group":
            value = match.group(spec.get("group", "value"))
        elif kind == "character":
            value = reply[int(spec["index"])]
        else:
            raise DriverError(f"Unsupported extraction type {kind!r}.")
        if spec.get("decode") == "integer":
            value = int(value)
        elif spec.get("decode") == "bcd_le":
            digits = bytes.fromhex(str(value))[::-1].hex()
            if not digits.isdigit():
                raise DriverError("Invalid BCD frequency in reply.")
            value = int(digits)
        mapping = spec.get("map")
        if isinstance(mapping, dict):
            try:
                value = mapping[str(value)]
            except KeyError as exc:
                raise DriverError(f"No result mapping exists for {value!r}.") from exc
        return value

    @staticmethod
    def _convert_input(spec: dict[str, Any], raw: Any) -> Any:
        kind = spec.get("type", "text")
        if kind == "bcd_le":
            value = int(raw)
            size = int(spec.get("bytes", 5))
            if value < 0 or value >= 10 ** (size * 2):
                raise DriverError("Frequency does not fit the CI-V field.")
            return bytes.fromhex(f"{value:0{size * 2}d}")[::-1].hex().upper()
        if kind == "integer":
            value = int(raw)
            if value < int(spec.get("minimum", value)) or value > int(spec.get("maximum", value)):
                raise DriverError(f"Value {value} is outside the driver's permitted range.")
            return value
        if kind in ("mapped_choice", "enum"):
            mapping = spec.get("map", {})
            raw_text = str(raw)
            for label, value in mapping.items():
                if raw_text.casefold() in (str(label).casefold(), str(value).casefold()):
                    return value
            raise DriverError(f"Driver cannot encode {raw!r}.")
        return raw


def test_driver_on_engine(driver: dict[str, Any], engine) -> list[str]:
    """Exercise the core read/write path, restoring each observed value."""
    runner = OperationRunner(driver, engine)
    results: list[str] = []

    identity, reply = runner.run("identify")
    results.append(f"Identify: {identity} [{reply}]")
    topology = driver.get("topology", {"type": "active_vfo"})
    if topology.get("type", "active_vfo") == "dual_receiver":
        receiver = str(topology.get("default_receiver", "MAIN"))
        if receiver not in ("MAIN", "SUB"):
            raise DriverError(f"Invalid default receiver {receiver!r}.")
        results.append(f"Receiver under test: {receiver} [driver topology]")
        selectors = {"receiver": receiver}
    elif topology.get("type") == "selected_vfo":
        selectors = {}
        results.append("Receiver under test: currently selected VFO")
    else:
        vfo, reply = runner.run("active_vfo")
        if vfo not in ("VFO A", "VFO B"):
            raise DriverError(f"Select VFO A or VFO B before testing; radio reports {vfo}.")
        results.append(f"Active VFO: {vfo} [{reply}]")
        selectors = {"active_vfo": vfo}
    frequency, reply = runner.run("frequency_read", selector_values=selectors)
    results.append(f"Frequency read: {frequency} Hz [{reply}]")
    _value, command = runner.run("frequency_write", frequency, selectors)
    confirmed, confirm_reply = runner.run("frequency_read", selector_values=selectors)
    if confirmed != frequency:
        raise DriverError(f"Frequency write did not verify: expected {frequency}, read {confirmed}.")
    results.append(f"Frequency write/restore: PASS [{command} -> {confirm_reply}]")
    mode, reply = runner.run("mode_read", selector_values=selectors)
    results.append(f"Mode read: {mode} [{reply}]")
    _value, command = runner.run("mode_write", mode, selectors)
    confirmed_mode, confirm_reply = runner.run("mode_read", selector_values=selectors)
    if confirmed_mode != mode:
        raise DriverError(f"Mode write did not verify: expected {mode}, read {confirmed_mode}.")
    results.append(f"Mode write/restore: PASS [{command} -> {confirm_reply}]")
    if "tx_state" in driver["capabilities"]:
        tx, reply = runner.run("tx_state")
        results.append(f"RX/TX state: {tx} [{reply}]")
    else:
        events = driver.get("events", {})
        if not events.get("transmit") or not events.get("receive"):
            raise DriverError("Driver has neither a TX-state query nor TX/RX event definitions.")
        results.append("RX/TX state: event-driven [AI TX/RX notifications]")
    if "antenna_read" not in driver["capabilities"] or "antenna_select" not in driver["capabilities"]:
        results.append("Antenna routing: not provided by this driver")
        return results
    antenna, reply = runner.run("antenna_read", selector_values=selectors)
    text = str(antenna)
    declared = driver["capabilities"]["antenna_select"]
    choices = declared.get("choices", {})
    if not choices and selectors:
        choices = declared.get("choices_by_selector", {}).get(selectors.get("receiver"), {})
    active_source = text if text in choices else (
        "RX ANT" if len(text) > 1 and text[1] == "1" else f"ANT{text[0]}"
    )
    results.append(f"Receive source: {active_source} [{reply}]")
    _value, command = runner.run("antenna_select", active_source, selectors)
    results.append(f"Receive-source write/restore: PASS [{command}]")
    return results
