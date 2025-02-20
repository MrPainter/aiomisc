import asyncio
import os
import socket
from contextlib import ExitStack
from tempfile import mktemp


import aiohttp.web
import pytest
import http.client

import aiomisc
from aiomisc.service import TCPServer, UDPServer, TLSServer
from aiomisc.service.aiohttp import AIOHTTPService


pytestmark = pytest.mark.catch_loop_exceptions


@pytest.fixture()
def unix_socket_udp():
    socket_path = mktemp(dir='/tmp', suffix='.sock')
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.setblocking(False)

    # Behaviour like in the bind_socket
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, 'SO_REUSEPORT'):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

    try:
        sock.bind(socket_path)
        yield sock
    except Exception:
        pass
    else:
        sock.close()
    finally:
        if os.path.exists(socket_path):
            os.remove(socket_path)


@pytest.fixture()
def unix_socket_tcp():
    socket_path = mktemp(dir='/tmp', suffix='.sock')
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.setblocking(False)

    try:
        sock.bind(socket_path)
        yield sock
    except Exception:
        pass
    else:
        sock.close()
    finally:
        if os.path.exists(socket_path):
            os.remove(socket_path)


def test_service_class():
    with pytest.raises(NotImplementedError):
        services = (
            aiomisc.Service(running=False, stopped=False),
            aiomisc.Service(running=False, stopped=False),
        )

        with aiomisc.entrypoint(*services):
            pass


def test_simple():
    class StartingService(aiomisc.Service):
        async def start(self):
            self.running = True

    class DummyService(StartingService):
        async def stop(self, err: Exception = None):
            self.stopped = True

    services = (
        DummyService(running=False, stopped=False),
        DummyService(running=False, stopped=False),
    )

    with aiomisc.entrypoint(*services):
        pass

    for svc in services:
        assert svc.running
        assert svc.stopped

    services = (
        DummyService(running=False, stopped=False),
        DummyService(running=False, stopped=False),
    )

    with pytest.raises(RuntimeError):
        with aiomisc.entrypoint(*services):
            raise RuntimeError

    for svc in services:
        assert svc.running
        assert svc.stopped

    services = (
        StartingService(running=False),
        StartingService(running=False),
    )

    with pytest.raises(RuntimeError):
        with aiomisc.entrypoint(*services):
            raise RuntimeError

    for svc in services:
        assert svc.running


def test_wrong_sublclass():
    with pytest.raises(TypeError):
        class _(aiomisc.Service):
            def start(self):
                return True

    class MyService(aiomisc.Service):
        async def start(self):
            return

    with pytest.raises(TypeError):
        class _(MyService):
            def stop(self):
                return True

    class _(MyService):
        async def stop(self):
            return True


def test_required_kwargs():
    class Svc(aiomisc.Service):
        __required__ = 'foo',

        async def start(self):
            pass

    with pytest.raises(AttributeError):
        Svc()

    assert Svc(foo='bar').foo == 'bar'


def test_tcp_server(aiomisc_unused_port):
    class TestService(TCPServer):
        DATA = []

        async def handle_client(self, reader: asyncio.StreamReader,
                                writer: asyncio.StreamWriter):
            self.DATA.append(await reader.readline())
            writer.close()

    service = TestService('127.0.0.1', aiomisc_unused_port)

    @aiomisc.threaded
    def writer():
        port = aiomisc_unused_port
        with socket.create_connection(('127.0.0.1', port)) as sock:
            sock.send(b'hello server\n')

    with aiomisc.entrypoint(service) as loop:
        loop.run_until_complete(writer())

    assert TestService.DATA
    assert TestService.DATA == [b'hello server\n']


def test_tls_server(certs, ssl_client_context, aiomisc_unused_port):
    class TestService(TLSServer):
        DATA = []

        async def handle_client(self, reader: asyncio.StreamReader,
                                writer: asyncio.StreamWriter):
            self.DATA.append(await reader.readline())
            writer.close()

    service = TestService(
        address='127.0.0.1', port=aiomisc_unused_port,
        ca=certs / 'ca.pem',
        key=certs / 'server.key',
        cert=certs / 'server.pem',
    )

    @aiomisc.threaded
    def writer():
        with ExitStack() as stack:
            sock = stack.enter_context(
                socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            )

            ssock = stack.enter_context(ssl_client_context.wrap_socket(
                sock, server_hostname='localhost'
            ))

            ssock.connect(('127.0.0.1', aiomisc_unused_port))
            ssock.send(b'hello server\n')

    with aiomisc.entrypoint(service) as loop:
        loop.run_until_complete(writer())

    assert TestService.DATA
    assert TestService.DATA == [b'hello server\n']


def test_udp_server(aiomisc_unused_port):
    class TestService(UDPServer):
        DATA = []

        async def handle_datagram(self, data: bytes, addr: tuple):
            self.DATA.append(data)

    service = TestService('127.0.0.1', aiomisc_unused_port)

    @aiomisc.threaded
    def writer():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        with sock:
            sock.sendto(b'hello server\n', ('127.0.0.1', aiomisc_unused_port))

    with aiomisc.entrypoint(service) as loop:
        loop.run_until_complete(writer())

    assert TestService.DATA
    assert TestService.DATA == [b'hello server\n']


