"""HTTP waits share the existing service budget; no real requests or retries."""
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import agent_extract as ax
from utils.ingest_budget import ingest_ceiling_sec


class HttpBudgetTests(unittest.TestCase):
    def send(self, method, path, **kwargs):
        with mock.patch.object(ax.urllib.request, 'urlopen') as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = b'{}'
            ax._http(method, 'http://fake' + path, {}, None, **kwargs)
            return open_url.call_args.kwargs['timeout']

    def test_extracted_preview_and_write_wait_for_full_service_budget(self):
        self.assertEqual(self.send('POST', '/api/ingest_extracted'), ingest_ceiling_sec() + 30)

    def test_read_only_and_explicit_budget_are_unchanged(self):
        self.assertEqual(self.send('POST', '/api/ingest_extracted/check'), 120)
        self.assertEqual(self.send('GET', '/health'), 120)
        self.assertEqual(self.send('POST', '/api/ingest_extracted', timeout=10), 10)


if __name__ == '__main__':
    unittest.main()
