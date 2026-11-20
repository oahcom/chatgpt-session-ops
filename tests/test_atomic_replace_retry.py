import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "chatgpt_send.py")

SPEC = importlib.util.spec_from_file_location(
    "chatgpt_send_atomic_retry_test_module",
    SCRIPT,
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def make_permission_error(winerror):
    exc = PermissionError("simulated Windows replace failure")
    exc.winerror = winerror
    return exc


class AtomicReplaceRetryTestCase(unittest.TestCase):

    def make_tmp_file(self):
        fd, path = tempfile.mkstemp(prefix="atomic-retry-test-")
        os.close(fd)
        return path

    def test_replace_retries_winerror_5_then_succeeds(self):
        tmp = self.make_tmp_file()
        calls = [
            make_permission_error(5),
            make_permission_error(5),
            None,
        ]

        with mock.patch.object(
            MODULE.os,
            "replace",
            side_effect=calls,
        ) as replace_mock, mock.patch(
            "time.sleep"
        ) as sleep_mock:
            MODULE._replace_with_bounded_retry(tmp, "target.json")

        self.assertEqual(replace_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [0.05, 0.10],
        )

        # The mock does not perform the real rename.
        os.unlink(tmp)

    def test_replace_retries_winerror_32_then_succeeds(self):
        tmp = self.make_tmp_file()
        calls = [
            make_permission_error(32),
            None,
        ]

        with mock.patch.object(
            MODULE.os,
            "replace",
            side_effect=calls,
        ) as replace_mock, mock.patch(
            "time.sleep"
        ) as sleep_mock:
            MODULE._replace_with_bounded_retry(tmp, "target.json")

        self.assertEqual(replace_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0.05)
        os.unlink(tmp)

    def test_replace_exhaustion_cleans_tmp_and_raises(self):
        tmp = self.make_tmp_file()
        failure = make_permission_error(5)

        with mock.patch.object(
            MODULE.os,
            "replace",
            side_effect=failure,
        ) as replace_mock, mock.patch(
            "time.sleep"
        ) as sleep_mock:
            with self.assertRaises(PermissionError) as raised:
                MODULE._replace_with_bounded_retry(tmp, "target.json")

        self.assertIs(raised.exception, failure)
        self.assertEqual(replace_mock.call_count, 7)
        self.assertEqual(sleep_mock.call_count, 6)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [0.05, 0.10, 0.20, 0.40, 0.80, 1.00],
        )
        self.assertFalse(os.path.exists(tmp))

    def test_non_target_permission_error_is_not_retried(self):
        tmp = self.make_tmp_file()
        failure = PermissionError("not a recognized Windows sharing error")

        with mock.patch.object(
            MODULE.os,
            "replace",
            side_effect=failure,
        ) as replace_mock, mock.patch(
            "time.sleep"
        ) as sleep_mock:
            with self.assertRaises(PermissionError):
                MODULE._replace_with_bounded_retry(tmp, "target.json")

        replace_mock.assert_called_once()
        sleep_mock.assert_not_called()
        self.assertFalse(os.path.exists(tmp))

    def test_non_permission_error_is_not_retried(self):
        tmp = self.make_tmp_file()
        failure = OSError("simulated disk failure")

        with mock.patch.object(
            MODULE.os,
            "replace",
            side_effect=failure,
        ) as replace_mock, mock.patch(
            "time.sleep"
        ) as sleep_mock:
            with self.assertRaises(OSError):
                MODULE._replace_with_bounded_retry(tmp, "target.json")

        replace_mock.assert_called_once()
        sleep_mock.assert_not_called()
        self.assertFalse(os.path.exists(tmp))


if __name__ == "__main__":
    unittest.main()
