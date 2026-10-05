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


if __name__ == "__main__": unittest.main()