def test_udp_without_port_or_socket():
    class TestService(UDPServer):
        pass

    with pytest.raises(RuntimeError):
        TestService()


def test_tcp_without_port_or_socket():
    class TestService(TCPServer):
        pass

    with pytest.raises(RuntimeError):
        TestService()


def test_udp_socket_server(unix_socket_udp):
    class TestService(UDPServer):
        DATA = []

        async def handle_datagram(self, data: bytes, addr: tuple):
            self.DATA.append(data)

    service = TestService(sock=unix_socket_udp)

    @aiomisc.threaded
    def writer():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

        with sock:
            sock.sendto(b'hello server\n', unix_socket_udp.getsockname())

    with aiomisc.entrypoint(service) as loop:
        loop.run_until_complete(writer())

    assert TestService.DATA
    assert TestService.DATA == [b'hello server\n']


def test_tcp_server_unix(unix_socket_tcp):
    class TestService(TCPServer):
        DATA = []

        async def handle_client(self, reader: asyncio.StreamReader,
                                writer: asyncio.StreamWriter):
            self.DATA.append(await reader.readline())
            writer.close()

    service = TestService(sock=unix_socket_tcp)

    @aiomisc.threaded
    def writer():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(unix_socket_tcp.getsockname())
            sock.send(b'hello server\n')

    with aiomisc.entrypoint(service) as loop:
        loop.run_until_complete(writer())

    assert TestService.DATA
    assert TestService.DATA == [b'hello server\n']


def test_aiohttp_service_create_app():
    with pytest.raises(TypeError):
        class _(AIOHTTPService):
            def create_application(self):
                return None

    class _(AIOHTTPService):
        async def create_application(self):
            return aiohttp.web.Application()


class AIOHTTPTestApp(AIOHTTPService):
    async def create_application(self):
        return aiohttp.web.Application()


def test_aiohttp_service_without_port_or_sock(aiomisc_unused_port):
    with pytest.raises(RuntimeError):
        AIOHTTPService()


def test_aiohttp_service(aiomisc_unused_port):
    @aiomisc.threaded
    def http_client():
        conn = http.client.HTTPConnection(
            host='127.0.0.1',
            port=aiomisc_unused_port,
            timeout=1
        )

        conn.request('GET', '/')
        response = conn.getresponse()
        return response.code

    service = AIOHTTPTestApp(address='127.0.0.1', port=aiomisc_unused_port)

    with aiomisc.entrypoint(service) as loop:
        response = loop.run_until_complete(http_client())

    assert response == 404


def test_aiohttp_service_sock(unix_socket_tcp):
    @aiomisc.threaded
    def http_client():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(unix_socket_tcp.getsockname())
            sock.send(b'GET / HTTP/1.1\nConnection: close\n\n')
            status = sock.recv(65535).splitlines()[0]
            return int(status.split()[1])

    service = AIOHTTPTestApp(sock=unix_socket_tcp)

    with aiomisc.entrypoint(service) as loop:
        response = loop.run_until_complete(http_client())

    assert response == 404


def test_service_events():
    class Initialization(aiomisc.Service):
        async def start(self):
            context = aiomisc.get_context()
            await asyncio.sleep(0.1)
            context['test'] = True

    class Awaiter(aiomisc.Service):
        result = None

        async def start(self):
            Awaiter.result = await self.context['test']

    services = (
        Awaiter(),
        Initialization(),
    )

    with aiomisc.entrypoint(*services):
        pass

    assert Awaiter.result


def test_service_events_2():
    class Initialization(aiomisc.Service):
        async def start(self):
            self.context['test'] = True

    class Awaiter(aiomisc.Service):
        result = None

        async def start(self):
            context = aiomisc.get_context()

            await asyncio.sleep(0.1)

            await context['test']
            Awaiter.result = await context['test']

    services = (
        Initialization(),
        Awaiter(),
    )

    with aiomisc.entrypoint(*services):
        pass

    assert Awaiter.result


def test_service_start_event():
    class Sleeper(aiomisc.Service):
        result = False

        async def start(self):
            self.start_event.set()

            await asyncio.sleep(86400)
            Sleeper.result = True

    with aiomisc.entrypoint(Sleeper()):
        pass

    assert not Sleeper.result


def test_service_no_start_event():
    class Sleeper(aiomisc.Service):
        result = False

        async def start(self):
            await asyncio.sleep(1)
            Sleeper.result = True

    with aiomisc.entrypoint(Sleeper()):
        pass

    assert Sleeper.result


def test_context_multiple_set():
    results = []

    async def test():
        context = aiomisc.get_context()

        context['foo'] = True
        await asyncio.sleep(0.1)
        results.append(await context['foo'])

        context['foo'] = False
        await asyncio.sleep(0.1)
        results.append(await context['foo'])

        context['foo'] = None
        await asyncio.sleep(0.1)
        results.append(await context['foo'])

    with aiomisc.entrypoint() as loop:
        loop.run_until_complete(test())

    assert results == [True, False, None]
