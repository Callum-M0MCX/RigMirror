import threading
import unittest
from dataclasses import dataclass

from errors import CATError
from mirror import MirrorCoordinator


@dataclass(frozen=True)
class FakeAntennaState:
    active_source: str


class FakeEndpoint:
    def __init__(self, frequency, tx_state=None, mode="LSB"):
        self.frequency = frequency
        self.transmit_frequency = frequency
        self.tx_follows_receive = True
        self.tx_writes = []
        self.writes = []
        self.tx_state = tx_state
        self.mode = mode
        self.mode_writes = []
        self.frequency_reads = 0
        self.mode_reads = 0
        self.antenna_reads = 0
        self.antenna_state = FakeAntennaState("ANT1")
        self.antenna_writes = []
        self.fail_antenna_write = False
        self.reject_antenna_change = False
        self.pending_antenna_rejection = False
        self.tx_reads = 0

    def read_frequency(self):
        self.frequency_reads += 1
        return self.frequency

    def set_frequency(self, frequency_hz):
        self.frequency = frequency_hz
        if self.tx_follows_receive:
            self.transmit_frequency = frequency_hz
        self.writes.append(frequency_hz)

    def read_transmit_frequency(self):
        return self.transmit_frequency

    def set_transmit_frequency(self, frequency_hz):
        self.transmit_frequency = frequency_hz
        self.tx_writes.append(frequency_hz)

    def read_transmitting(self):
        self.tx_reads += 1
        return self.tx_state

    def read_mode(self):
        self.mode_reads += 1
        return self.mode

    def set_mode(self, mode):
        self.mode = mode
        self.mode_writes.append(mode)

    def read_antenna_state(self):
        self.antenna_reads += 1
        if self.pending_antenna_rejection:
            self.pending_antenna_rejection = False
            raise CATError("Unexpected AN response: '?;'")
        return self.antenna_state

    def set_receive_source(self, source):
        if self.fail_antenna_write:
            self.fail_antenna_write = False
            raise RuntimeError("simulated antenna failure")
        if self.reject_antenna_change:
            self.reject_antenna_change = False
            self.pending_antenna_rejection = True
            return
        self.antenna_state = FakeAntennaState(source)
        self.antenna_writes.append(source)

    def restore_antenna_state(self, state):
        self.antenna_state = state
        self.antenna_writes.append(state.active_source)


