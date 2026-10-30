"""RTSP control proxy and opaque RTP/RTCP transport adapter."""
import asyncio
import base64
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import signal
import time
from .dashboard import Dashboard, sdp_details
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit, urljoin
import yaml

LOG = logging.getLogger('bridge')
LIMIT = 1024 * 1024


async def read_message(reader):
    first = await reader.readexactly(1)
    if first == b'$':
        header = await reader.readexactly(3)
        return header[0], await reader.readexactly(int.from_bytes(header[1:], 'big'))
    raw = first + await reader.readuntil(b'\r\n\r\n')
    lines = raw.decode('latin1').split('\r\n')
    headers = {}
    for line in lines[1:]:
        if line:
            key, value = line.split(':', 1)
            headers[key.lower()] = value.strip()
    size = int(headers.get('content-length', '0'))
    if not 0 <= size <= LIMIT:
        raise ValueError('RTSP body too large')
    return lines[0], headers, await reader.readexactly(size)


def encode(start, headers, body=b''):
    headers = dict(headers)
    headers['content-length'] = str(len(body))
    return (start + '\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in headers.items()) + '\r\n').encode('latin1') + body


def authentication(challenge, method, uri, username, password):
    scheme, _, value = challenge.partition(' ')
    if scheme.lower() == 'basic':
        return 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()
    if scheme.lower() != 'digest':
        raise ValueError('Unsupported upstream authentication')
    fields = dict((k.lower(), a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', value))
    algorithm = fields.get('algorithm', 'MD5').upper()
    if algorithm not in ('MD5', 'SHA-256'):
        raise ValueError('Unsupported Digest algorithm')
    digest = lambda s: hashlib.new('md5' if algorithm == 'MD5' else 'sha256', s.encode()).hexdigest()
    realm, nonce = fields['realm'], fields['nonce']
    ha1, ha2 = digest(f'{username}:{realm}:{password}'), digest(f'{method}:{uri}')
    qop = fields.get('qop')
    cnonce = secrets.token_hex(16)
    if qop and 'auth' not in qop.split(','):
        raise ValueError('Unsupported Digest qop')
    response = digest(f'{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}') if qop else digest(f'{ha1}:{nonce}:{ha2}')
    result = f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}", algorithm={algorithm}'
    if qop:
        result += f', qop=auth, nc=00000001, cnonce="{cnonce}"'
    if 'opaque' in fields:
        result += f', opaque="{fields["opaque"]}"'
    return result


def load_config(path):
    with open(path) as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict) or not config.get('rules'):
        raise ValueError('At least one routing rule is required')
    seen = set()
    for rule in config['rules']:
        if type(rule.get('audio_enabled', True)) is not bool:
            raise ValueError('audio_enabled must be a YAML boolean')
        port = rule['listen_port']
        if type(port) is not int or not 1 <= port <= 65535 or port in seen:
            raise ValueError('Listening ports must be valid and unique')
        seen.add(port)
        if type(rule['upstream'].get('tls_verify', True)) is not bool:
            raise ValueError('upstream.tls_verify must be a YAML boolean')
        target = urlsplit(rule['upstream']['url'])
        if target.scheme not in ('rtsp', 'rtsps') or not target.hostname or target.username or target.password or target.fragment:
            raise ValueError('Upstream URL must be rtsp/rtsps without embedded credentials or fragments')
        if rule.get('credentials', {}).get('mode', 'drop') not in ('drop', 'replace', 'passthrough'):
            raise ValueError('Invalid credential mode')
        if rule.get('credentials', {}).get('mode') == 'replace' and not all(key in rule['credentials'] for key in ('username', 'password')):
            raise ValueError('Replace mode requires username and password')
        for transport in (rule.get('downstream_transport', 'udp'), rule['upstream'].get('transport', 'tcp')):
            if transport not in ('tcp', 'udp'):
                raise ValueError('Transport must be tcp or udp')
    settings = config.get('logging', {})
    if settings.get('level', 'INFO').upper() not in ('INFO', 'DEBUG', 'TRACE', 'WARNING', 'ERROR'):
        raise ValueError('Invalid logging level')
    for value in (config.get('request_timeout', 15), settings.get('stats_interval', 30), settings.get('max_bytes', 10485760), settings.get('backups', 5)):
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError('Timeouts, log sizes, and retention must be positive')
    low, high = config.get('udp_port_range', [20000, 20199])
    if not 1024 <= low < high <= 65535:
        raise ValueError('Invalid UDP port range')
    dashboard = config.get('dashboard', {})
    if not isinstance(dashboard, dict) or type(dashboard.get('enabled', False)) is not bool:
        raise ValueError('dashboard.enabled must be a YAML boolean')
    if not isinstance(dashboard.get('host', '127.0.0.1'), str):
        raise ValueError('dashboard.host must be a string')
    port = dashboard.get('port', 8080)
    if type(port) is not int or not 1 <= port <= 65535 or (dashboard.get('enabled', False) and port in seen):
        raise ValueError('Dashboard port must be valid and separate from RTSP ports')
    return config


class Datagram(asyncio.DatagramProtocol):
    def __init__(self, callback):
        self.callback = callback

    def datagram_received(self, data, addr):
        self.callback(data, addr)


class Session:
    def __init__(self, reader, writer, rule, config):
        self.reader, self.writer, self.rule, self.config = reader, writer, rule, config
        self.started = time.monotonic()
        self.state = 'connected'
        self.sdp = None
        self.id = secrets.token_hex(6)
        self.upreader = self.upwriter = None
        self.tasks, self.sockets = [], []
        self.responses = asyncio.Queue()
        self.upchannels, self.downchannels = {}, {}
        self.maps = {}
        self.sequence = 0
        self.session = None
        self.creds = None
        self.challenge = None
        self.stage = 'downstream_read'
        self.control_pending = False
        self.pending_media = []
        self.tracks = []
        self.packets = [0, 0]
        self.bytes = [0, 0]
        self.peer = writer.get_extra_info('peername')[0]
        self.url = rule['upstream']['url']
        self.base = self.url

    def log(self, level, event, **fields):
        LOG.log(level, 'session=%s rule_port=%s event=%s %s', self.id, self.rule['listen_port'], event,
                ' '.join(f'{k}={v}' for k, v in fields.items()))

    async def send(self, writer, data):
        writer.write(data)
        await writer.drain()

    def media(self, destination, channel, data):
        if destination == 0 and self.control_pending:
            if len(self.pending_media) < 256:
                self.pending_media.append((channel, data))
            return
        writer = self.writer if destination == 0 else self.upwriter
        if writer and not writer.is_closing():
            if writer.transport.get_write_buffer_size() > 4 * LIMIT:
                self.log(logging.WARNING, 'media_dropped_backpressure', side=destination)
                return
            writer.write(b'$' + bytes([channel]) + len(data).to_bytes(2, 'big') + data)

    def counted(self, direction, data, track):
        track["packets"][direction] += 1
        track["bytes"][direction] += len(data)
        if self.packets[direction] == 0:
            self.log(logging.INFO, 'first_media_packet', direction='upstream_to_bosch' if direction == 0 else 'bosch_to_upstream')
        self.packets[direction] += 1
        self.bytes[direction] += len(data)

    async def upstream_reader(self):
        try:
            while True:
                message = await read_message(self.upreader)
                if isinstance(message[0], int):
                    callback = self.upchannels.get(message[0])
                    if callback:
                        callback(message[1])
                elif message[0].startswith('RTSP/'):
                    await self.responses.put(message)
                else:
                    # Server-initiated requests are uncommon; explicitly reject them.
                    await self.send(self.upwriter, encode('RTSP/1.0 501 Not Implemented', {'cseq': message[1].get('cseq', '0')}))
        except (Exception, asyncio.CancelledError) as exc:
            await self.responses.put(exc)
            if not self.control_pending:
                self.writer.close()

    async def request(self, method, uri, headers, body):
        for attempt in range(2):
            self.sequence += 1
            headers = dict(headers)
            headers['cseq'] = str(self.sequence)
            headers.pop('authorization', None)
            if self.session:
                headers['session'] = self.session
            if self.creds and self.challenge:
                headers['authorization'] = authentication(self.challenge, method, uri, *self.creds)
            self.stage = 'upstream_' + method.lower()
            self.log(logging.DEBUG, 'upstream_request', method=method, cseq=self.sequence)
            await self.send(self.upwriter, encode(f'{method} {uri} RTSP/1.0', headers, body))
            result = await asyncio.wait_for(self.responses.get(), self.config.get('request_timeout', 15))
            if isinstance(result, BaseException):
                raise ConnectionError('Upstream closed')
            status, response_headers, _ = result
            self.log(logging.DEBUG, 'upstream_response', status=status.split()[1])
            if status.split()[1] == '401' and self.creds and attempt == 0:
                self.challenge = response_headers.get('www-authenticate', '')
                self.log(logging.INFO, 'upstream_authentication_challenge')
                continue
            if 'session' in response_headers:
                self.session = response_headers['session'].split(';')[0]
            return result
        raise ConnectionError('Authentication retry exhausted')

    async def pair(self, callbacks):
        low, high = self.config.get('udp_port_range', [20000, 20199])
        loop = asyncio.get_running_loop()
        for port in range(low + low % 2, high, 2):
            sockets = []
            try:
                for number in (port, port + 1):
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sockets.append(sock)
                    sock.bind((self.config.get('listen_host', '0.0.0.0'), number))
                    sock.setblocking(False)
            except OSError:
                for sock in sockets:
                    sock.close()
                continue
            transports = []
            for sock, callback in zip(sockets, callbacks):
                transport, _ = await loop.create_datagram_endpoint(lambda cb=callback: Datagram(cb), sock=sock)
                transports.append(transport)
                self.sockets.append(transport)
            return port, transports
        raise OSError('UDP port range exhausted')

    async def setup(self, uri, headers, body):
        transport = headers.get('transport', '')
        downstream_tcp = self.rule.get('downstream_transport', 'udp') == 'tcp'
        if ('RTP/AVP/TCP' in transport.upper()) != downstream_tcp or 'multicast' in transport.lower():
            return 'RTSP/1.0 461 Unsupported Transport', {}, b''
        field = 'interleaved' if downstream_tcp else 'client_port'
        match = re.search(field + r'=(\d+)-(\d+)', transport)
        if not match:
            return 'RTSP/1.0 461 Unsupported Transport', {}, b''
        down_numbers = list(map(int, match.groups()))
        if downstream_tcp and (max(down_numbers) > 255 or any(n in self.downchannels for n in down_numbers)):
            return 'RTSP/1.0 461 Unsupported Transport', {}, b''
        if not downstream_tcp and not all(1 <= n <= 65535 for n in down_numbers):
            return 'RTSP/1.0 461 Unsupported Transport', {}, b''
        upstream_tcp = self.rule['upstream'].get('transport', 'tcp') == 'tcp'
        up_numbers = [len(self.upchannels), len(self.upchannels) + 1]
        if upstream_tcp and up_numbers[1] > 255:
            raise ValueError('Too many tracks')
        track = {"index": len(self.tracks), "packets": [0, 0], "bytes": [0, 0]}
        self.tracks.append(track)
        up_destination = [None, None]
        up_sockets, down_sockets = None, None

        def to_down(index, data):
            self.counted(0, data, track)
            if downstream_tcp:
                self.media(0, down_numbers[index], data)
            else:
                down_sockets[index].sendto(data, (self.peer, down_numbers[index]))

        def to_up(index, data):
            self.counted(1, data, track)
            if upstream_tcp:
                self.media(1, up_numbers[index], data)
            elif up_destination[index]:
                up_sockets[index].sendto(data, up_destination[index])

        if not downstream_tcp:
            down_port, down_sockets = await self.pair([
                lambda data, addr, i=i: to_up(i, data) if addr == (self.peer, down_numbers[i]) else None for i in range(2)])
        if upstream_tcp:
            for i, channel in enumerate(up_numbers):
                self.upchannels[channel] = lambda data, i=i: to_down(i, data)
            headers['transport'] = f'RTP/AVP/TCP;unicast;interleaved={up_numbers[0]}-{up_numbers[1]}'
        else:
            up_port, up_sockets = await self.pair([
                lambda data, addr, i=i: to_down(i, data) if addr == up_destination[i] else None for i in range(2)])
            headers['transport'] = f'RTP/AVP;unicast;client_port={up_port}-{up_port+1}'
        result = await self.request('SETUP', uri, headers, body)
        status, response_headers, response_body = result
        if status.split()[1] != '200':
            return result
        received = response_headers.get('transport', '')
        negotiated_tcp = 'RTP/AVP/TCP' in received.upper()
        if negotiated_tcp != upstream_tcp:
            raise ValueError('Upstream selected unexpected transport')
        if upstream_tcp:
            negotiated = re.search(r'interleaved=(\d+)-(\d+)', received)
            if not negotiated or list(map(int, negotiated.groups())) != up_numbers:
                raise ValueError('Upstream selected unexpected channels')
        else:
            ports = re.search(r'server_port=(\d+)-(\d+)', received)
            if not ports:
                raise ValueError('Upstream omitted server UDP ports')
            host = self.upwriter.get_extra_info('peername')[0]
            for i, port in enumerate(map(int, ports.groups())):
                up_destination[i] = (host, port)
        for i, channel in enumerate(down_numbers):
            if downstream_tcp:
                self.downchannels[channel] = lambda data, i=i: to_up(i, data)
        response_headers['transport'] = (f'RTP/AVP/TCP;unicast;interleaved={down_numbers[0]}-{down_numbers[1]}' if downstream_tcp else
            f'RTP/AVP;unicast;client_port={down_numbers[0]}-{down_numbers[1]};server_port={down_port}-{down_port+1}')
        self.log(logging.DEBUG, 'media_endpoints', upstream_channels=up_numbers if upstream_tcp else 'udp', upstream_udp=up_destination if not upstream_tcp else 'none', downstream_server_ports='interleaved' if downstream_tcp else [down_port, down_port + 1])
        self.log(logging.INFO, 'track_setup', downstream='tcp' if downstream_tcp else 'udp', upstream='tcp' if upstream_tcp else 'udp', downstream_ports=down_numbers)
        return status, response_headers, response_body

    def rewrite_sdp(self, body, root):
        lines = body.decode().splitlines()
        if not self.rule.get('audio_enabled', True):
            filtered = []
            dropping = False
            audio_tracks = 0
            for line in lines:
                if line.startswith('m='):
                    dropping = line.split()[0].lower() == 'm=audio'
                    audio_tracks += int(dropping)
                if not dropping:
                    filtered.append(line)
            lines = filtered
            self.log(logging.INFO, 'audio_filtered', removed_tracks=audio_tracks)
        self.sdp = sdp_details(('\r\n'.join(lines)).encode())
        for index, line in enumerate(lines):
            if line.startswith('a=control:'):
                control = line[len('a=control:'):]
                if control == '*':
                    continue
                # urllib does not classify rtsp as a relative-URL scheme.
                absolute = control if urlsplit(control).scheme else urljoin(self.base.replace('rtsps:', 'https:', 1).replace('rtsp:', 'http:', 1), control).replace('https:', 'rtsps:', 1).replace('http:', 'rtsp:', 1)
                local = root.rstrip('/') + f'/track/{len(self.maps)}'
                self.maps[local] = absolute
                lines[index] = 'a=control:' + local
            elif line.startswith('c=IN IP4 '):
                lines[index] = 'c=IN IP4 0.0.0.0'
        return ('\r\n'.join(lines) + '\r\n').encode()

    async def statistics(self):
        previous = 0
        while True:
            await asyncio.sleep(self.config.get('logging', {}).get('stats_interval', 30))
            self.log(logging.INFO, 'media_statistics', packets=self.packets, bytes=self.bytes, tracks=self.tracks)
            if self.session and self.packets[0] == previous:
                self.log(logging.WARNING, 'upstream_media_inactive')
            previous = self.packets[0]

    async def run(self):
        self.log(logging.INFO, 'accepted', peer=self.peer)
        self.tasks.append(asyncio.create_task(self.statistics()))
        try:
            while True:
                message = await read_message(self.reader)
                if isinstance(message[0], int):
                    callback = self.downchannels.get(message[0])
                    if callback:
                        callback(message[1])
                    continue
                start, headers, body = message
                method, local_uri, version = start.split()
                if method not in {'OPTIONS', 'DESCRIBE', 'SETUP', 'PLAY', 'PAUSE', 'TEARDOWN', 'GET_PARAMETER', 'SET_PARAMETER'}:
                    await self.send(self.writer, encode('RTSP/1.0 405 Method Not Allowed', {'cseq': headers.get('cseq', '0')}))
                    continue
                cseq = headers.get('cseq', '0')
                self.log(logging.DEBUG, 'downstream_request', method=method, cseq=cseq)
                if version != 'RTSP/1.0':
                    raise ValueError('Unsupported RTSP version')
                credentials = self.rule.get('credentials', {})
                mode = credentials.get('mode', 'drop')
                if mode == 'passthrough' and not self.creds:
                    auth = headers.get('authorization', '')
                    try:
                        if not auth.lower().startswith('basic '):
                            raise ValueError()
                        self.creds = tuple(base64.b64decode(auth[6:], validate=True).decode().split(':', 1))
                        if len(self.creds) != 2:
                            raise ValueError()
                    except (ValueError, UnicodeError):
                        await self.send(self.writer, encode('RTSP/1.0 401 Unauthorized', {'cseq': cseq, 'www-authenticate': 'Basic realm="RTSP bridge"'}))
                        continue
                elif mode == 'replace':
                    self.creds = (str(credentials.get('username', '')), str(credentials.get('password', '')))
                if not self.upwriter:
                    self.stage = 'upstream_connect'
                    target = urlsplit(self.url)
                    self.log(logging.INFO, 'upstream_connect', host=target.hostname, port=target.port or (322 if target.scheme == 'rtsps' else 554), tls=target.scheme == 'rtsps')
                    context = None
                    if target.scheme == 'rtsps':
                        if self.rule['upstream'].get('tls_verify', True):
                            context = ssl.create_default_context(cafile=self.rule['upstream'].get('tls_ca_file'))
                        else:
                            self.log(logging.WARNING, 'tls_verification_disabled', host=target.hostname)
                            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                            context.check_hostname = False
                            context.verify_mode = ssl.CERT_NONE
                    self.upreader, self.upwriter = await asyncio.wait_for(asyncio.open_connection(target.hostname, target.port or (322 if context else 554), ssl=context, limit=LIMIT), self.config.get('request_timeout', 15))
                    self.tasks.append(asyncio.create_task(self.upstream_reader()))
                root_parts = urlsplit(local_uri)
                root = urlunsplit((root_parts.scheme, root_parts.netloc, '/', '', ''))
                uri = '*' if local_uri == '*' else self.maps.get(local_uri, self.url)
                forwarded = {k: v for k, v in headers.items() if k not in ('authorization', 'host', 'content-length')}
                self.control_pending = True
                if method == 'SETUP':
                    result = await self.setup(uri, forwarded, body)
                else:
                    result = await self.request(method, uri, forwarded, body)
                status, reply_headers, reply_body = result
                if status.split()[1] == '200' and method in ('PLAY', 'PAUSE'):
                    self.state = 'playing' if method == 'PLAY' else 'paused'
                reply_headers = dict(reply_headers)
                reply_headers['cseq'] = cseq
                if method == 'DESCRIBE' and status.split()[1] == '200':
                    self.base = reply_headers.get('content-base', reply_headers.get('content-location', self.url))
                    self.maps.clear()
                    reply_body = self.rewrite_sdp(reply_body, root)
                    reply_headers['content-base'] = root
                if 'rtp-info' in reply_headers:
                    reverse = {v: k for k, v in self.maps.items()}
                    reply_headers['rtp-info'] = re.sub(r'url=([^,;]+)', lambda match: 'url=' + reverse.get(match[1], root), reply_headers['rtp-info'])
                reply_headers.pop('content-location', None)
                reply_headers.pop('location', None)
                # Do not expose upstream authentication challenges to Bosch in replace/drop modes.
                if mode != 'passthrough':
                    reply_headers.pop('www-authenticate', None)
                self.log(logging.INFO, 'rtsp_response', method=method, status=status.split()[1])
                await self.send(self.writer, encode(status, reply_headers, reply_body))
                self.control_pending = False
                for channel, packet in self.pending_media:
                    self.media(0, channel, packet)
                self.pending_media.clear()
                if method == 'TEARDOWN' or self.upreader.at_eof():
                    break
        except asyncio.IncompleteReadError:
            self.log(logging.INFO, 'downstream_disconnected')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Exception text can contain upstream URLs, tokens, and passwords.
            self.log(logging.ERROR, 'session_failed', stage=self.stage, error_type=type(exc).__name__, errno=getattr(exc, 'errno', 'none'))
            if not self.writer.is_closing():
                try:
                    await self.send(self.writer, encode('RTSP/1.0 502 Bad Gateway', {'cseq': locals().get('cseq', '0')}))
                except ConnectionError:
                    pass
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            for transport in self.sockets:
                transport.close()
            for writer in (self.writer, self.upwriter):
                if writer:
                    writer.close()
            self.log(logging.INFO, 'closed', packets=self.packets, bytes=self.bytes)


async def serve(config):
    servers, sessions = [], set()
    dashboard = Dashboard(config)
    def accept(reader, writer, rule):
        session = Session(reader, writer, rule, config)
        dashboard.sessions[session.id] = session
        task = asyncio.create_task(session.run())
        task.add_done_callback(lambda _: dashboard.sessions.pop(session.id, None))
        sessions.add(task)
        task.add_done_callback(sessions.discard)
    try:
        for rule in config['rules']:
            if urlsplit(rule['upstream']['url']).scheme == 'rtsps' and not rule['upstream'].get('tls_verify', True):
                LOG.warning('event=tls_verification_disabled rule_port=%s', rule['listen_port'])
            server = await asyncio.start_server(lambda r, w, rule=rule: accept(r, w, rule), config.get('listen_host', '0.0.0.0'), rule['listen_port'], limit=LIMIT)
            servers.append(server)
            LOG.info('event=listening port=%s', rule['listen_port'])
        settings = config.get('dashboard', {})
        if settings.get('enabled', False):
            servers.append(await asyncio.start_server(dashboard.handle, settings.get('host', '127.0.0.1'), settings.get('port', 8080), limit=8192))
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await stop.wait()
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        for task in sessions:
            task.cancel()
        await asyncio.gather(*sessions, return_exceptions=True)


def main():
    os.umask(0o077)
    config = load_config(os.environ.get('CONFIG_PATH', '/config/config.yaml'))
    settings = config.get('logging', {})
    directory = Path(settings.get('directory', '/logs'))
    directory.mkdir(parents=True, exist_ok=True)
    level = settings.get('level', 'INFO').upper()
    # TRACE deliberately has the same sanitized protocol detail as DEBUG; no raw dumps.
    handler = RotatingFileHandler(directory / 'bridge.log', maxBytes=settings.get('max_bytes', 10485760), backupCount=settings.get('backups', 5))
    logging.basicConfig(level=logging.DEBUG if level == 'TRACE' else level,
                        format='%(asctime)s %(levelname)s %(message)s',
                        datefmt='%Y-%m-%dT%H:%M:%S%z',
                        handlers=[logging.StreamHandler(), handler])
    asyncio.run(serve(config))


if __name__ == '__main__':
    main()
