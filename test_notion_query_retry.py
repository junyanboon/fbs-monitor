"""Network-free regression for the Sep 13 connection-reset visibility loss."""
import unittest
from unittest.mock import Mock, patch

import requests
import build


def response(status=200, rows=None, cursor=None, headers=None):
    return Mock(status_code=status, text="synthetic error", headers=headers or {},
                json=Mock(return_value={"results": rows or [], "has_more": cursor is not None,
                                        "next_cursor": cursor}))


class QueryRetryTests(unittest.TestCase):
    def run_query(self, replies):
        with patch.object(build.requests, "post", side_effect=replies) as post, \
             patch.object(build.time, "sleep") as sleep:
            rows = build._notion_query("test-token", "test-source", {"page_size": 100})
        return rows, post, sleep

    def test_connection_reset_recovers_on_current_endpoint(self):
        rows, post, sleep = self.run_query([
            requests.ConnectionError("Connection reset by peer"), response(rows=[{"id": "a"}])])
        self.assertEqual(rows, [{"id": "a"}])
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all("/data_sources/" in c.args[0] for c in post.call_args_list))
        sleep.assert_called_once_with(1)

    def test_second_page_retry_preserves_cursor_without_duplicates(self):
        rows, post, _ = self.run_query([response(rows=[{"id": "a"}], cursor="next"),
                                      requests.Timeout(), response(rows=[{"id": "b"}])])
        self.assertEqual(rows, [{"id": "a"}, {"id": "b"}])
        self.assertEqual(post.call_args_list[1].kwargs["json"], post.call_args_list[2].kwargs["json"])
        self.assertEqual(post.call_args_list[2].kwargs["json"]["start_cursor"], "next")

    def test_retryable_http_and_server_backoff(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                _, post, sleep = self.run_query([response(status, headers={"Retry-After": "7"}), response()])
                self.assertEqual(post.call_count, 2)
                sleep.assert_called_once_with(7)

    def test_non_transient_errors_are_not_retried(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status), patch.object(build.requests, "post", return_value=response(status)) as post, \
                 patch.object(build.time, "sleep") as sleep:
                with self.assertRaises(RuntimeError):
                    build._notion_query("token", "ds", {})
                self.assertEqual(post.call_count, 2)  # once per existing endpoint
                sleep.assert_not_called()

    def test_exhaustion_raises_and_does_not_return_partial_rows(self):
        replies = [response(rows=[{"id": "partial"}], cursor="next")]
        replies += [requests.ConnectionError("reset")] * 3 + [response(404)]
        with patch.object(build.requests, "post", side_effect=replies) as post, patch.object(build.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "2025-09-03: reset.*2022-06-28: 404"):
                build._notion_query("token", "ds", {})
            self.assertEqual(post.call_count, 5)

    def test_long_or_unknown_retry_after_is_not_shortened(self):
        for value in ("120", "unknown", "-1"):
            with self.subTest(value=value), patch.object(build.requests, "post", return_value=response(429, headers={"Retry-After": value})) as post, \
                 patch.object(build.time, "sleep") as sleep:
                self.assertEqual(build._notion_query_page("url", {}, {}).status_code, 429)
                self.assertEqual(post.call_count, 1)
                sleep.assert_not_called()

    def test_backoff_is_bounded(self):
        _, post, sleep = self.run_query([requests.Timeout(), requests.Timeout(), response()])
        self.assertEqual(post.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])


if __name__ == "__main__":
    unittest.main()