class MirrorTests(unittest.TestCase):
    def test_alignment_is_applied_only_to_final_sub_frequency(self):
        master = FakeEndpoint(21_200_000)
        sub = FakeEndpoint(7_100_000)
        updates = []
        coordinator = MirrorCoordinator(
            (master, sub), lambda index, hz: updates.append((index, hz)), self.fail,
            poll_interval=0.005, alignment_offsets={"15m": 20},
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: sub.frequency == 21_200_020))
        finally:
            coordinator.stop()
        self.assertEqual(master.frequency, 21_200_000)
        self.assertIn((0, 21_200_000), updates)
        self.assertIn((1, 21_200_020), updates)

    def test_live_band_change_selects_that_bands_alignment(self):
        master = FakeEndpoint(21_200_000)
        sub = FakeEndpoint(21_200_000)
        coordinator = MirrorCoordinator(
            (master, sub), lambda *_: None, self.fail, poll_interval=0.005,
            alignment_offsets={"15m": 20, "20m": -10},
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: sub.frequency == 21_200_020))
            master.frequency = 14_200_000
            self.assertTrue(self._wait_for(lambda: sub.frequency == 14_199_990))
        finally:
            coordinator.stop()

    def test_inactive_alignment_preview_tunes_sub_only(self):
        master = FakeEndpoint(21_200_000)
        sub = FakeEndpoint(21_200_000)
        coordinator = MirrorCoordinator(
            (master, sub), lambda *_: None, self.fail, poll_interval=0.005,
        )
        coordinator.start_monitoring()
        try:
            coordinator.request_sub_frequency(21_200_030)
            self.assertTrue(self._wait_for(lambda: sub.frequency == 21_200_030))
        finally:
            coordinator.stop()
        self.assertEqual(master.frequency, 21_200_000)

    def test_memory_remains_in_master_domain_with_aligned_sub(self):
        master = FakeEndpoint(7_151_000)
        sub = FakeEndpoint(7_151_000)
        coordinator = MirrorCoordinator(
            (master, sub), lambda *_: None, self.fail, poll_interval=0.005,
            alignment_offsets={"40m": 20},
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            coordinator.arm_memory(7_151_000)
            coordinator.request_tune(7_150_470)
            self.assertTrue(self._wait_for(
                lambda: master.frequency == 7_150_470 and sub.frequency == 7_150_490
            ))
            coordinator.set_transmitting(True)
            self.assertTrue(self._wait_for(
                lambda: master.transmit_frequency == 7_151_000
            ))
            self.assertEqual(master.frequency, 7_150_470)
            self.assertEqual(sub.frequency, 7_150_490)
        finally:
            coordinator.stop()

    def test_split_moves_master_up_and_holds_aligned_sub_on_dx(self):
        master = FakeEndpoint(7_138_000, tx_state=False)
        sub = FakeEndpoint(7_138_000)
        coordinator = MirrorCoordinator(
            (master, sub), lambda *_: None, self.fail, poll_interval=0.005,
            alignment_offsets={"40m": 20},
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: sub.frequency == 7_138_020))
            coordinator.start_split(7_138_000)
            self.assertTrue(self._wait_for(lambda: master.frequency == 7_143_000))
            self.assertEqual(sub.frequency, 7_138_020)

            master.frequency = 7_146_400
            self.assertTrue(self._wait_for(lambda: coordinator._known[0] == 7_146_400))
            self.assertEqual(sub.frequency, 7_138_020)

            sub.frequency = 7_150_000
            self.assertTrue(self._wait_for(lambda: sub.frequency == 7_138_020, timeout=0.8))
        finally:
            coordinator.stop()

    def test_return_from_split_recalls_dx_and_resumes_mirroring(self):
        master = FakeEndpoint(7_138_000, tx_state=False)
        sub = FakeEndpoint(7_138_000)
        coordinator = MirrorCoordinator(
            (master, sub), lambda *_: None, self.fail, poll_interval=0.005,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            coordinator.start_split(7_138_000)
            self.assertTrue(self._wait_for(lambda: master.frequency == 7_143_000))
            master.frequency = 7_146_000
            self.assertTrue(self._wait_for(lambda: coordinator._known[0] == 7_146_000))
            self.assertEqual(coordinator.return_from_split(), 7_138_000)
            self.assertTrue(self._wait_for(
                lambda: master.frequency == 7_138_000 and sub.frequency == 7_138_000
            ))
            master.frequency = 7_139_000
            self.assertTrue(self._wait_for(lambda: sub.frequency == 7_139_000))
        finally:
            coordinator.stop()

    def test_split_does_not_move_either_frequency_on_tx(self):
        master = FakeEndpoint(7_138_000, tx_state=False)
        sub = FakeEndpoint(7_138_000)
        coordinator = MirrorCoordinator(
            (master, sub), lambda *_: None, self.fail, poll_interval=0.005,
            tx_source="ANT2",
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            coordinator.arm_memory(7_138_000)
            coordinator.start_split(7_138_000)
            self.assertTrue(self._wait_for(lambda: master.frequency == 7_143_000))
            master.frequency = 7_146_000
            self.assertTrue(self._wait_for(lambda: coordinator._known[0] == 7_146_000))
            master.tx_state = True
            self.assertTrue(self._wait_for(
                lambda: coordinator._transmitting
                and sub.antenna_state.active_source == "ANT2"
            ))
            self.assertEqual(master.frequency, 7_146_000)
            self.assertEqual(sub.frequency, 7_138_000)
            master.tx_state = False
            self.assertTrue(self._wait_for(
                lambda: not coordinator._transmitting
                and sub.antenna_state.active_source == "ANT1"
            ))
            self.assertEqual(master.frequency, 7_146_000)
            self.assertEqual(sub.frequency, 7_138_000)
        finally:
            coordinator.stop()

    def test_shared_main_sub_starts_in_sync_without_antenna_routing(self):
        master = FakeEndpoint(7_175_500, tx_state=False, mode="LSB")
        sub = FakeEndpoint(14_241_230, mode="USB")
        route_states = []
        coordinator = MirrorCoordinator(
            (master, sub), lambda *_: None, self.fail,
            poll_interval=0.005, tx_source="RX ANT", routing_enabled=False,
            route_callback=lambda tx, source: route_states.append((tx, source)),
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(
                lambda: sub.frequency == master.frequency and sub.mode == master.mode
            ))
            master.tx_state = True
            self.assertTrue(self._wait_for(lambda: coordinator._transmitting))
            master.tx_state = False
            self.assertTrue(self._wait_for(lambda: not coordinator._transmitting))
        finally:
            coordinator.stop()

        self.assertEqual(sub.antenna_reads, 0)
        self.assertEqual(sub.antenna_writes, [])
        self.assertEqual(route_states, [])

    def test_roles_swap_without_swapping_endpoints(self):
        endpoints = (FakeEndpoint(14_195_000), FakeEndpoint(7_100_000))
        changed = threading.Event()

        def updated(index, frequency_hz):
            if index == 1 and frequency_hz == 14_195_000:
                changed.set()

        coordinator = MirrorCoordinator(endpoints, updated, self.fail, poll_interval=0.01)
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(changed.wait(0.5))
            self.assertEqual(endpoints[1].frequency, 14_195_000)
            coordinator.set_source(1)
            coordinator.request_tune(14_196_000)
            for _ in range(50):
                if endpoints[0].frequency == 14_196_000:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(endpoints[0].frequency, 14_196_000)
            self.assertEqual(endpoints[1].frequency, 14_196_000)
        finally:
            coordinator.stop()

    def test_memory_switches_home_on_tx_and_listen_on_rx(self):
        endpoints = (FakeEndpoint(7_151_000), FakeEndpoint(7_151_000))
        coordinator = MirrorCoordinator(endpoints, lambda *_: None, self.fail, poll_interval=0.005)
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            coordinator.arm_memory(7_151_000)
            coordinator.request_tune(7_150_470)
            self.assertTrue(self._wait_for(lambda: endpoints[1].frequency == 7_150_470))
            coordinator.set_transmitting(True)
            self.assertTrue(self._wait_for(
                lambda: endpoints[0].transmit_frequency == 7_151_000
            ))
            self.assertEqual(endpoints[1].frequency, 7_150_470)
            coordinator.set_transmitting(False)
            self.assertTrue(self._wait_for(
                lambda: not coordinator._transmitting
                and all(e.frequency == 7_150_470 for e in endpoints)
            ))
            self.assertEqual(coordinator.cancel_memory(), 7_151_000)
            self.assertTrue(self._wait_for(lambda: all(e.frequency == 7_151_000 for e in endpoints)))
        finally:
            coordinator.stop()

    def test_abandon_memory_does_not_recall_stale_home(self):
        endpoints = (FakeEndpoint(3_784_000), FakeEndpoint(3_784_000))
        coordinator = MirrorCoordinator(endpoints, lambda *_: None, self.fail)
        coordinator.arm_memory(3_784_000)
        coordinator.request_tune(3_783_990)
        coordinator.abandon_memory()
        controls = coordinator._snapshot_controls()
        self.assertIsNone(controls[2])       # no pending stale tune
        self.assertFalse(controls[3])        # M is disarmed
        self.assertFalse(controls[7])        # no return-home operation

    def test_memory_polls_master_ptt_when_endpoint_supports_it(self):
        master = FakeEndpoint(7_151_000, tx_state=False)
        slave = FakeEndpoint(7_151_000)
        states = []
        coordinator = MirrorCoordinator(
            (master, slave), lambda *_: None, self.fail,
            poll_interval=0.005, transmit_callback=states.append,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            coordinator.arm_memory(7_151_000)
            coordinator.request_tune(7_150_470)
            self.assertTrue(self._wait_for(lambda: slave.frequency == 7_150_470))
            master.tx_state = True
            self.assertTrue(self._wait_for(lambda: master.transmit_frequency == 7_151_000))
            self.assertEqual(slave.frequency, 7_150_470)
            master.tx_state = False
            self.assertTrue(self._wait_for(lambda: states == [True, False]))
            self.assertEqual(slave.frequency, 7_150_470)
            self.assertEqual(states, [True, False])
        finally:
            coordinator.stop()

    def test_repeated_memory_tx_rx_cycles_do_not_stall(self):
        endpoints = (FakeEndpoint(3_784_000), FakeEndpoint(3_784_000))
        states = []
        coordinator = MirrorCoordinator(
            endpoints, lambda *_: None, self.fail,
            poll_interval=0.002, transmit_callback=states.append,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            coordinator.arm_memory(3_784_000)
            coordinator.request_tune(3_783_990)
            self.assertTrue(self._wait_for(lambda: endpoints[1].frequency == 3_783_990))
            for _ in range(100):
                coordinator.set_transmitting(True)
                self.assertTrue(self._wait_for(
                    lambda: endpoints[0].transmit_frequency == 3_784_000
                ))
                self.assertEqual(endpoints[1].frequency, 3_783_990)
                coordinator.set_transmitting(False)
                self.assertTrue(self._wait_for(
                    lambda: not coordinator._transmitting
                    and all(endpoint.frequency == 3_783_990 for endpoint in endpoints)
                ))
            self.assertEqual(states, [True, False] * 100)
        finally:
            coordinator.stop()
        self.assertFalse(coordinator.running)

    def test_mode_is_written_only_when_listener_differs(self):
        master = FakeEndpoint(7_151_000, mode="LSB")
        listener = FakeEndpoint(7_151_000, mode="USB")
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None, self.fail, poll_interval=0.005
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: listener.mode == "LSB"))
            self.assertEqual(listener.mode_writes, ["LSB"])
            threading.Event().wait(0.05)
            self.assertEqual(listener.mode_writes, ["LSB"])
        finally:
            coordinator.stop()

    def test_listener_diverts_on_tx_and_restores_on_rx_and_stop(self):
        master = FakeEndpoint(7_151_000, tx_state=False)
        listener = FakeEndpoint(7_151_000)
        route_states = []
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None, self.fail,
            poll_interval=0.005, tx_source="ANT2",
            route_callback=lambda tx, source: route_states.append((tx, source)),
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: route_states))
            master.tx_state = True
            self.assertTrue(self._wait_for(lambda: listener.antenna_state.active_source == "ANT2"))
            master.tx_state = False
            self.assertTrue(self._wait_for(lambda: listener.antenna_state.active_source == "ANT1"))
        finally:
            coordinator.stop()
        self.assertEqual(route_states[:3], [(False, "ANT1"), (True, "ANT2"), (False, "ANT1")])

    def test_listener_uses_start_snapshot_without_a_pre_tx_query(self):
        master = FakeEndpoint(7_151_000, tx_state=False)
        listener = FakeEndpoint(7_151_000)
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None, self.fail,
            poll_interval=0.005, tx_source="ANT2",
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: coordinator._listener_normal_state is not None))
            reads_before_tx = listener.antenna_reads
            # A manual change after Mirror starts is deliberately not queried
            # on the urgent TX edge; the captured start state is restored.
            listener.antenna_state = FakeAntennaState("RX ANT")
            master.tx_state = True
            self.assertTrue(self._wait_for(lambda: listener.antenna_state.active_source == "ANT2"))
            self.assertEqual(listener.antenna_reads, reads_before_tx + 1)  # confirmation only
            master.tx_state = False
            self.assertTrue(self._wait_for(lambda: listener.antenna_state.active_source == "ANT1"))
        finally:
            coordinator.stop()

    def test_stopping_mirror_restores_pre_mirror_antenna_state(self):
        master = FakeEndpoint(7_151_000, tx_state=False)
        listener = FakeEndpoint(7_151_000)
        listener.antenna_state = FakeAntennaState("RX ANT")
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None, self.fail,
            poll_interval=0.005, tx_source="ANT2",
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        self.assertTrue(self._wait_for(lambda: coordinator.running))
        self.assertEqual(listener.antenna_writes, [])
        master.tx_state = True
        self.assertTrue(self._wait_for(lambda: listener.antenna_state.active_source == "ANT2"))
        coordinator.stop()
        self.assertEqual(listener.antenna_state.active_source, "RX ANT")

    def test_live_routing_change_applies_immediately_during_tx(self):
        master = FakeEndpoint(7_151_000, tx_state=False)
        listener = FakeEndpoint(7_151_000)
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None, self.fail,
            poll_interval=0.005, tx_source="ANT2",
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            master.tx_state = True
            self.assertTrue(self._wait_for(lambda: listener.antenna_state.active_source == "ANT2"))
            coordinator.set_routing("RX ANT")
            self.assertTrue(self._wait_for(lambda: listener.antenna_state.active_source == "RX ANT"))
            coordinator.set_routing("NO CHANGE")
            self.assertTrue(self._wait_for(lambda: listener.antenna_state.active_source == "ANT1"))
        finally:
            coordinator.stop()

    def test_routing_failure_stops_mirror_without_connection_failure(self):
        master = FakeEndpoint(7_151_000, tx_state=False)
        listener = FakeEndpoint(7_151_000)
        route_failures = []
        connection_failures = []
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None,
            lambda index, message: connection_failures.append((index, message)),
            poll_interval=0.005, tx_source="ANT2",
            routing_failure_callback=route_failures.append,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: coordinator.running))
            listener.fail_antenna_write = True
            master.tx_state = True
            self.assertTrue(self._wait_for(lambda: route_failures))
            self.assertTrue(coordinator.running)
            self.assertEqual(connection_failures, [])
            self.assertEqual(listener.antenna_state.active_source, "ANT1")
        finally:
            coordinator.stop()

    def test_delayed_antenna_rejection_is_routing_failure_not_cat_loss(self):
        master = FakeEndpoint(3_785_720, tx_state=False)
        listener = FakeEndpoint(3_785_720)
        route_failures = []
        connection_failures = []
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None,
            lambda index, message: connection_failures.append((index, message)),
            poll_interval=0.005, tx_source="ANT2",
            routing_failure_callback=route_failures.append,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: coordinator.running))
            coordinator.arm_memory(3_785_720)
            coordinator.request_tune(3_785_710)
            self.assertTrue(self._wait_for(lambda: listener.frequency == 3_785_710))
            listener.reject_antenna_change = True
            master.tx_state = True
            self.assertTrue(self._wait_for(lambda: route_failures))
            self.assertIn("Unexpected AN response", route_failures[0])
            self.assertEqual(connection_failures, [])
            self.assertTrue(coordinator.running)
            self.assertEqual(master.frequency, 3_785_710)
            self.assertEqual(listener.frequency, 3_785_710)
        finally:
            coordinator.stop()

    def test_listener_tx_state_is_not_polled_on_master_tx(self):
        master = FakeEndpoint(3_785_720, tx_state=False)
        listener = FakeEndpoint(3_785_720, tx_state=True)
        route_failures = []
        connection_failures = []
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None,
            lambda index, message: connection_failures.append((index, message)),
            poll_interval=0.005, tx_source="ANT2",
            routing_failure_callback=route_failures.append,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            coordinator.arm_memory(3_785_720)
            coordinator.request_tune(3_785_710)
            self.assertTrue(self._wait_for(lambda: listener.frequency == 3_785_710))
            master.tx_state = True
            self.assertTrue(self._wait_for(
                lambda: listener.antenna_state.active_source == "ANT2"
            ))
            self.assertEqual(listener.tx_reads, 0)
            self.assertEqual(listener.antenna_writes, ["ANT2"])
            self.assertEqual(route_failures, [])
            self.assertEqual(connection_failures, [])
            self.assertEqual(master.frequency, 3_785_710)
            self.assertEqual(listener.frequency, 3_785_710)
        finally:
            coordinator.stop()

    def test_routine_listener_and_mode_polling_pauses_during_tx(self):
        master = FakeEndpoint(3_785_720, tx_state=False)
        listener = FakeEndpoint(3_785_720)
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None, self.fail,
            poll_interval=0.005, tx_source="NO CHANGE",
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: master.mode_reads > 0))
            master.tx_state = True
            self.assertTrue(self._wait_for(lambda: coordinator._transmitting))
            listener_reads = listener.frequency_reads
            master_modes = master.mode_reads
            listener_modes = listener.mode_reads
            threading.Event().wait(0.05)
            self.assertEqual(listener.frequency_reads, listener_reads)
            self.assertEqual(master.mode_reads, master_modes)
            self.assertEqual(listener.mode_reads, listener_modes)
        finally:
            coordinator.stop()

    def test_routing_failure_during_tx_defers_restore_until_rx(self):
        master = FakeEndpoint(7_151_000, tx_state=False)
        listener = FakeEndpoint(7_151_000)
        route_failures = []
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None, self.fail,
            poll_interval=0.005, tx_source="ANT2",
            routing_failure_callback=route_failures.append,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            master.tx_state = True
            self.assertTrue(self._wait_for(lambda: listener.antenna_state.active_source == "ANT2"))
            listener.fail_antenna_write = True
            coordinator.set_routing("RX ANT")
            self.assertTrue(self._wait_for(lambda: route_failures))
            self.assertEqual(listener.antenna_state.active_source, "ANT2")
            master.tx_state = False
            self.assertTrue(self._wait_for(lambda: listener.antenna_state.active_source == "ANT1"))
        finally:
            coordinator.stop()

    def test_m_targets_actual_master_transmit_vfo_and_never_moves_sub(self):
        master = FakeEndpoint(1_849_960)
        master.tx_follows_receive = False
        master.transmit_frequency = 1_849_960
        listener = FakeEndpoint(1_849_960)
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None, self.fail, poll_interval=0.005,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            coordinator.arm_memory(1_850_000)
            self.assertTrue(self._wait_for(lambda: coordinator._memory_prepared_for_tx))
            coordinator.set_transmitting(True)
            self.assertTrue(self._wait_for(
                lambda: master.transmit_frequency == 1_850_000
            ))
            self.assertEqual(master.tx_writes, [1_850_000])
            self.assertEqual(listener.frequency, 1_849_960)
            self.assertEqual(listener.writes, [])
        finally:
            coordinator.stop()

    def test_parking_frequency_is_applied_and_restored(self):
        master = FakeEndpoint(14_200_000, tx_state=False)
        listener = FakeEndpoint(14_200_000)
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None, self.fail, poll_interval=0.005,
            tx_action="PARKING FREQUENCY", parking_frequency=10_100_000,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            master.tx_state = True
            self.assertTrue(self._wait_for(lambda: listener.frequency == 10_100_000))
            master.tx_state = False
            self.assertTrue(self._wait_for(lambda: listener.frequency == 14_200_000))
        finally:
            coordinator.stop()

    def test_above_30mhz_pauses_writes_and_resumes_automatically(self):
        master = FakeEndpoint(50_144_000)
        listener = FakeEndpoint(14_200_000)
        range_events = []
        coordinator = MirrorCoordinator(
            (master, listener), lambda *_: None, self.fail, poll_interval=0.005,
            range_callback=lambda paused, hz: range_events.append((paused, hz)),
            frequency_ceiling_hz=30_000_000,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: range_events == [(True, 50_144_000)]))
            self.assertEqual(listener.frequency, 14_200_000)
            master.frequency = 29_100_000
            self.assertTrue(self._wait_for(lambda: listener.frequency == 29_100_000))
            self.assertIn((False, 29_100_000), range_events)
        finally:
            coordinator.stop()

    def test_connection_failure_identifies_the_failed_endpoint(self):
        class FailedSub(FakeEndpoint):
            def read_frequency(self):
                raise CATError("Sub went away")

        failures = []
        coordinator = MirrorCoordinator(
            (FakeEndpoint(7_100_000), FailedSub(7_100_000)),
            lambda *_: None,
            lambda index, message: failures.append((index, message)),
            poll_interval=0.005,
        )
        coordinator.set_active(True)
        coordinator.start_monitoring()
        try:
            self.assertTrue(self._wait_for(lambda: bool(failures)))
            self.assertEqual(failures[0][0], 1)
            self.assertIn("Sub went away", failures[0][1])
        finally:
            coordinator.stop()

    @staticmethod
    def _wait_for(predicate, timeout=0.5):
        event = threading.Event()
        for _ in range(int(timeout / 0.005)):
            if predicate():
                return True
            event.wait(0.005)
        return predicate()


if __name__ == "__main__":
    unittest.main()
