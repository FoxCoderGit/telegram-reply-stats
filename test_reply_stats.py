import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from reply_stats import DEFAULT, awake_seconds, analyze, parse_dt, write_outputs


def msg(ts, who):
    return {'ts': ts, 'who': who, 'kind': 'text'}


class StatisticsTests(unittest.TestCase):
    def test_overnight(self):
        s = {'timezone': 'UTC', 'sleep_start': '00:00', 'sleep_end': '08:00'}
        self.assertEqual(awake_seconds(parse_dt('2026-01-01T23:50Z'), parse_dt('2026-01-02T08:20Z'), s), 1800)

    def test_wrapping_sleep_and_multiple_days(self):
        s = {'timezone': 'UTC', 'sleep_start': '23:00', 'sleep_end': '07:00'}
        self.assertEqual(awake_seconds(parse_dt('2026-01-01T22:00Z'), parse_dt('2026-01-03T08:00Z'), s), 18*3600)
        self.assertEqual(awake_seconds(parse_dt('2026-01-01T23:30Z'), parse_dt('2026-01-02T06:00Z'), s), 0)

    def test_different_recipient_timezone(self):
        s = {'timezone': 'Asia/Barnaul', 'sleep_start': '00:00', 'sleep_end': '08:00'}
        self.assertEqual(awake_seconds(parse_dt('2026-01-01T16:50Z'), parse_dt('2026-01-02T01:20Z'), s), 1800)

    def test_dst_uses_elapsed_time(self):
        s = {'timezone': 'America/New_York', 'sleep_start': '00:00', 'sleep_end': '08:00'}
        self.assertEqual(awake_seconds(parse_dt('2026-03-08T04:00Z'), parse_dt('2026-03-08T13:00Z'), s), 2*3600)

    def test_series_and_calendar_attribution(self):
        cfg = deepcopy(DEFAULT)
        cfg['report_timezone'] = 'UTC'
        messages = [msg('2026-01-31T23:50Z', 'me'), msg('2026-01-31T23:55Z', 'me'), msg('2026-02-01T00:10Z', 'friend')]
        result = analyze(messages, cfg, parse_dt('2026-02-01T01:00Z'))
        self.assertEqual(len(result['events']), 1)
        e = result['events'][0]
        self.assertEqual(e['raw_first_s'], 1200)
        self.assertEqual(e['raw_last_s'], 900)
        self.assertEqual(result['periods']['monthly'][0]['friend']['n'], 1)
        self.assertEqual(result['periods']['monthly'][1]['friend']['n'], 0)
        self.assertIsNone(result['periods']['monthly'][1]['friend']['median_min'])
        self.assertEqual(result['pending']['waiting_for'], 'me')

    def test_long_gap_is_separate(self):
        cfg = deepcopy(DEFAULT)
        result = analyze([msg('2026-01-01T10:00Z', 'me'), msg('2026-01-03T10:00Z', 'friend')], cfg, parse_dt('2026-01-03T11:00Z'))
        self.assertEqual(result['summary']['friend']['n'], 0)
        self.assertEqual(result['summary']['friend']['long_gap_count'], 1)
        self.assertEqual(result['summary']['friend']['including_long_gaps']['n'], 1)

    def test_empty_and_single_sender(self):
        cfg = deepcopy(DEFAULT)
        result = analyze([], cfg, datetime.now(timezone.utc))
        self.assertIsNone(result['pending'])
        self.assertIsNone(result['summary']['me']['median_min'])
        result = analyze([msg('2026-01-01T10:00Z', 'me')], cfg, parse_dt('2026-01-01T11:00Z'))
        self.assertEqual(result['summary']['friend']['n'], 0)
        self.assertEqual(result['pending']['raw_s'], 3600)

    def test_no_private_content_exported(self):
        import json
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            path = write_outputs([msg('2026-01-01T10:00Z', 'me')], deepcopy(DEFAULT), '2026-01-01T10:00Z', '2026-01-01T11:00Z', Path(d))
            with zipfile.ZipFile(path) as z:
                self.assertIsNone(z.testzip())
                payload = json.loads(z.read('stats.json'))
                self.assertEqual(set(payload['messages'][0]), {'ts', 'who', 'kind'})
                self.assertIn('report.html', z.namelist())


if __name__ == '__main__':
    unittest.main()
