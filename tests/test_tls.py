"""Local RTSPS handshake tests; OpenSSL generates disposable test certificates."""
import asyncio
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import unittest

from bridge.proxy import Session, encode, load_config, read_message


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required for local test certificates')
class TLSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.cert, self.key = root / 'cert.pem', root / 'key.pem'
        await asyncio.to_thread(subprocess.run, [
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(self.key), '-out', str(self.cert), '-days', '1',
            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
        ], check=True, capture_output=True)

    async def connect(self, upstream_settings, expected, hostname='127.0.0.1'):
        loop = asyncio.get_running_loop()
        original_handler = loop.get_exception_handler()
        def handle_error(loop, context):
            if context.get('message') == 'Error on transport creation for incoming connection' and isinstance(context.get('exception'), ConnectionResetError):
                return  # Expected server-side reset when the client rejects its certificate.
            if original_handler:
                original_handler(loop, context)
            else:
                loop.default_exception_handler(context)
        loop.set_exception_handler(handle_error)
        self.addCleanup(loop.set_exception_handler, original_handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        tasks = []

        async def upstream(reader, writer):
            try:
                _, headers, _ = await read_message(reader)
                writer.write(encode('RTSP/1.0 200 OK', {'cseq': headers['cseq']}))
                await writer.drain()
                await reader.read()
            finally:
                writer.close()

        camera = await asyncio.start_server(upstream, '127.0.0.1', 0, ssl=context)
        port = camera.sockets[0].getsockname()[1]
        rule = {'listen_port': 8554, 'upstream': {
            'url': f'rtsps://{hostname}:{port}/private-token', **upstream_settings}}

        def accept(reader, writer):
            tasks.append(asyncio.create_task(Session(reader, writer, rule, {}).run()))

        proxy = await asyncio.start_server(accept, '127.0.0.1', 0)
        reader, writer = await asyncio.open_connection('127.0.0.1', proxy.sockets[0].getsockname()[1])
        try:
            writer.write(encode('OPTIONS rtsp://127.0.0.1:8554/ RTSP/1.0', {'cseq': '1'}))
            await writer.drain()
            with self.assertLogs('bridge', level='INFO') as logs:
                result = await asyncio.wait_for(read_message(reader), 5)
            self.assertIn(expected, result[0])
            self.assertNotIn('private-token', '\n'.join(logs.output))
            return logs.output
        finally:
            writer.close()
            await writer.wait_closed()
            proxy.close()
            camera.close()
            await proxy.wait_closed()
            await camera.wait_closed()
            await asyncio.gather(*tasks)

    async def test_default_rejects_self_signed(self):
        logs = await self.connect({}, '502')
        self.assertTrue(any('SSLCertVerificationError' in line for line in logs))

    async def test_opt_out_accepts_self_signed_and_warns(self):
        logs = await self.connect({'tls_verify': False, 'tls_ca_file': '/does/not/exist'}, '200')
        self.assertTrue(any('WARNING' in line and 'tls_verification_disabled' in line for line in logs))

    async def test_explicit_verify_rejects_self_signed(self):
        await self.connect({'tls_verify': True}, '502')

    async def test_trusted_certificate_connects(self):
        await self.connect({'tls_ca_file': str(self.cert)}, '200', hostname='localhost')

    async def test_trusted_certificate_still_checks_hostname(self):
        await self.connect({'tls_ca_file': str(self.cert)}, '502')


class TLSConfigTests(unittest.TestCase):
    def test_reject_string_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yaml'
            config.write_text('rules:\n  - listen_port: 8554\n    upstream:\n      url: rtsps://localhost/stream\n      tls_verify: "false"\n')
            with self.assertRaisesRegex(ValueError, 'YAML boolean'):
                load_config(config)
