import asyncio
import json
import unittest
from types import SimpleNamespace
from bridge.dashboard import Dashboard, sdp_details


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_read_only_and_redacted(self):
        rule = {'listen_port': 8554, 'upstream': {'url': 'rtsp://secret-user:secret-password@camera/secret-path?token=secret-token'}, 'credentials': {'username': 'secret-user', 'password': 'secret-password'}}
        dashboard = Dashboard({'rules': [rule]})
        session = SimpleNamespace(id='safe-id', rule=rule, state='playing', started=dashboard.started,
                                  packets=[2, 1], bytes=[100, 20], tracks=[{'index': 0, 'packets': [2, 1], 'bytes': [100, 20]}],
                                  sdp=sdp_details(b's=secret-token\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:rtsp://camera/secret-path\r\na=fmtp:96 secret-password\r\n'))
        dashboard.sessions[session.id] = session
        server = await asyncio.start_server(dashboard.handle, '127.0.0.1', 0)
        async def request(method, path):
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            writer.write(f'{method} {path} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            return response
        try:
            response = await request('GET', '/api/status')
            for secret in (b'secret-user', b'secret-password', b'secret-path', b'secret-token'):
                self.assertNotIn(secret, response)
            data = json.loads(response.split(b'\r\n\r\n', 1)[1])
            self.assertEqual((data['connected'], data['playing']), (1, 1))
            self.assertEqual(data['sessions'][0]['bytes'], [100, 20])
            self.assertEqual(data['sessions'][0]['sdp'][0]['codecs'][0]['codec'], 'H264')
            self.assertIn(b'405 Method Not Allowed', await request('POST', '/api/status'))
            self.assertIn(b'404 Not Found', await request('GET', '/config'))
            self.assertIn(b'200 OK', await request('GET', '/'))
            dashboard.sessions.clear()
            self.assertEqual(dashboard.snapshot()['connected'], 0)
        finally:
            server.close()
            await server.wait_closed()

    def test_sdp_unknown_and_sensitive_fields(self):
        self.assertEqual(sdp_details(b'm=secret-token 0 RTP/AVP 96\r\na=rtpmap:96 secret-token/90000\r\na=crypto:secret'), [{'media': 'other', 'codecs': []}])
