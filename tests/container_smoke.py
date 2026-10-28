"""Optional Docker smoke test; first build root-rtsp-bridge:test."""
import subprocess, tempfile, pathlib, time, os

def run(*args):
    result = subprocess.run(args,capture_output=True,text=True)
    if result.returncode:
        raise RuntimeError(result.stderr + result.stdout)
    return result.stdout
with tempfile.TemporaryDirectory(prefix='rtsp-smoke-') as directory:
    root=pathlib.Path(directory)
    (root/'config').mkdir()
    (root/'logs').mkdir()
    (root/'config'/'config.yaml').write_text('listen_host: 127.0.0.1\nlogging:\n  directory: /logs\nrules:\n  - listen_port: 18554\n    upstream:\n      url: rtsp://127.0.0.1:18555/private-token\n')
    name='rtsp-bridge-smoke'
    try:
        run('docker','run','-d','--name',name,'--network','none','--user',f'{os.getuid()}:{os.getgid()}','--read-only','--cap-drop','ALL','-v',str(root/'config')+':/config:ro','-v',str(root/'logs')+':/logs','root-rtsp-bridge:test')
        for attempt in range(30):
            if (root/'logs'/'bridge.log').exists() and 'event=listening' in (root/'logs'/'bridge.log').read_text(): break
            time.sleep(.1)
        result=run('docker','exec',name,'python','-c',"import socket; s=socket.create_connection(('127.0.0.1',18554)); s.sendall(b'OPTIONS rtsp://127.0.0.1:18554/ RTSP/1.0\\r\\nCSeq: 1\\r\\n\\r\\n'); r=s.recv(4096); assert b'502 Bad Gateway' in r, r; print('Expected upstream failure returned as RTSP 502'); s.close()")
        run('docker','restart',name)
        for attempt in range(30):
            log=(root/'logs'/'bridge.log').read_text()
            if log.count('event=listening') == 2: break
            time.sleep(.1)
        assert log.count('event=listening') == 2,log
        assert 'stage=upstream_connect' in log,log
        assert 'private-token' not in log,log
        print(result.strip())
        print('Container startup, config mount, persistent logs across restart, and token redaction passed')
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True)
