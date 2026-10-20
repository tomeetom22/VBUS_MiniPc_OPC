import time
import unittest

from opc_n3_logger import BIN_COLUMNS, CSV_COLUMNS, OPCN3Logger, SensorConfig, console_summary, normalize_reading


class FakeN3:
    def __init__(self, values): self.values, self.off_calls = iter(values), 0
    def on(self): pass
    def off(self): self.off_calls += 1
    def histogram(self):
        value = next(self.values)
        if isinstance(value, Exception): raise value
        return value


class LoggerTests(unittest.TestCase):
    def test_maps_24_bins(self):
        logger = OPCN3Logger(SensorConfig("n3", "COM3", warmup_seconds=0), 3, lambda _: FakeN3([]), lambda _: None)
        row = normalize_reading(logger, {f"Bin {n}": n for n in range(24)}, None)
        self.assertEqual([row[column] for column in BIN_COLUMNS], list(range(24)))

    def test_console_summary_shows_pm_not_bins(self):
        row = {"timestamp_utc": "2026-10-02T12:00:00.000Z", "sensor_id": "n3", "record_status": "ok", "pm1_ug_m3": 1, "pm2_5_ug_m3": 2.5, "pm4_25_ug_m3": "", "pm10_ug_m3": 10}
        output = console_summary(row)
        self.assertIn("PM2.5=2.50", output)
        self.assertNotIn("bin_00", output)

    def test_recovers_after_three_failures(self):
        failed, fresh = FakeN3([RuntimeError("lost")] * 3), FakeN3([])
        devices = iter([failed, fresh])
        logger = OPCN3Logger(SensorConfig("n3", "COM3", warmup_seconds=0), 3, lambda _: next(devices), lambda _: None)
        for _ in range(3): reading, error = logger.poll()
        self.assertIsNone(reading)
        self.assertIn("lost", error)
        self.assertEqual((logger.last_poll_consecutive_failures, logger.recovery_count, failed.off_calls), (3, 1, 1))
        self.assertIn("pm10_ug_m3", CSV_COLUMNS)

    def test_close_releases_the_usb_iss_connection(self):
        device, close_calls = FakeN3([]), []
        device._logger_close_connection = lambda: close_calls.append(True)
        logger = OPCN3Logger(SensorConfig("n3", "COM3", warmup_seconds=0), 3, lambda _: device, lambda _: None)
        logger.device = device

        logger.close()

        self.assertEqual((device.off_calls, close_calls, logger.device), (1, [True], None))

    def test_timed_out_poll_does_not_start_another_poll_on_the_same_sensor(self):
        class SlowN3(FakeN3):
            def histogram(self):
                time.sleep(0.03)
                return {"Bin 0": 1}

        logger = OPCN3Logger(SensorConfig("n3", "COM3", warmup_seconds=0), 3, lambda _: SlowN3([]), lambda _: None)
        self.assertTrue(logger.start_poll())
        reading, error = logger.finish_poll(0.001)
        self.assertIsNone(reading)
        self.assertIn("timed out", error)
        self.assertFalse(logger.start_poll())
        time.sleep(0.04)
        self.assertTrue(logger.start_poll())

    def test_recovery_reuses_known_port_before_adapter_serial_scan(self):
        configs, fresh = [], FakeN3([])

        def factory(config):
            configs.append(config)
            return fresh

        logger = OPCN3Logger(SensorConfig("n3", "COM3", adapter_serial="3030303531343535", warmup_seconds=0), 3, factory, lambda _: None)
        logger.active_port = "COM15"
        logger._recover()

        self.assertEqual([(config.port, config.adapter_serial) for config in configs], [("COM15", None)])
        self.assertEqual(logger.recovery_count, 1)

    def test_initial_connection_uses_configured_port_without_serial_scan(self):
        configs, device = [], FakeN3([{ "Bin 0": 1 }])

        def factory(config):
            configs.append(config)
            return device

        logger = OPCN3Logger(SensorConfig("n3", "COM15", adapter_serial="3030303531343535", warmup_seconds=0), 3, factory, lambda _: None)
        reading, error = logger.poll()

        self.assertEqual(reading, {"Bin 0": 1})
        self.assertIsNone(error)
        self.assertEqual([(config.port, config.adapter_serial) for config in configs], [("COM15", None)])

    def test_recovery_does_not_scan_when_known_port_is_unavailable(self):
        configs = []

        def factory(config):
            configs.append(config)
            raise RuntimeError("COM15 unavailable")

        logger = OPCN3Logger(SensorConfig("n3", "COM3", adapter_serial="3030303531343535", warmup_seconds=0), 3, factory, lambda _: None)
        logger.active_port = "COM15"
        with self.assertRaisesRegex(RuntimeError, "COM15 unavailable"):
            logger._recover()

        self.assertEqual([(config.port, config.adapter_serial) for config in configs], [("COM15", None)])
        self.assertTrue(logger._retry_active_port)

    def test_failed_recovery_retries_known_port_on_the_next_poll(self):
        configs, fresh = [], FakeN3([{ "Bin 0": 1 }])

        def factory(config):
            configs.append(config)
            if len(configs) == 1:
                raise RuntimeError("COM15 unavailable")
            return fresh

        logger = OPCN3Logger(SensorConfig("n3", "COM3", adapter_serial="3030303531343535", warmup_seconds=0), 3, factory, lambda _: None)
        logger.active_port = "COM15"
        with self.assertRaises(RuntimeError):
            logger._recover()

        reading, error = logger.poll()

        self.assertEqual(reading, {"Bin 0": 1})
        self.assertIsNone(error)
        self.assertEqual([(config.port, config.adapter_serial) for config in configs], [("COM15", None), ("COM15", None)])

    def test_failed_recovery_waits_for_three_more_bad_polls(self):
        device = FakeN3([RuntimeError("lost")] * 6)
        logger = OPCN3Logger(SensorConfig("n3", "COM3", warmup_seconds=0), 3, lambda _: device, lambda _: None)
        attempts = []

        def failed_recovery():
            attempts.append(logger.consecutive_failures)
            raise RuntimeError("bridge unavailable")

        logger._recover = failed_recovery  # Simulate a USB--SPI recovery failure.
        for _ in range(6):
            logger.poll()

        self.assertEqual(attempts, [3, 3])
        self.assertEqual(logger.last_poll_consecutive_failures, 3)
        self.assertEqual(logger.consecutive_failures, 0)

    def test_recovers_after_three_all_zero_pm_count_polls(self):
        zero_counts = {f"Bin {n}": 0 for n in range(24)}
        failed, fresh = FakeN3([zero_counts] * 3), FakeN3([])
        devices = iter([failed, fresh])
        logger = OPCN3Logger(SensorConfig("n3", "COM3", warmup_seconds=0), 3, lambda _: next(devices), lambda _: None)

        for _ in range(3):
            reading, error = logger.poll()

        self.assertEqual(reading, zero_counts)
        self.assertIn("all PM count bins were zero", error)
        self.assertEqual((logger.last_poll_consecutive_zero_pm_polls, logger.consecutive_zero_pm_polls, logger.recovery_count, failed.off_calls), (3, 0, 1, 1))

    def test_nonzero_or_incomplete_pm_counts_do_not_trigger_recovery(self):
        nonzero = {f"Bin {n}": 0 for n in range(24)}
        nonzero["Bin 7"] = 0.1
        incomplete = {f"Bin {n}": 0 for n in range(23)}
        device = FakeN3([nonzero, incomplete, nonzero])
        logger = OPCN3Logger(SensorConfig("n3", "COM3", warmup_seconds=0), 3, lambda _: device, lambda _: None)

        for _ in range(3):
            reading, error = logger.poll()
            self.assertIsNotNone(reading)
            self.assertIsNone(error)

        self.assertEqual((logger.consecutive_zero_pm_polls, logger.recovery_count, device.off_calls), (0, 0, 0))


if __name__ == "__main__": unittest.main()
