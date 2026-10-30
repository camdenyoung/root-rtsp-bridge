"""Read-only, allowlisted monitoring data. Never serialize protocol/config objects."""
import asyncio
import json
import re
import time


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
    return tracks


class Dashboard:
    def __init__(self, config):
        self.config = config
        self.sessions = {}
        self.started = time.monotonic()

    def snapshot(self):
        sessions = []
        for session in self.sessions.values():
            sessions.append({'id': session.id, 'route_port': session.rule['listen_port'],
                             'state': session.state, 'age_seconds': int(time.monotonic() - session.started),
                             'packets': list(session.packets), 'bytes': list(session.bytes),
                             'tracks': [{'index': t['index'], 'packets': list(t['packets']), 'bytes': list(t['bytes'])} for t in session.tracks],
                             'sdp': session.sdp})
        return {'uptime_seconds': int(time.monotonic() - self.started),
                'connected': len(sessions), 'playing': sum(s['state'] == 'playing' for s in sessions),
                'routes': [{'listen_port': r['listen_port'], 'downstream_transport': r.get('downstream_transport', 'udp'),
                            'upstream_transport': r['upstream'].get('transport', 'tcp'),
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
<style>body{font:16px system-ui;margin:2rem auto;padding:0 1rem;max-width:1100px;background:#101923;color:#e4edf5}h1,h2{color:#8dded1}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:.7rem;border-bottom:1px solid #385063}pre{white-space:pre-wrap}small{color:#afbecb}</style>
<h1>Root RTSP Bridge</h1><small>Read-only monitoring · Refreshes every 2 seconds · Credentials and stream tokens hidden</small><p id="health">Loading…</p>
<h2>Configured routes</h2><div id="routes"></div><h2>Sessions</h2><p>Media counters include RTP and RTCP. Direction: camera → client / client → camera. Counters cover each connected session.</p><div id="sessions"></div>
<script>
function table(target, heads, rows){const t=document.createElement('table');const h=t.insertRow();heads.forEach(v=>{const c=document.createElement('th');c.textContent=v;h.append(c)});rows.forEach(row=>{const r=t.insertRow();row.forEach(v=>{const c=r.insertCell();c.textContent=v})});document.getElementById(target).replaceChildren(t)}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error();const s=await r.json();document.getElementById('health').textContent=s.connected+' connected · '+s.playing+' playing · Uptime '+s.uptime_seconds+'s · Updated '+new Date().toLocaleTimeString();table('routes',['Port','Client transport','Camera transport','Audio'],s.routes.map(r=>[r.listen_port,r.downstream_transport,r.upstream_transport,r.audio_enabled?'Enabled':'Disabled']));table('sessions',['Session / route','State / age','Packets → / ←','Bytes → / ←','Track counters / available SDP'],s.sessions.map(r=>[r.id+' / '+r.route_port,r.state+' / '+r.age_seconds+'s',r.packets.join(' / '),r.bytes.join(' / '),JSON.stringify({tracks:r.tracks,sdp:r.sdp})]));if(!s.sessions.length)document.getElementById('sessions').textContent='No connected sessions.'}catch(e){document.getElementById('health').textContent='Dashboard unavailable; displayed values may be stale.'}finally{setTimeout(refresh,2000)}}refresh();
</script></html>'''
