"""Read-only, allowlisted monitoring data. Never serialize protocol/config objects."""
import asyncio
import json
import re
import time
from urllib.parse import urlsplit


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
        return {'uptime': format_duration(time.monotonic() - self.started), 'uptime_seconds': int(time.monotonic() - self.started),
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


HTML = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Root RTSP Bridge</title>
<style>body{font:16px system-ui;margin:2rem 0;padding:0 clamp(1rem,2vw,3rem);background:#101923;color:#e4edf5}h1,h2{color:#8dded1}#routes,#sessions{overflow-x:auto}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:.7rem;border-bottom:1px solid #385063;vertical-align:top}th{white-space:nowrap}td:last-child{overflow-wrap:anywhere}#sessions td:last-child{min-width:20rem;max-width:40rem}@media(max-width:700px){td,th{padding:.5rem;font-size:.875rem}}pre{white-space:pre-wrap}small{color:#afbecb}</style>
<h1>Root RTSP Bridge</h1><small>Read-only monitoring · Refreshes every 2 seconds · Credentials hidden</small><p id="health">Loading…</p>
<h2>Configured routes</h2><p>Upstream URLs include paths and queries; embedded usernames and passwords are hidden. TLS verification applies to RTSPS connections.</p><div id="routes"></div><h2>Sessions</h2><p>Media counters include RTP and RTCP. Direction: camera → client / client → camera. Counters cover each connected session.</p><div id="sessions"></div>
<script>
function table(target, heads, rows){const t=document.createElement('table');const h=t.insertRow();heads.forEach(v=>{const c=document.createElement('th');c.textContent=v;h.append(c)});rows.forEach(row=>{const r=t.insertRow();row.forEach(v=>{const c=r.insertCell();c.textContent=v})});document.getElementById(target).replaceChildren(t)}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error();const s=await r.json();document.getElementById('health').textContent=s.connected+' connected · '+s.playing+' playing · Uptime '+s.uptime+' · Updated '+new Date().toLocaleTimeString();table('routes',['Port','Connections','Upstream URL','TLS verify','Credential mode','Client transport','Camera transport','Audio'],s.routes.map(r=>[r.listen_port,r.connected,r.upstream_url,String(r.tls_verify),r.credential_mode,r.downstream_transport,r.upstream_transport,r.audio_enabled?'Enabled':'Disabled']));table('sessions',['Session / route','Source IP','State / age','Packets → / ←','Bytes → / ←','Resolution (SDP)','Track counters / available SDP'],s.sessions.map(r=>[r.id+' / '+r.route_port,r.source_ip,r.state+' / '+r.age,r.packets.join(' / '),r.bytes.join(' / '),(r.sdp||[]).filter(t=>t.media==='video').map(t=>t.resolution?t.resolution.width+' × '+t.resolution.height:'Unavailable').join(', ')||'Unavailable',JSON.stringify({tracks:r.tracks,sdp:r.sdp})]));if(!s.sessions.length)document.getElementById('sessions').textContent='No connected sessions.'}catch(e){document.getElementById('health').textContent='Dashboard unavailable; displayed values may be stale.'}finally{setTimeout(refresh,2000)}}refresh();
</script></html>'''
