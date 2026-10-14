"""On-demand radio discovery with bounded requests and owned cancellation."""
import asyncio
from html import unescape
import ipaddress
import re
import socket
from threading import Event

from aiohttp import ClientSession, ClientTimeout, TCPConnector


def get_local_ip():
    connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # This selects an outbound interface without transmitting a packet.
        connection.connect(("8.8.8.8", 80))
        return connection.getsockname()[0]
    finally:
        connection.close()


class IPRadioScanner:
    MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(self, timeout=2.0, concurrency=4, debug=False, cancel=None):
        self.timeout = max(0.1, min(30.0, float(timeout)))
        self.concurrency = max(1, min(4, int(concurrency)))
        self.debug = debug
        self.cancel = cancel if cancel is not None else Event()
        self.local_ip = get_local_ip()
        self.network = ipaddress.ip_network(f"{self.local_ip}/24", strict=False)

    async def _fetch(self, session, ip):
        if self.cancel.is_set() or ip == self.local_ip or ip == "0.0.0.0":
            return None
        try:
            async with session.get(f"http://{ip}:8080") as response:
                if response.status != 200:
                    return None
                body = bytearray()
                async for block in response.content.iter_chunked(8192):
                    if self.cancel.is_set() or len(body) + len(block) > self.MAX_RESPONSE_BYTES:
                        return None
                    body.extend(block)
                text = body.decode("utf-8", errors="replace")
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:
            return None
        title = re.search(r"<title>(.*?)</title>", text, re.DOTALL)
        if title:
            location = re.search(r"<location>(.*?)</location>", text, re.DOTALL)
            return ip, unescape(title[1]), location[1] if location else "0"
        return None

    async def _scan(self, callback, first_only=False):
        if self.cancel.is_set():
            return
        # Share an iterator between a fixed number of workers. A /24 does not
        # allocate one coroutine and pending request for each of its 254 hosts.
        addresses = iter(self.network.hosts())
        found_first = asyncio.Event()

        async def worker(session):
            for address in addresses:
                if self.cancel.is_set() or found_first.is_set():
                    return
                result = await self._fetch(session, str(address))
                if self.cancel.is_set() or found_first.is_set():
                    return
                if result:
                    callback(*result)
                    if first_only:
                        found_first.set()
                        return

        timeout = ClientTimeout(total=self.timeout)
        connector = TCPConnector(limit=self.concurrency, limit_per_host=1)
        async with ClientSession(timeout=timeout, connector=connector) as session:
            workers = [asyncio.create_task(worker(session)) for _ in range(self.concurrency)]
            pending = set(workers)
            try:
                while pending and not self.cancel.is_set() and not found_first.is_set():
                    completed, pending = await asyncio.wait(
                        pending, timeout=0.2, return_when=asyncio.FIRST_COMPLETED)
                    for task in completed:
                        task.result()
            finally:
                for task in workers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    async def getAllIps(self, callback):
        await self._scan(callback)

    async def getFirstIp(self, callback):
        await self._scan(callback, first_only=True)


class SimpleRadioScan:
    def __init__(self, timeout=2.0, concurrency=4, debug=False, cancel=None):
        self.scanner = IPRadioScanner(timeout, concurrency, debug, cancel)

    def scan_all(self, callback):
        asyncio.run(self.scanner.getAllIps(callback))

    def scan_first(self, callback):
        asyncio.run(self.scanner.getFirstIp(callback))


if __name__ == "__main__":
    scanner = SimpleRadioScan()
    scanner.scan_all(lambda ip, title, location: print(ip, title, location))
