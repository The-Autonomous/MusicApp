"""Radio resource checks use fake replies; no LAN scan or audio device opens."""
import asyncio
from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radioClient import RadioClient, _read_limited_response
from radioIpScanner import IPRadioScanner


class FakeResponse:
    def __init__(self, blocks, encoding="utf-8"):
        self.blocks = blocks
        self.encoding = encoding
        self.closed = False
        self.read_blocks = 0

    def iter_content(self, chunk_size):
        for block in self.blocks:
            self.read_blocks += 1
            yield block

    def raise_for_status(self):
        pass

    def close(self):
        self.closed = True


class RadioResponseTests(unittest.TestCase):
    def test_exact_limit_and_unicode(self):
        response = FakeResponse(["song 🎵".encode("utf-8")])
        self.assertEqual(_read_limited_response(response, 9), "song 🎵")

    def test_oversized_body_stops_before_consuming_tail(self):
        response = FakeResponse([b"a" * 8192] * 20)
        with self.assertRaisesRegex(ValueError, "size limit"):
            _read_limited_response(response, 128 * 1024)
        self.assertEqual(response.read_blocks, 17)

    def test_cancelled_body_is_not_accumulated(self):
        cancel = Event()
        cancel.set()
        with self.assertRaises(InterruptedError):
            _read_limited_response(FakeResponse([b"data"]), 64, cancel)

    def test_invalid_charset_falls_back_to_utf8(self):
        self.assertEqual(_read_limited_response(
            FakeResponse([b"song"], "missing-charset"), 64), "song")

    def test_status_response_limit_also_closes_response(self):
        response = FakeResponse([b"a" * 8192] * 10)
        session = Mock()
        session.get.return_value = response
        client = RadioClient(Mock(), "127.0.0.1")
        with self.assertRaisesRegex(ValueError, "size limit"):
            client._fetch_data(session)
        self.assertTrue(response.closed)
        self.assertFalse(client._responses)
        self.assertEqual(response.read_blocks, 9)
        self.assertTrue(session.get.call_args.kwargs["stream"])

    def test_stop_closes_only_listener_owned_response(self):
        owned = FakeResponse([b"data"])
        unrelated = FakeResponse([b"other window"])
        session = Mock()
        session.get.return_value = owned
        player = Mock()
        client = RadioClient(player)
        client._running.set()
        with client._response(session, "http://example.invalid", timeout=(2, 2)):
            client.stopListening()
            self.assertTrue(owned.closed)
            self.assertFalse(unrelated.closed)
        self.assertFalse(client._responses)
        player.stop.assert_called_once()

    def test_stop_during_connection_does_not_leave_response_open(self):
        response = FakeResponse([b"data"])
        client = RadioClient(Mock())
        session = Mock()

        def connect(*args, **kwargs):
            client._stop.set()
            return response

        session.get.side_effect = connect
        with self.assertRaises(InterruptedError):
            with client._response(session, "http://example.invalid", timeout=(2, 2)):
                self.fail("Cancelled response should not be used")
        self.assertTrue(response.closed)
        self.assertFalse(client._responses)


class FakeAsyncSession:
    def __init__(self, **kwargs):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class RadioScannerTests(unittest.IsolatedAsyncioTestCase):
    def scanner(self, **options):
        with patch("radioIpScanner.get_local_ip", return_value="127.0.0.1"):
            return IPRadioScanner(**options)

    def fake_session(self):
        return patch("radioIpScanner.ClientSession", FakeAsyncSession)

    async def test_scan_allocates_only_four_workers_and_visits_every_host(self):
        scanner = self.scanner(concurrency=100)
        active = maximum = visited = 0

        async def fetch(session, address):
            nonlocal active, maximum, visited
            active += 1
            maximum = max(maximum, active)
            visited += 1
            await asyncio.sleep(0)
            active -= 1

        scanner._fetch = fetch
        create_task = asyncio.create_task
        with self.fake_session(), patch("radioIpScanner.TCPConnector"), patch(
                "radioIpScanner.asyncio.create_task", wraps=create_task) as created:
            await scanner.getAllIps(lambda *args: None)
        self.assertEqual(created.call_count, 4)
        self.assertLessEqual(maximum, 4)
        self.assertEqual(visited, 254)
        self.assertEqual(active, 0)

    async def test_cancellation_drains_all_request_workers(self):
        cancel = Event()
        scanner = self.scanner(cancel=cancel)
        entered = asyncio.Event()
        never = asyncio.Event()
        active = cancelled = 0

        async def fetch(session, address):
            nonlocal active, cancelled
            active += 1
            entered.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise
            finally:
                active -= 1

        scanner._fetch = fetch
        with self.fake_session(), patch("radioIpScanner.TCPConnector"):
            task = asyncio.create_task(scanner.getAllIps(lambda *args: None))
            await asyncio.wait_for(entered.wait(), 1)
            cancel.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(active, 0)
        self.assertEqual(cancelled, 4)

    async def test_first_station_cancels_other_workers(self):
        scanner = self.scanner()
        never = asyncio.Event()
        seen = []
        active = 0

        async def fetch(session, address):
            nonlocal active
            active += 1
            try:
                if address == "127.0.0.1":
                    await asyncio.sleep(0)
                    return address, "Station", "0"
                await never.wait()
            finally:
                active -= 1

        scanner._fetch = fetch
        with self.fake_session(), patch("radioIpScanner.TCPConnector"):
            await asyncio.wait_for(scanner.getFirstIp(lambda *args: seen.append(args)), 1)
        self.assertEqual(seen, [("127.0.0.1", "Station", "0")])
        self.assertEqual(active, 0)

    async def test_pre_cancelled_scan_creates_no_session(self):
        cancel = Event()
        cancel.set()
        scanner = self.scanner(cancel=cancel)
        with patch("radioIpScanner.ClientSession") as session:
            await scanner.getAllIps(lambda *args: None)
        session.assert_not_called()

    async def test_scanner_closes_oversized_response(self):
        scanner = self.scanner()

        class Response:
            status = 200
            closed = False

            @property
            def content(self):
                return self

            async def iter_chunked(self, size):
                for _ in range(10):
                    yield b"a" * 8192

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.closed = True

        response = Response()
        session = Mock()
        session.get.return_value = response
        result = await scanner._fetch(session, "127.0.0.2")
        self.assertIsNone(result)
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
