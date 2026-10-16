from __future__ import annotations

import unittest

from table_qa.config import validate_config
from table_qa.contracts import PipelineError
from table_qa.rate_limit import ActiveTimer, GeminiSlidingWindowGate, RateLimitSettings


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class GeminiRateLimitTests(unittest.TestCase):
    def gate(self, clock, *, rpm=2, tpm=100, factor=1.0, max_wait=120):
        return GeminiSlidingWindowGate(RateLimitSettings(
            enabled=True, requests_per_minute=rpm,
            input_tokens_per_minute=tpm, window_seconds=60,
            token_estimate_safety_factor=factor, max_wait_seconds=max_wait,
        ), clock=clock.monotonic, sleeper=clock.sleep)

    def test_requests_run_immediately_while_rpm_and_tpm_have_capacity(self):
        clock = FakeClock()
        gate = self.gate(clock, rpm=3, tpm=100)
        first = gate.acquire(20)
        second = gate.acquire(30)
        self.assertEqual(first.waited_seconds, 0)
        self.assertEqual(second.waited_seconds, 0)
        self.assertEqual(clock.now, 0)

    def test_rpm_waits_only_until_oldest_required_record_expires(self):
        clock = FakeClock()
        gate = self.gate(clock, rpm=2, tpm=1000)
        gate.acquire(10)
        clock.now = 10
        gate.acquire(10)
        clock.now = 20
        admission = gate.acquire(10)
        self.assertAlmostEqual(clock.now, 60.000001)
        self.assertAlmostEqual(admission.waited_seconds, 40.000001)
        self.assertEqual(admission.wait_reason, "rpm")

    def test_tpm_waits_until_enough_token_records_expire(self):
        clock = FakeClock()
        gate = self.gate(clock, rpm=10, tpm=100)
        gate.acquire(40)
        clock.now = 10
        gate.acquire(40)
        clock.now = 20
        admission = gate.acquire(50)
        self.assertAlmostEqual(clock.now, 60.000001)
        self.assertEqual(admission.wait_reason, "tpm")

    def test_commit_actual_usage_releases_reserved_capacity(self):
        clock = FakeClock()
        gate = self.gate(clock, rpm=10, tpm=100, factor=2.0)
        first = gate.acquire(40)
        gate.commit(first, 20)
        second = gate.acquire(40)
        self.assertEqual(second.waited_seconds, 0)

    def test_single_request_over_tpm_fails_without_sleep(self):
        clock = FakeClock()
        gate = self.gate(clock, rpm=10, tpm=100, factor=1.25)
        with self.assertRaisesRegex(PipelineError, "exceeding"):
            gate.acquire(81)
        self.assertEqual(clock.now, 0)

    def test_gemini_rate_limit_config_is_strict(self):
        base = {
            "llm": {"provider": "gemini", "model": "gemini-3.1-flash-lite",
                    "provider_options": {"gemini": {"rate_limit": {
                        "enabled": True, "requests_per_minute": 5,
                        "input_tokens_per_minute": 100000,
                    }}}},
        }
        config = validate_config(base)
        self.assertTrue(config.llm.provider_options["gemini"]["rate_limit"]["enabled"])
        bad = {"llm": {"provider": "gemini", "model": "x",
                       "provider_options": {"gemini": {"rate_limit": {
                           "enabled": True, "requests_per_minute": 0,
                       }}}}}
        with self.assertRaises(PipelineError):
            validate_config(bad)

    def test_active_timer_excludes_only_declared_quota_wait(self):
        clock = FakeClock()
        timer = ActiveTimer(clock=clock.monotonic)
        clock.now = 10
        timer.exclude(6)
        self.assertEqual(timer.elapsed(), 4)
        self.assertEqual(timer.wall_elapsed(), 10)


if __name__ == "__main__":
    unittest.main()
