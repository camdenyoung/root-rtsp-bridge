import asyncio
import json
import unittest
from types import SimpleNamespace
from bridge.dashboard import Dashboard, sdp_details, format_duration


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

    def test_duration_units(self):
        for seconds, expected in ((0, '0 seconds'), (59, '59 seconds'),
                                  (61, '1 minute 1 second'), (3660, '1 hour 1 minute'),
                                  (90000, '1 day 1 hour'), (691200, '1 week 1 day'),
                                  (32140800, '1 year 1 week')):
            self.assertEqual(format_duration(seconds), expected)

    def test_sdp_resolution(self):
        details = sdp_details(b'm=video 0 RTP/AVP 96\r\na=framesize:96 1920-1080\r\na=control:secret-token\r\n')
        self.assertEqual(details[0]['resolution'], {'width': 1920, 'height': 1080})
        self.assertNotIn('resolution', sdp_details(b'm=video 0 RTP/AVP 96\r\na=framesize:96 0-1080')[0])
        self.assertNotIn('resolution', sdp_details(b'm=audio 0 RTP/AVP 96\r\na=framesize:96 1920-1080')[0])

    def test_route_connection_counts(self):
        rules = [{'listen_port': port, 'upstream': {}} for port in (8554, 8555, 8556)]
        dashboard = Dashboard({'rules': rules})
        for index, rule in enumerate((rules[0], rules[0], rules[1])):
            dashboard.sessions[index] = SimpleNamespace(
                id=str(index), rule=rule, state='connected', started=dashboard.started,
                packets=[0, 0], bytes=[0, 0], tracks=[], sdp=None)
        def counts():
            return [r['connected'] for r in dashboard.snapshot()['routes']]
        self.assertEqual(counts(), [2, 1, 0])
        del dashboard.sessions[0]
        self.assertEqual(counts(), [1, 1, 0])
        dashboard.sessions.clear()
        self.assertEqual(counts(), [0, 0, 0])
