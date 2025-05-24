import asyncio, socket, re, ipaddress
from aiohttp import ClientSession, ClientTimeout

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't actually send data, but picks the right outbound interface
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

class IPRadioScanner:
    def __init__(self, timeout: float = 2.0, concurrency: int = 200):
        self.timeout   = timeout
        self.semaphore = asyncio.Semaphore(concurrency)

        host_ip       = get_local_ip()
        self.local_ip = host_ip
        self.network  = ipaddress.ip_network(f"{host_ip}/24", strict=False)

    async def _fetch(self, session: ClientSession, ip: str):
        if ip == self.local_ip or ip == "0.0.0.0":
            return None
        url = f"http://{ip}:8080"
        try:
            async with self.semaphore, session.get(url) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
        except Exception:
            return None

        title  = re.search(r'<title>(.*?)</title>', text)
        location = re.search(r'<location>(.*?)</location>', text)
        if title:
            return ip, title.group(1), (location.group(1) if location else None)
        return None

    async def getAllIps(self, callback):
        timeout = ClientTimeout(total=self.timeout)
        async with ClientSession(timeout=timeout) as session:
            tasks = [
                asyncio.create_task(self._fetch(session, str(ip)))
                for ip in self.network.hosts()
            ]
            for coro in asyncio.as_completed(tasks):
                result = await coro
                if result:
                    ip, title, location = result
                    callback(ip, title, location)

    async def getFirstIp(self, callback):
        timeout = ClientTimeout(total=self.timeout)
        async with ClientSession(timeout=timeout) as session:
            tasks = [
                asyncio.create_task(self._fetch(session, str(ip)))
                for ip in self.network.hosts()
            ]
            for coro in asyncio.as_completed(tasks):
                result = await coro
                if result:
                    ip, title, location = result
                    callback(ip, title, location)
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    return

class SimpleRadioScan:
    """
    Synchronous wrapper around IPRadioScanner for easy calls.
    """
    def __init__(self, timeout: float = 2.0, concurrency: int = 200):
        self.scanner = IPRadioScanner(timeout=timeout, concurrency=concurrency)

    def scan_all(self, callback):
        """
        Scan the entire /24, calling callback(ip, title, location)
        for every match.
        """
        asyncio.run(self.scanner.getAllIps(callback))

    def scan_first(self, callback):
        """
        Scan until the first match, then call callback(ip, title, location)
        and stop.
        """
        asyncio.run(self.scanner.getFirstIp(callback))