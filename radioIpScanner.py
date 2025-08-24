import asyncio, socket, re, ipaddress
from aiohttp import ClientSession, ClientTimeout, ClientConnectorError

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't actually send data, but picks the right outbound interface
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

class IPRadioScanner:
    def __init__(self, timeout: float = 5.0, concurrency: int = 50, debug: bool = True):
        self.timeout   = timeout
        self.semaphore = asyncio.Semaphore(concurrency)
        self.debug     = debug

        host_ip       = get_local_ip()
        self.local_ip = host_ip
        self.network  = ipaddress.ip_network(f"{host_ip}/24", strict=False)
        
        if self.debug:
            print(f"Local IP: {self.local_ip}")
            print(f"Scanning network: {self.network}")
            print(f"Total hosts to scan: {len(list(self.network.hosts()))}")

    async def _fetch(self, session: ClientSession, ip: str):
        if ip == self.local_ip or ip == "0.0.0.0":
            return None
            
        url = f"http://{ip}:8080"
        
        try:
            if self.debug:
                print(f"Trying {url}...")
                
            async with self.semaphore, session.get(url) as resp:
                if self.debug:
                    print(f"  {url} - Status: {resp.status}")
                    
                if resp.status != 200:
                    return None
                    
                text = await resp.text()
                
                if self.debug:
                    print(f"  {url} - Response: {text[:200]}...")  # First 200 chars
                    
        except ClientConnectorError as e:
            if self.debug:
                print(f"  {url} - Connection failed: {e}")
            return None
        except asyncio.TimeoutError:
            if self.debug:
                print(f"  {url} - Timeout")
            return None
        except Exception as e:
            if self.debug:
                print(f"  {url} - Error: {e}")
            return None

        # Parse response for radio data
        title_match = re.search(r'<title>(.*?)</title>', text)
        location_match = re.search(r'<location>(.*?)</location>', text)
        
        if title_match:
            title = title_match.group(1)
            location = location_match.group(1) if location_match else "0"
            
            if self.debug:
                print(f"  ✓ Found radio at {ip}: {title}")
                
            return ip, title, location
            
        return None

    async def getAllIps(self, callback):
        timeout = ClientTimeout(total=self.timeout)
        async with ClientSession(timeout=timeout) as session:
            tasks = [
                asyncio.create_task(self._fetch(session, str(ip)))
                for ip in self.network.hosts()
            ]
            
            results_found = 0
            for coro in asyncio.as_completed(tasks):
                result = await coro
                if result:
                    ip, title, location = result
                    callback(ip, title, location)
                    results_found += 1
                    
            if self.debug:
                print(f"Scan complete. Found {results_found} radio servers.")

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
                    # Cancel remaining tasks
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    return
                    
            if self.debug:
                print("No radio servers found in scan.")

class SimpleRadioScan:
    """
    Synchronous wrapper around IPRadioScanner for easy calls.
    """
    def __init__(self, timeout: float = 5.0, concurrency: int = 50, debug: bool = False):
        self.scanner = IPRadioScanner(timeout=timeout, concurrency=concurrency, debug=debug)

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
        
if __name__ == "__main__":
    def print_result(ip, title, location):
        print(f"Found Radio - IP: {ip}, Title: {title}, Location: {location}")

    scanner = SimpleRadioScan(timeout=3.0, concurrency=100, debug=True)
    
    print("Scanning for all radios...")
    scanner.scan_all(print_result)
    
    print("\nScanning for first radio...")
    scanner.scan_first(print_result)