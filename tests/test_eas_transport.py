import unittest
from http.client import IncompleteRead
from unittest.mock import patch
from urllib.error import URLError

from environments.eas.transport import Bridge, validate_url


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_credentials_and_plaintext_remote_urls_are_rejected(self):
        for url in ['http://example.com/eas', 'https://user:secret@example.com', 'https://example.com/?token=secret', 'https://example.com/#secret', 'file:///tmp/bridge']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url, local=True)
        self.assertEqual(validate_url('http://127.0.0.1:8080/eas', local=True), 'http://127.0.0.1:8080/eas')
        with self.assertRaises(ValueError):
            validate_url('http://127.0.0.1:8080/eas')

    async def test_transient_failure_reuses_identical_request_bytes_and_id(self):
        bridge = Bridge('http://localhost:8080/eas', 'x' * 40)
        with patch.object(bridge, '_request', side_effect=[IncompleteRead(b'partial'), (202, b'')]) as request:
            self.assertEqual(await bridge.request('POST', '/session/requests/id', {'command':'once'}), (202,b''))
        first, second = request.call_args_list
        self.assertEqual(first.args[:3], second.args[:3])

    async def test_permanent_errors_do_not_retry(self):
        bridge = Bridge('http://localhost:8080/eas', 'x' * 40)
        with patch.object(bridge, '_request', side_effect=RuntimeError('rejected (403)')) as request:
            with self.assertRaisesRegex(RuntimeError, '403'):
                await bridge.request('POST', '/sessions', {})
        self.assertEqual(request.call_count, 1)
