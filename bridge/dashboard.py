"""Read-only, allowlisted monitoring data. Never serialize protocol/config objects."""
import asyncio
import json
import re
import time
from urllib.parse import urlsplit

from . import __version__
from .dashboard_ui import HTML


def redacted_upstream_url(url):
    target = urlsplit(url)
    host = target.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    port = f':{target.port}' if target.port is not None else ''
    query = f'?{target.query}' if target.query else ''
    return f'{target.scheme}://{host}{port}{target.path}{query}'


def format_duration(seconds):
    remaining = max(0, int(seconds))
    parts = []
    for unit, size in (('year', 31536000), ('week', 604800), ('day', 86400),
                       ('hour', 3600), ('minute', 60), ('second', 1)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count} {unit}{'s' if count != 1 else ''}")
        if len(parts) == 2:
            break
    return ' '.join(parts) or '0 seconds'


def sdp_details(body):
    """Expose only media types and validated RTP codec descriptions, never raw SDP."""
    tracks = []
    for line in body.decode('utf-8', errors='replace').splitlines():
        if line.startswith('m='):
            fields = line[2:].split()
            kind = fields[0] if fields else ''
            tracks.append({'media': kind if kind in ('audio', 'video', 'application', 'text') else 'other', 'codecs': []})
        elif tracks:
            match = re.fullmatch(r'a=rtpmap:(\d{1,3}) ([A-Za-z0-9_-]{1,32})/(\d{1,6})(?:/(\d{1,2}))?', line)
            if match and match[2].upper() in {'H264', 'H265', 'HEVC', 'JPEG', 'MP4V-ES', 'MPEG4-GENERIC', 'PCMU', 'PCMA', 'OPUS', 'L16', 'G722', 'VP8', 'VP9', 'AV1'}:
                tracks[-1]['codecs'].append({'payload_type': int(match[1]), 'codec': match[2].upper(), 'clock_rate': int(match[3]), 'channels': int(match[4]) if match[4] else None})
            if tracks[-1]['media'] == 'video':
                dimensions = re.fullmatch(r'a=framesize:\d{1,3} (\d{1,5})-(\d{1,5})', line)
                if dimensions:
                    width, height = map(int, dimensions.groups())
                    if 0 < width <= 16384 and 0 < height <= 16384:
                        tracks[-1]['resolution'] = {'width': width, 'height': height}
    return tracks


class Dashboard:
    def __init__(self, config):
        self.config = config
        self.sessions = {}
        self.started = time.monotonic()

    def snapshot(self):
        sessions = []
        for session in self.sessions.values():
            sessions.append({'id': session.id, 'route_port': session.rule['listen_port'], 'source_ip': session.peer,
                             'state': session.state, 'age': format_duration(time.monotonic() - session.started), 'age_seconds': int(time.monotonic() - session.started),
                             'packets': list(session.packets), 'bytes': list(session.bytes),
                             'tracks': [{'index': t['index'], 'packets': list(t['packets']), 'bytes': list(t['bytes'])} for t in session.tracks],
                             'sdp': session.sdp})
        return {'version': __version__, 'uptime': format_duration(time.monotonic() - self.started), 'uptime_seconds': int(time.monotonic() - self.started),
                'connected': len(sessions), 'playing': sum(s['state'] == 'playing' for s in sessions),
                'routes': [{'listen_port': r['listen_port'],
                            'connected': sum(s['route_port'] == r['listen_port'] for s in sessions), 'downstream_transport': r.get('downstream_transport', 'udp'),
                            'upstream_transport': r['upstream'].get('transport', 'tcp'),
                            'upstream_url': redacted_upstream_url(r['upstream'].get('url', '')),
                            'tls_verify': r['upstream'].get('tls_verify', True),
                            'credential_mode': r.get('credentials', {}).get('mode', 'drop'),
                            'audio_enabled': r.get('audio_enabled', True)} for r in self.config['rules']],
                'sessions': sessions}

    async def handle(self, reader, writer):
        try:
            request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            method, path, _ = request.split(b'\r\n', 1)[0].split()
            headers = b'Content-Type: application/json\r\n'
            if method != b'GET':
                status, body = '405 Method Not Allowed', b'{}'
                headers += b'Allow: GET\r\n'
            elif path == b'/api/status':
                status, body = '200 OK', json.dumps(self.snapshot()).encode()
            elif path == b'/':
                status, body = '200 OK', HTML.encode()
                headers = b'Content-Type: text/html; charset=utf-8\r\n'
            else:
                status, body = '404 Not Found', b'{}'
            writer.write(f'HTTP/1.1 {status}\r\n'.encode() + headers +
                         b'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Security-Policy: default-src \'self\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; connect-src \'self\'; frame-ancestors \'none\'\r\nConnection: close\r\n' +
                         f'Content-Length: {len(body)}\r\n\r\n'.encode() + body)
            await writer.drain()
        except (ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
