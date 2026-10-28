"""Tests for the notify POST retry wrapper `_post_json_retry`."""

from __future__ import annotations

import unittest

import fund_tracker.notify as notify
from fund_tracker.notify import RETRY_SLEEP_BASE, _post_json_retry


class FakeResponse:
    def __init__(self):
        self.status_code = 200


class FlakyPost:
    """requests.post stand-in that fails the first N times then succeeds."""

    def __init__(self, failures: int, last_exc: Exception | None = None):
        self.failures = failures
        self.calls = 0
        self.last_exc = last_exc or ConnectionError("connection reset")

    def __call__(self, url, json=None, timeout=None):  # noqa: A002
        self.calls += 1
        if self.calls <= self.failures:
            raise self.last_exc
        return FakeResponse()


class AlwaysFailingPost(FlakyPost):
    def __init__(self):
        super().__init__(failures=10**9)


class PostJsonRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_post = notify.requests.post
        self.sleeps: list[float] = []

    def tearDown(self) -> None:
        notify.requests.post = self._original_post

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def test_immediate_success_single_attempt(self) -> None:
        post = FlakyPost(failures=0)
        notify.requests.post = post
        response, attempt = _post_json_retry(
            "https://example", {}, sleep_for=self._sleep
        )
        self.assertEqual(post.calls, 1)
        self.assertEqual(attempt, 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(response.status_code, 200)

    def test_recovers_by_third_attempt(self) -> None:
        post = FlakyPost(failures=2)
        notify.requests.post = post
        response, attempt = _post_json_retry(
            "https://example", {}, attempts=3, sleep_for=self._sleep
        )
        self.assertEqual(post.calls, 3)
        self.assertEqual(attempt, 3)
        # backoff: BASE*1, BASE*2 after the first two failures
        self.assertEqual(self.sleeps, [RETRY_SLEEP_BASE * 1, RETRY_SLEEP_BASE * 2])

    def test_raises_after_exhaustion(self) -> None:
        post = AlwaysFailingPost()
        notify.requests.post = post
        with self.assertRaises(ConnectionError):
            _post_json_retry("https://example", {}, attempts=3, sleep_for=self._sleep)
        self.assertEqual(post.calls, 3)
        # no sleep after the final attempt
        self.assertEqual(self.sleeps, [RETRY_SLEEP_BASE * 1, RETRY_SLEEP_BASE * 2])


if __name__ == "__main__":
    unittest.main()
