import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from bridge.proxy import Session, read_message, encode, authentication, load_config


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, down_tcp, up_tcp, mode='drop', challenge=None, audio_enabled=True):
        upstream_requests = []
        upstream_connections = []
        sessions = []
        live_sessions = []
        datagrams = []
        payload = b'\x80\x60\x00\x01opaque-h264-payload'
        rtcp = b'\x80\xc9\x00\x01feedback'
        upstream_got_rtcp = asyncio.Event()
        loop = asyncio.get_running_loop()
        tcp_upwriter = None
        up_udp = None
        up_client_port = None

        class Receiver(asyncio.DatagramProtocol):
            def __init__(self, callback): self.callback = callback
            def datagram_received(self, data, addr): self.callback(data, addr)

        async def udp_pair(callback):
            import socket
            for port in range(26000, 28000, 2):
                socks = []
                try:
                    for p in (port, port+1):
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        socks.append(s)
                        s.bind(('127.0.0.1', p))
                        s.setblocking(False)
                except OSError:
                    for s in socks: s.close()
                    continue
                transports = []
                for s in socks:
                    t, _ = await loop.create_datagram_endpoint(lambda: Receiver(callback), sock=s)
                    transports.append(t)
                    datagrams.append(t)
                return port, transports
            raise AssertionError('No UDP ports')

        async def upstream(reader, writer):
            nonlocal tcp_upwriter, up_udp, up_client_port
            tcp_upwriter = writer
            upstream_connections.append(writer)
            try:
                while True:
                    msg = await read_message(reader)
                    if isinstance(msg[0], int):
                        if msg[0] == 1 and msg[1] == rtcp: upstream_got_rtcp.set()
                        continue
                    start, headers, body = msg
                    method, uri, _ = start.split()
                    upstream_requests.append((method, uri, headers))
                    response = {'cseq': headers['cseq']}
                    status = 'RTSP/1.0 200 OK'
                    data = b''
                    if challenge and 'authorization' not in headers:
                        status = 'RTSP/1.0 401 Unauthorized'
                        response['www-authenticate'] = challenge
                    elif method == 'DESCRIBE':
                        response['content-base'] = f'rtsp://127.0.0.1:{up_port}/secret/'
                        response['content-type'] = 'application/sdp'
                        data = b'v=0\r\nc=IN IP4 127.0.0.1\r\na=control:*\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=0\r\n'
                        if not audio_enabled:
                            data = data.replace(b'm=video', b'm=audio 0 RTP/AVP 97\r\na=rtpmap:97 MPEG4-GENERIC/16000/1\r\na=control:audio-aac\r\nm=audio 0 RTP/AVP 98\r\na=rtpmap:98 opus/48000/2\r\na=control:audio-opus\r\nm=video')
                    elif method == 'SETUP':
                        response['session'] = 'up-session;timeout=60'
                        if up_tcp:
                            response['transport'] = headers['transport']
                        else:
                            import re
                            up_client_port = int(re.search('client_port=(\\d+)', headers['transport'])[1])
                            p, up_udp = await udp_pair(lambda d,a: upstream_got_rtcp.set() if d == rtcp else None)
                            response['transport'] = headers['transport'] + f';server_port={p}-{p+1}'
                    writer.write(encode(status, response, data))
                    await writer.drain()
                    if method == 'PLAY':
                        if up_tcp:
                            writer.write(b'$\x00' + len(payload).to_bytes(2, 'big') + payload)
                            await writer.drain()
                        else:
                            up_udp[0].sendto(payload, ('127.0.0.1', up_client_port))
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        upstream_server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        up_port = upstream_server.sockets[0].getsockname()[1]
        rule = {'audio_enabled': audio_enabled, 'listen_port': 8554, 'downstream_transport': 'tcp' if down_tcp else 'udp',
                'upstream': {'url': f'rtsp://127.0.0.1:{up_port}/secret?token=private', 'transport': 'tcp' if up_tcp else 'udp'},
                'credentials': {'mode': mode, 'username': 'replacement', 'password': 'password'}}
        def accept(r,w):
            session = Session(r,w,rule,{'listen_host':'127.0.0.1','udp_port_range':[22000,23999]})
            live_sessions.append(session)
            task = asyncio.create_task(session.run())
            sessions.append(task)
        proxy = await asyncio.start_server(accept, '127.0.0.1', 0)
        port = proxy.sockets[0].getsockname()[1]
        root = f'rtsp://127.0.0.1:{port}/'
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        received_udp = loop.create_future()
        async def request(method, uri=root, extra=None, auth=False):
            headers = {'cseq':'7'}
            if extra: headers.update(extra)
            if auth: headers['authorization'] = 'Basic ' + base64.b64encode(b'keypad:secret').decode()
            writer.write(encode(f'{method} {uri} RTSP/1.0',headers))
            await writer.drain()
            return await asyncio.wait_for(read_message(reader), 3)
        try:
            if mode == 'passthrough':
                response = await request('DESCRIBE')
                self.assertIn('401', response[0])
            response = await request('DESCRIBE', auth=mode=='passthrough')
            self.assertIn('200', response[0])
            self.assertNotIn(b'secret', response[2])
            if not audio_enabled:
                self.assertNotIn(b'm=audio', response[2])
                self.assertNotIn(b'opus', response[2])
                self.assertNotIn(b'MPEG4-GENERIC', response[2])
                self.assertIn(b'm=video', response[2])
            self.assertIn((root+'track/0').encode(), response[2])
            if down_tcp:
                transport = 'RTP/AVP/TCP;unicast;interleaved=4-5'
            else:
                down_port, down_udp = await udp_pair(lambda d,a: received_udp.set_result(d) if not received_udp.done() else None)
                transport = f'RTP/AVP;unicast;client_port={down_port}-{down_port+1}'
            response = await request('SETUP', root+'track/0', {'transport':transport})
            self.assertIn('200', response[0])
            if not down_tcp:
                import re
                proxy_rtcp_port = int(re.search(r'server_port=(\d+)-(\d+)', response[1]['transport'])[2])
            self.assertEqual(upstream_requests[-1][1], f'rtsp://127.0.0.1:{up_port}/secret/trackID=0')
            response = await request('PLAY', extra={'session':'up-session'})
            self.assertIn('200', response[0])
            self.assertEqual(live_sessions[0].state, 'playing')
            self.assertEqual(live_sessions[0].sdp[0]['media'], 'video')
            if down_tcp:
                self.assertEqual(await asyncio.wait_for(read_message(reader),3), (4,payload))
                writer.write(b'$\x05'+len(rtcp).to_bytes(2,'big')+rtcp)
                await writer.drain()
            else:
                self.assertEqual(await asyncio.wait_for(received_udp,3),payload)
                down_udp[1].sendto(rtcp, ('127.0.0.1', proxy_rtcp_port))
            await asyncio.wait_for(upstream_got_rtcp.wait(),3)
            response = await request('PAUSE')
            self.assertIn('200', response[0])
            self.assertEqual(live_sessions[0].state, 'paused')
            response = await request('TEARDOWN')
            self.assertIn('200',response[0])
            if challenge:
                auth_headers = [h['authorization'] for _,_,h in upstream_requests if 'authorization' in h]
                self.assertTrue(auth_headers)
                if challenge.startswith('Basic'):
                    expected = b'keypad:secret' if mode == 'passthrough' else b'replacement:password'
                    self.assertEqual(base64.b64decode(auth_headers[0][6:]), expected)
        finally:
            writer.close()
            await writer.wait_closed()
            proxy.close()
            upstream_server.close()
            await proxy.wait_closed()
            await upstream_server.wait_closed()
            for t in datagrams: t.close()
            for w in upstream_connections: w.close()
            await asyncio.gather(*sessions)

    async def test_transport_matrix(self):
        for down in (False, True):
            for up in (False, True):
                with self.subTest(down_tcp=down, up_tcp=up):
                    await self.exercise(down,up)

    async def test_video_only_transport_matrix(self):
        for down in (False, True):
            for up in (False, True):
                with self.subTest(down_tcp=down, up_tcp=up):
                    await self.exercise(down, up, audio_enabled=False)

    async def test_replace_basic(self):
        await self.exercise(True,True,'replace','Basic realm="camera"')

    async def test_passthrough_basic(self):
        await self.exercise(True,True,'passthrough','Basic realm="camera"')

    async def test_replace_digest(self):
        await self.exercise(True,True,'replace','Digest realm="camera", nonce="123", qop="auth", algorithm=MD5')


