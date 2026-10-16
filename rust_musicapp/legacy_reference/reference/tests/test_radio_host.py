"""Real Waitress lifecycle checks bind only temporary loopback listeners."""
import importlib.util
from pathlib import Path
import socket
import sys
import threading
from threading import Event
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
import waitress.server

ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = "127.0.0.1"


def isolated_host_module():
    logger = ModuleType("log_loader")
    logger.log_loader = Mock(return_value=Mock())
    logger.OutputRedirector = SimpleNamespace(filename=str(ROOT / "tests" / "unused-host-fixture-log.txt"))
    admin = ModuleType("adminRaise")
    admin.Administrator = Mock(side_effect=AssertionError("Host must never elevate or kill another app"))
    specification = importlib.util.spec_from_file_location("_musicapp_host_fixture", ROOT / "radioMaster.py")
    module = importlib.util.module_from_spec(specification)
    with patch.dict(sys.modules, {"log_loader": logger, "adminRaise": admin}):
        specification.loader.exec_module(module)
    return module


class RadioHostLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.module = isolated_host_module()
        self.player = SimpleNamespace(pause_event=Event(), repeat_event=Event(),
                                      current_volume=0.42, get_bands=lambda: {1000: 0})
        self.hosts = []
        self.maps = []
        self.dispatchers = []
        self.forced_port = 0
        self.addCleanup(self.cleanup_owned_servers)
        real_create_server = self.module.create_server
        real_dispatcher = waitress.server.ThreadedTaskDispatcher

        def capture_dispatcher():
            dispatcher = real_dispatcher()
            self.dispatchers.append(dispatcher)
            return dispatcher

        dispatcher_patch = patch.object(waitress.server, "ThreadedTaskDispatcher", side_effect=capture_dispatcher)
        dispatcher_patch.start()
        self.addCleanup(dispatcher_patch.stop)

        def create_server(app, **options):
            # Force loopback and an OS-assigned port, even if a default changes.
            options["host"] = LOOPBACK
            options["port"] = self.forced_port
            server_map = options.setdefault("map", {})
            self.maps.append(server_map)
            dispatcher = options.get("_dispatcher")
            if dispatcher is not None and dispatcher not in self.dispatchers:
                self.dispatchers.append(dispatcher)
            return real_create_server(app, **options)

        factory_patch = patch.object(self.module, "create_server", side_effect=create_server)
        factory_patch.start()
        self.addCleanup(factory_patch.stop)

    def cleanup_owned_servers(self):
        for host in self.hosts:
            host.close()
        # An expected bind failure may leave partially constructed Waitress
        # objects. Capture/close only maps and dispatchers created by this test.
        for server_map in self.maps:
            for owned_socket in tuple(server_map.values()):
                owned_socket.close()
        for dispatcher in self.dispatchers:
            dispatcher.shutdown(timeout=1)
        for host in self.hosts:
            host._server_thread.join(timeout=1)
            self.assertFalse(host._server_thread.is_alive())

    def make_host(self):
        host = self.module.RadioHost(self.player, port=0, ip=LOOPBACK)
        self.hosts.append(host)
        return host

    def test_loopback_status_then_close_releases_listener_and_workers(self):
        host = self.make_host()
        server = host._server
        listener = server.socket
        port = listener.getsockname()[1]
        self.assertEqual(listener.getsockname()[0], LOOPBACK)
        self.assertNotEqual(port, 0)
        mixer = SimpleNamespace(get_pos=lambda: 12.5, get_duration=lambda: 300.0)
        host.initSong("Synthetic![]!Fixture song", "", mixer, "[]", duration=300,
                      track_id="owned-fixture")
        with requests.Session() as session:
            session.trust_env = False  # Never send a loopback request through a proxy.
            with session.post(f"http://{LOOPBACK}:{port}/action", json={"action": "status"},
                              timeout=(1, 1)) as response:
                response.raise_for_status()
                status = response.json()
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(status["title"], "Synthetic![]!Fixture song")
        self.assertEqual(status["track_id"], "owned-fixture")
        self.assertEqual(status["position"], 12.5)
        self.assertFalse(status["paused"])
        self.assertEqual(status["duration"], 300.0)
        thread = host._server_thread
        host.close()
        self.assertIsNone(host._server)
        self.assertFalse(thread.is_alive())
        self.assertEqual(listener.fileno(), -1)
        self.assertFalse(server.task_dispatcher.threads)
        self.assertTrue(all(not server_map for server_map in self.maps))

    def test_close_is_idempotent(self):
        host = self.make_host()
        host.close()
        host.close()
        self.assertIsNone(host._server)
        self.assertFalse(host._server_thread.is_alive())

    def test_failure_after_listener_start_cleans_its_threads_and_sockets(self):
        previous_threads = {thread.ident for thread in threading.enumerate()}
        with patch.object(self.module.ll, "print", side_effect=RuntimeError("mock startup failure")):
            with self.assertRaisesRegex(RuntimeError, "mock startup failure"):
                self.make_host()
        self.assertTrue(all(not server_map for server_map in self.maps))
        self.assertTrue(all(not dispatcher.threads for dispatcher in self.dispatchers))
        self.assertFalse([thread for thread in threading.enumerate()
                          if thread.ident not in previous_threads])

    def test_occupied_port_preserves_listener_and_leaves_no_worker_threads(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((LOOPBACK, 0))
            listener.listen(1)
            listener.settimeout(1)
            self.forced_port = listener.getsockname()[1]
            original_handle = listener.fileno()
            with self.assertRaises(OSError):
                self.make_host()
            self.assertEqual(listener.fileno(), original_handle)
            with socket.create_connection(listener.getsockname(), timeout=1):
                accepted, _ = listener.accept()
                accepted.close()
            self.module.Administrator.assert_not_called()
            self.assertTrue(all(not dispatcher.threads for dispatcher in self.dispatchers),
                            "A failed hosting attempt left Waitress workers running")


if __name__ == "__main__":
    unittest.main()
