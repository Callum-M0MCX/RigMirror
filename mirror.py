"""Continuous monitoring and mirroring between two radio endpoints."""

from __future__ import annotations

import threading
import time
from typing import Callable, Mapping, Optional

from alignment import aligned_sub_frequency, normalise_offsets
from errors import CATError
from radio import RadioEndpoint


FrequencyCallback = Callable[[int, int], None]
FailureCallback = Callable[[int, str], None]
TransmitCallback = Callable[[bool], None]
RouteCallback = Callable[[bool, str], None]
RoutingFailureCallback = Callable[[str], None]
OperationFailureCallback = Callable[[str], None]
RangeCallback = Callable[[bool, int], None]


class MirrorCoordinator:
    """Run all live radio operations on one background thread."""

    def __init__(
        self,
        endpoints: tuple[RadioEndpoint, RadioEndpoint],
        frequency_callback: FrequencyCallback,
        failure_callback: FailureCallback,
        poll_interval: float = 0.10,
        transmit_callback: Optional[TransmitCallback] = None,
        route_callback: Optional[RouteCallback] = None,
        routing_failure_callback: Optional[RoutingFailureCallback] = None,
        operation_failure_callback: Optional[OperationFailureCallback] = None,
        range_callback: Optional[RangeCallback] = None,
        tx_action: str = "NO CHANGE",
        tx_source: str = "NO CHANGE",
        parking_frequency: int = 10_100_000,
        frequency_ceiling_hz: int = 30_000_000,
        routing_enabled: bool = True,
        alignment_offsets: Optional[Mapping[str, int]] = None,
    ) -> None:
        self.endpoints = endpoints
        self.frequency_callback = frequency_callback
        self.failure_callback = failure_callback
        self.poll_interval = poll_interval
        self.transmit_callback = transmit_callback
        self.route_callback = route_callback
        self.routing_failure_callback = routing_failure_callback
        self.operation_failure_callback = operation_failure_callback
        self.range_callback = range_callback
        self.tx_action = tx_action
        self.tx_source = tx_source
        # Compatibility for pre-v0.3.001 callers: a real destination used to
        # imply antenna diversion because there was no separate action field.
        if self.tx_action == "NO CHANGE" and self.tx_source != "NO CHANGE":
            self.tx_action = "SWITCH ANTENNA / RX PORT"
        self.parking_frequency = parking_frequency
        self.frequency_ceiling_hz = frequency_ceiling_hz
        self.routing_enabled = routing_enabled
        self._alignment_offsets = normalise_offsets(alignment_offsets or {})
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._guard = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._active = False
        self._source_index = 0
        self._pending_tune: Optional[int] = None
        self._pending_sub_frequency: Optional[int] = None
        self._known: list[Optional[int]] = [None, None]
        self._target_check_at = 0.0
        self._known_modes = [None, None]
        self._source_mode_check_at = 0.0
        self._target_mode_check_at = 0.0
        self._memory_armed = False
        self._home_frequency: Optional[int] = None
        self._listen_frequency: Optional[int] = None
        self._memory_tx_plan = None
        self._memory_prepare_pending = False
        self._memory_restore_pending = False
        self._memory_prepared_for_tx = False
        self._split_active = False
        self._split_frequency: Optional[int] = None
        self._transmitting = False
        self._pending_tx_state: Optional[bool] = None
        self._return_home = False
        self._was_active = False
        self._listener_pre_mirror_state = None
        self._listener_normal_state = None
        self._listener_diverted = False
        self._listener_parked = False
        self._listener_normal_frequency: Optional[int] = None
        self._restore_after_tx = False
        self._routing_changed = False
        self._out_of_range = False
        self._failure_index = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_monitoring(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None

    def set_active(self, active: bool) -> None:
        with self._guard:
            self._active = active
            self._target_check_at = 0.0
            if not active:
                self._split_active = False
                self._split_frequency = None
            if active and self._transmitting:
                self._pending_tx_state = True
        self._wake.set()

    def set_routing(
        self, tx_action: str, tx_source: str | None = None,
        parking_frequency: int | None = None,
    ) -> None:
        if tx_source is None:
            # Compatibility with set_routing("ANT2") from older UI/tests.
            tx_source = tx_action
            tx_action = (
                "NO CHANGE" if tx_source == "NO CHANGE"
                else "SWITCH ANTENNA / RX PORT"
            )
        with self._guard:
            self.tx_action = tx_action
            self.tx_source = tx_source
            if parking_frequency is not None:
                self.parking_frequency = parking_frequency
            self._routing_changed = True
        self._wake.set()

    def set_alignment_offsets(self, offsets: Mapping[str, int]) -> None:
        """Replace the live per-band alignment table and realign promptly."""
        with self._guard:
            self._alignment_offsets = normalise_offsets(offsets)
            self._target_check_at = 0.0
        self._wake.set()

    def set_poll_interval(self, seconds: float) -> None:
        with self._guard:
            self.poll_interval = max(0.05, float(seconds))
        self._wake.set()

    def set_source(self, source_index: int) -> None:
        if source_index not in (0, 1):
            raise ValueError("source_index must be 0 or 1")
        with self._guard:
            self._source_index = source_index
            self._pending_tune = None
            self._target_check_at = 0.0
            self._source_mode_check_at = 0.0
            self._target_mode_check_at = 0.0
        self._wake.set()

    def request_tune(self, frequency_hz: int) -> None:
        """Coalesce rapid wheel events into the latest absolute frequency."""
        with self._guard:
            self._pending_tune = frequency_hz
        self._wake.set()

    def request_sub_frequency(self, frequency_hz: int) -> None:
        """Tune only the Sub while Mirror is inactive (alignment preview)."""
        with self._guard:
            self._pending_sub_frequency = frequency_hz
        self._wake.set()

    def arm_memory(self, home_frequency: int) -> None:
        with self._guard:
            self._split_active = False
            self._split_frequency = None
            self._memory_armed = True
            self._home_frequency = home_frequency
            self._listen_frequency = home_frequency
            self._return_home = False
            self._memory_prepare_pending = True
            self._memory_restore_pending = False
            self._memory_prepared_for_tx = False
        self._wake.set()

    def cancel_memory(self) -> Optional[int]:
        """Disarm offset operation and return the stored home frequency."""
        with self._guard:
            home = self._home_frequency
            self._memory_armed = False
            self._home_frequency = None
            self._listen_frequency = None
            self._pending_tx_state = None
            self._return_home = home is not None
            self._memory_prepare_pending = False
            self._memory_restore_pending = self._memory_tx_plan is not None
            self._memory_prepared_for_tx = False
            if home is not None:
                self._pending_tune = home
        self._wake.set()
        return home

    def abandon_memory(self) -> None:
        """Disarm offset operation without recalling its old home frequency."""
        with self._guard:
            self._memory_armed = False
            self._home_frequency = None
            self._listen_frequency = None
            self._pending_tune = None
            self._return_home = False
            self._memory_prepare_pending = False
            self._memory_restore_pending = self._memory_tx_plan is not None
            self._memory_prepared_for_tx = False
        self._wake.set()

    def start_split(self, dx_frequency: int, offset_hz: int = 5_000) -> None:
        """Hold the Sub on the DX while releasing Master at DX + offset."""
        with self._guard:
            self._memory_armed = False
            self._home_frequency = None
            self._listen_frequency = None
            self._return_home = False
            self._memory_prepare_pending = False
            self._memory_restore_pending = self._memory_tx_plan is not None
            self._memory_prepared_for_tx = False
            self._split_active = True
            self._split_frequency = dx_frequency
            self._pending_tune = dx_frequency + offset_hz
            self._target_check_at = 0.0
        self._wake.set()

    def return_from_split(self) -> Optional[int]:
        """Recall the stored DX frequency and resume ordinary mirroring."""
        with self._guard:
            dx_frequency = self._split_frequency
            self._split_active = False
            self._split_frequency = None
            self._target_check_at = 0.0
            if dx_frequency is not None:
                self._pending_tune = dx_frequency
        self._wake.set()
        return dx_frequency

    def abandon_split(self) -> None:
        """Resume mirroring at the Master's present frequency without recall."""
        with self._guard:
            self._split_active = False
            self._split_frequency = None
            self._target_check_at = 0.0
        self._wake.set()

    def set_transmitting(self, transmitting: bool) -> None:
        with self._guard:
            if transmitting != self._transmitting:
                self._pending_tx_state = transmitting
        self._wake.set()

    def _snapshot_controls(self):
        with self._guard:
            active = self._active
            source_index = self._source_index
            pending = self._pending_tune
            self._pending_tune = None
            memory_armed = self._memory_armed
            home = self._home_frequency
            listen = self._listen_frequency
            tx_state = self._pending_tx_state
            self._pending_tx_state = None
            return_home = self._return_home
            self._return_home = False
            routing_changed = self._routing_changed
            self._routing_changed = False
            prepare_memory = self._memory_prepare_pending
            self._memory_prepare_pending = False
            restore_memory = self._memory_restore_pending
            self._memory_restore_pending = False
            pending_sub = self._pending_sub_frequency
            self._pending_sub_frequency = None
            split_active = self._split_active
            split_frequency = self._split_frequency
        return (active, source_index, pending, memory_armed, home, listen,
                tx_state, return_home, routing_changed, pending_sub,
                split_active, split_frequency, prepare_memory, restore_memory)

    def _set_both(self, source_index: int, frequency_hz: int) -> None:
        target_index = 1 - source_index
        self.endpoints[source_index].set_frequency(frequency_hz)
        self._known[source_index] = frequency_hz
        self.frequency_callback(source_index, frequency_hz)
        target_hz = aligned_sub_frequency(frequency_hz, self._alignment_offsets)
        self.endpoints[target_index].set_frequency(target_hz)
        self._known[target_index] = target_hz
        self.frequency_callback(target_index, target_hz)

    def _activate_listener_route(self, target: RadioEndpoint) -> None:
        if self.tx_action != "SWITCH ANTENNA / RX PORT":
            self._listener_pre_mirror_state = None
            self._listener_normal_state = None
            self._listener_diverted = False
            return
        if hasattr(target, "supported_receive_sources") and not target.supported_receive_sources():
            self._listener_pre_mirror_state = None
            self._listener_normal_state = None
            self._listener_diverted = False
            if self.route_callback is not None:
                self.route_callback(False, "NO CAT ROUTING")
            return
        # Snapshot only.  The Sub remains exactly as the operator left it
        # until Master TX is observed.
        self._listener_pre_mirror_state = target.read_antenna_state()
        self._listener_normal_state = self._listener_pre_mirror_state
        self._listener_diverted = False
        if self.route_callback is not None:
            self.route_callback(False, self._listener_normal_state.active_source)

    def _divert_listener(self, target: RadioEndpoint) -> None:
        # The routing write is deliberately the first Sub CAT operation after
        # Master TX is observed.  The RX state was captured when Mirror began;
        # no Sub TX query or pre-change antenna query delays the diversion.
        target.set_receive_source(self.tx_source)
        confirmed = target.read_antenna_state()
        if confirmed.active_source != self.tx_source:
            raise CATError(
                f"Sub did not confirm {self.tx_source}; it remains on "
                f"{confirmed.active_source}."
            )
        self._listener_diverted = True
        if self.route_callback is not None:
            self.route_callback(True, self.tx_source)

    def _park_listener(self, target: RadioEndpoint, target_index: int) -> None:
        if not self._listener_parked:
            self._listener_normal_frequency = self._known[target_index]
        target.set_frequency(self.parking_frequency)
        self._known[target_index] = self.parking_frequency
        self.frequency_callback(target_index, self.parking_frequency)
        self._listener_parked = True
        if self.route_callback is not None:
            self.route_callback(True, f"PARK {self.parking_frequency / 1_000_000:.3f} MHz")

    def _restore_listener_receive(self, target: RadioEndpoint) -> None:
        if self._listener_parked and self._listener_normal_frequency is not None:
            target.set_frequency(self._listener_normal_frequency)
            self._known[1 - self._source_index] = self._listener_normal_frequency
            self.frequency_callback(1 - self._source_index, self._listener_normal_frequency)
        self._listener_parked = False
        self._listener_normal_frequency = None
        if self._listener_diverted and self._listener_normal_state is not None:
            target.restore_antenna_state(self._listener_normal_state)
            confirmed = target.read_antenna_state()
            if confirmed != self._listener_normal_state:
                raise CATError("Sub did not confirm restoration of its RX antenna state.")
        self._listener_diverted = False
        if self.route_callback is not None and self._listener_normal_state is not None:
            self.route_callback(False, self._listener_normal_state.active_source)

    def _deactivate_listener_route(self, target: RadioEndpoint) -> None:
        if self._listener_parked and self._listener_normal_frequency is not None and not self._transmitting:
            target.set_frequency(self._listener_normal_frequency)
        self._listener_parked = False
        self._listener_normal_frequency = None
        if self._listener_pre_mirror_state is not None:
            target.restore_antenna_state(self._listener_pre_mirror_state)
            confirmed = target.read_antenna_state()
            if confirmed != self._listener_pre_mirror_state:
                raise CATError("Sub did not confirm its pre-Mirror antenna state.")
        self._listener_pre_mirror_state = None
        self._listener_normal_state = None
        self._listener_diverted = False
        if self.route_callback is not None:
            self.route_callback(False, "OFF")

    def _routing_failed(self, target: RadioEndpoint, action: str, exc: Exception) -> None:
        """Stop mirroring without treating a routing problem as lost CAT."""
        # Never deliberately reconnect the normal antenna while RF is still
        # present.  Defer exact restoration until Master returns to RX.
        if self._transmitting:
            self._restore_after_tx = True
        else:
            try:
                if self._listener_pre_mirror_state is not None:
                    target.restore_antenna_state(self._listener_pre_mirror_state)
            except Exception:
                pass
            self._listener_pre_mirror_state = None
            self._listener_normal_state = None
            self._listener_diverted = False
        self._was_active = False
        with self._guard:
            self._active = False
            self._memory_armed = False
            self._home_frequency = None
            self._listen_frequency = None
            self._split_active = False
            self._split_frequency = None
        if self.routing_failure_callback is not None:
            self.routing_failure_callback(f"Sub routing failed while {action}: {exc}")

    def _operation_failed(self, target: RadioEndpoint, action: str, exc: Exception) -> None:
        """Stop Mirror for a rejected operation while preserving healthy CAT links."""
        if self._transmitting:
            self._restore_after_tx = True
            self._was_active = False
            restore_memory = self._memory_tx_plan is not None
        else:
            restore_memory = False
            if self._memory_tx_plan is not None:
                try:
                    source = self.endpoints[self._source_index]
                    source.restore_memory_transmit(self._memory_tx_plan)
                except Exception:
                    pass
                self._memory_tx_plan = None
        with self._guard:
            self._active = False
            self._memory_armed = False
            self._home_frequency = None
            self._listen_frequency = None
            self._memory_prepare_pending = False
            self._memory_restore_pending = restore_memory
            self._memory_prepared_for_tx = False
            self._split_active = False
            self._split_frequency = None
        if not self._transmitting:
            try:
                self._deactivate_listener_route(target)
            except Exception:
                pass
            self._was_active = False
        if self.operation_failure_callback is not None:
            self.operation_failure_callback(f"RigMirror could not complete {action}: {exc}")

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                (active, source_index, pending, memory_armed, home, listen,
                 tx_state, return_home, routing_changed,
                 pending_sub, split_active,
                 split_frequency, prepare_memory,
                 restore_memory) = self._snapshot_controls()
                target_index = 1 - source_index
                source = self.endpoints[source_index]
                target = self.endpoints[target_index]

                if self.routing_enabled:
                    try:
                        if active and not self._was_active:
                            self._activate_listener_route(target)
                        elif not active and self._was_active:
                            self._deactivate_listener_route(target)
                    except Exception as exc:
                        self._routing_failed(target, "starting or stopping Mirror", exc)
                        self._wake.wait(self.poll_interval)
                        self._wake.clear()
                        continue
                self._was_active = active

                # Keep the RX/TX indicator honest even while Mirror is idle.
                # TS-990 reports this automatically; TS-590SG is polled.
                self._failure_index = source_index
                observed_tx = source.read_transmitting()
                if (tx_state is None and observed_tx is not None
                        and observed_tx != self._transmitting):
                    tx_state = observed_tx

                if restore_memory:
                    if self._transmitting or tx_state is True:
                        with self._guard:
                            self._memory_restore_pending = True
                    elif self._memory_tx_plan is not None:
                        source.restore_memory_transmit(self._memory_tx_plan)
                        self._memory_tx_plan = None

                if prepare_memory and memory_armed and home is not None:
                    prepare = getattr(source, "prepare_memory_transmit", None)
                    if (self._transmitting or tx_state is True) and callable(prepare):
                        self._operation_failed(
                            target, "preparing M before transmission",
                            CATError("M was keyed before its transmit VFO was ready."),
                        )
                        self._wake.wait(self.poll_interval)
                        self._wake.clear()
                        continue
                    try:
                        self._memory_tx_plan = (
                            prepare(home)
                            if callable(prepare) and not (self._transmitting or tx_state is True)
                            else None
                        )
                        self._memory_prepared_for_tx = True
                    except Exception as exc:
                        self._operation_failed(target, "preparing M", exc)
                        self._wake.wait(self.poll_interval)
                        self._wake.clear()
                        continue

                if tx_state is not None:
                    self._transmitting = tx_state
                    if self.transmit_callback is not None:
                        self.transmit_callback(tx_state)
                    if tx_state:
                        routing_error = None
                        if self.routing_enabled:
                            try:
                                self._failure_index = target_index
                                if self.tx_action == "SWITCH ANTENNA / RX PORT":
                                    self._divert_listener(target)
                                elif self.tx_action == "PARKING FREQUENCY":
                                    self._park_listener(target, target_index)
                                elif self.route_callback is not None:
                                    self.route_callback(True, "NO CHANGE")
                            except Exception as exc:
                                routing_error = exc
                        if memory_armed and home is not None:
                            current = self._known[source_index]
                            if current is not None and current != home:
                                self._listen_frequency = current
                            if self._memory_tx_plan is None:
                                try:
                                    self._failure_index = source_index
                                    source.set_transmit_frequency(home)
                                    confirmed = source.read_transmit_frequency()
                                    if confirmed != home:
                                        raise CATError(
                                            f"Master did not confirm M transmit frequency {home}; read {confirmed}."
                                        )
                                except Exception as exc:
                                    self._operation_failed(
                                        target, "returning the Master to its M transmit frequency", exc
                                    )
                                    self._wake.wait(self.poll_interval)
                                    self._wake.clear()
                                    continue
                                self._known[source_index] = home
                            self.frequency_callback(source_index, home)
                        if routing_error is not None:
                            self._routing_failed(target, "changing the TX antenna", routing_error)
                            self._wake.wait(self.poll_interval)
                            self._wake.clear()
                            continue
                    else:
                        if memory_armed and home is not None and listen is not None:
                            restore = self._listen_frequency or listen
                            if self._memory_tx_plan is None:
                                self._failure_index = source_index
                                source.set_frequency(restore)
                                confirmed = source.read_frequency()
                                if confirmed != restore:
                                    raise CATError(
                                        f"Master did not confirm M receive frequency {restore}; read {confirmed}."
                                    )
                            self._known[source_index] = restore
                            self.frequency_callback(source_index, restore)
                        if self.routing_enabled:
                            try:
                                self._failure_index = target_index
                                if self._restore_after_tx:
                                    self._deactivate_listener_route(target)
                                    self._restore_after_tx = False
                                else:
                                    self._restore_listener_receive(target)
                            except Exception as exc:
                                self._routing_failed(target, "restoring the RX antenna", exc)
                                self._wake.wait(self.poll_interval)
                                self._wake.clear()
                                continue

                # Options are live.  If the destination changes during TX,
                # move the Sub immediately; on RX the new choice waits
                # harmlessly for the next PTT.
                if (self.routing_enabled and active and self._transmitting
                        and routing_changed and tx_state is None):
                    try:
                        self._failure_index = target_index
                        if self.tx_action == "SWITCH ANTENNA / RX PORT":
                            # Switch directly between safe destinations. If
                            # the new command fails, leave the previous
                            # diverted antenna in place while RF is present.
                            if self._listener_parked:
                                self._restore_listener_receive(target)
                            self._divert_listener(target)
                        elif self.tx_action == "PARKING FREQUENCY":
                            self._restore_listener_receive(target)
                            self._park_listener(target, target_index)
                        else:
                            self._restore_listener_receive(target)
                    except Exception as exc:
                        self._routing_failed(target, "applying the new TX antenna", exc)
                        self._wake.wait(self.poll_interval)
                        self._wake.clear()
                        continue

                # Once the TX transition work is complete, avoid routine
                # Sub and mode queries until RX.  A TS-590SG Master is
                # already checked through IF above.  A TS-990S relies on AI
                # notifications, so one harmless FA read keeps its serial
                # input moving and allows the RX notification to be drained.
                if self._transmitting:
                    if pending_sub is not None:
                        with self._guard:
                            self._pending_sub_frequency = pending_sub
                    if observed_tx is None:
                        source_hz = source.read_frequency()
                        self._known[source_index] = source_hz
                        self.frequency_callback(source_index, source_hz)
                    delay = max(0.0, self.poll_interval - (time.monotonic() - started))
                    self._wake.wait(delay)
                    self._wake.clear()
                    continue

                if pending_sub is not None and not active:
                    self._failure_index = target_index
                    target.set_frequency(pending_sub)
                    self._known[target_index] = pending_sub
                    self.frequency_callback(target_index, pending_sub)

                if return_home and pending is not None:
                    self._set_both(source_index, pending)
                    pending = None

                if pending is not None:
                    self._failure_index = source_index
                    source.set_frequency(pending)
                    self._known[source_index] = pending
                    self.frequency_callback(source_index, pending)
                    if active and not split_active and pending <= self.frequency_ceiling_hz:
                        self._failure_index = target_index
                        aligned_pending = aligned_sub_frequency(pending, self._alignment_offsets)
                        target.set_frequency(aligned_pending)
                        self._known[target_index] = aligned_pending
                        self.frequency_callback(target_index, aligned_pending)

                    if memory_armed and not self._transmitting:
                        with self._guard:
                            self._listen_frequency = pending

                self._failure_index = source_index
                source_hz = source.read_frequency()
                self._known[source_index] = source_hz
                self.frequency_callback(source_index, source_hz)
                if memory_armed and not self._transmitting and source_hz != home:
                    with self._guard:
                        self._listen_frequency = source_hz

                out_of_range = source_hz > self.frequency_ceiling_hz
                if out_of_range != self._out_of_range:
                    self._out_of_range = out_of_range
                    if self.range_callback is not None:
                        self.range_callback(out_of_range, source_hz)
                if out_of_range:
                    delay = max(0.0, self.poll_interval - (time.monotonic() - started))
                    self._wake.wait(delay)
                    self._wake.clear()
                    continue

                now = time.monotonic()
                if active:
                    target_reference_hz = (
                        split_frequency
                        if split_active and split_frequency is not None
                        else source_hz
                    )
                    expected_target_hz = aligned_sub_frequency(
                        target_reference_hz, self._alignment_offsets
                    )
                    target_hz = self._known[target_index]
                    if now >= self._target_check_at:
                        self._failure_index = target_index
                        target_hz = target.read_frequency()
                        self._known[target_index] = target_hz
                        self.frequency_callback(target_index, target_hz)
                        self._target_check_at = now + 0.50
                    if target_hz != expected_target_hz:
                        self._failure_index = target_index
                        target.set_frequency(expected_target_hz)
                        self._known[target_index] = expected_target_hz
                        self.frequency_callback(target_index, expected_target_hz)
                else:
                    self._failure_index = target_index
                    target_hz = target.read_frequency()
                    self._known[target_index] = target_hz
                    self.frequency_callback(target_index, target_hz)

                # Mode is much less time-sensitive than frequency.  Poll the
                # Master twice per second and the Sub every two seconds;
                # only transmit a mode command when the values actually differ.
                now = time.monotonic()
                if active and now >= self._source_mode_check_at:
                    self._failure_index = source_index
                    source_mode = source.read_mode()
                    source_changed = source_mode != self._known_modes[source_index]
                    self._known_modes[source_index] = source_mode
                    self._source_mode_check_at = now + 0.50
                    if source_changed or now >= self._target_mode_check_at:
                        self._failure_index = target_index
                        target_mode = target.read_mode()
                        self._known_modes[target_index] = target_mode
                        self._target_mode_check_at = now + 2.0
                        if target_mode != source_mode:
                            target.set_mode(source_mode)
                            self._known_modes[target_index] = source_mode
                elif active and now >= self._target_mode_check_at:
                    self._failure_index = target_index
                    target_mode = target.read_mode()
                    self._known_modes[target_index] = target_mode
                    self._target_mode_check_at = now + 2.0
                    source_mode = self._known_modes[source_index]
                    if source_mode is not None and target_mode != source_mode:
                        target.set_mode(source_mode)
                        self._known_modes[target_index] = source_mode

                delay = max(0.0, self.poll_interval - (time.monotonic() - started))
                self._wake.wait(delay if active else max(delay, 0.20))
                self._wake.clear()
        except CATError as exc:
            if not self._stop.is_set():
                self.failure_callback(self._failure_index, str(exc))
        except Exception as exc:
            if not self._stop.is_set():
                self.failure_callback(self._failure_index, f"Unexpected mirror error: {exc}")
        finally:
            if self._memory_tx_plan is not None and not self._transmitting:
                try:
                    source = self.endpoints[self._source_index]
                    source.restore_memory_transmit(self._memory_tx_plan)
                except Exception:
                    pass
                self._memory_tx_plan = None
            if self.routing_enabled and self._was_active:
                try:
                    target = self.endpoints[1 - self._source_index]
                    self._deactivate_listener_route(target)
                except Exception:
                    pass
                self._was_active = False