class ConfigTests(unittest.TestCase):
    def test_audio_default_preserves_tracks(self):
        from unittest.mock import Mock
        writer = Mock()
        writer.get_extra_info.return_value = ('127.0.0.1', 1234)
        session = Session(None, writer, {'listen_port': 8554, 'upstream': {'url': 'rtsp://camera/stream/'}}, {})
        session.base = 'rtsp://camera/stream/'
        sdp = b'v=0\r\nm=audio 0 RTP/AVP 97\r\na=control:audio\r\nm=video 0 RTP/AVP 96\r\na=control:video\r\n'
        result = session.rewrite_sdp(sdp, 'rtsp://bridge:8554/')
        self.assertIn(b'm=audio', result)
        self.assertEqual(set(session.maps.values()), {'rtsp://camera/stream/audio', 'rtsp://camera/stream/video'})

    def test_audio_flag_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_text('rules:\n  - listen_port: 8554\n    audio_enabled: "false"\n    upstream:\n      url: rtsp://localhost/path\n')
            with self.assertRaisesRegex(ValueError, 'audio_enabled'):
                load_config(path)

    def test_example(self):
        self.assertEqual(load_config('config/config.example.yaml')['rules'][0]['listen_port'],8554)

    def test_digest_known_vector(self):
        # RFC 2069 legacy Digest example.
        result = authentication('Digest realm="testrealm@host.com", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093"',
                                'GET','/dir/index.html','Mufasa','Circle Of Life')
        self.assertIn('670fd8c2df070c60b045671b8b24ff02',result)

    def test_reject_credentials_in_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.yaml'
            path.write_text('rules:\n  - listen_port: 8554\n    upstream:\n      url: rtsp://user:pass@localhost/path\n')
            with self.assertRaises(ValueError): load_config(path)
