import argparse
import io
import json
import threading
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from faster_whisper.audio import decode_audio


DATA_DIR = Path(__file__).resolve().parents[1] / 'data/wake-calibration'
MAX_BYTES = 6 * 1024 * 1024
PAGE = '''<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ノルンの録音調整</title>
<style>body{font-family:"Yu Gothic UI",Meiryo,sans-serif;max-width:640px;margin:48px auto;padding:0 20px;color:#202b24;background:#f4f5ee}h1{font-size:24px}p{line-height:1.8}button{font:inherit;padding:12px 16px;border:1px solid #3d654c;border-radius:6px;margin:4px;background:#fff;color:#202b24}button:disabled{opacity:.45}#start{background:#3d654c;color:white}audio{display:block;width:100%;margin:20px 0}#status{min-height:48px;overflow-wrap:anywhere}</style>
<h1>ノルンの録音調整</h1>
<p>起動しやすい言い方で「ノルン」を2回、続けて起動しにくい言い方で2回、1秒程度ずつあけて話してください。</p>
<p>録音は最大15秒です。録音開始ボタンを押すまでマイクは使いません。保存先はこのPCのGit管理外で、Geminiや外部サービスへ送信しません。</p>
<button id="start">録音開始（15秒）</button><button id="stop" disabled>録音終了</button>
<audio id="preview" controls></audio><button id="save" disabled>この録音をローカルへ保存</button>
<p id="status" role="status">未録音</p>
<script>
const start=document.querySelector('#start'),stop=document.querySelector('#stop'),save=document.querySelector('#save'),status=document.querySelector('#status'),preview=document.querySelector('#preview');
let recorder,clip,url,timer,clock;
start.onclick=async()=>{
 try{
  const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
  const type=['audio/webm;codecs=opus','audio/mp4'].find(value=>MediaRecorder.isTypeSupported(value));
  recorder=new MediaRecorder(stream,type?{mimeType:type}:undefined);const chunks=[];clip=null;save.disabled=true;
  recorder.ondataavailable=event=>{if(event.data.size)chunks.push(event.data)};
  recorder.onstop=()=>{clearTimeout(timer);clearInterval(clock);stream.getTracks().forEach(track=>track.stop());clip=new Blob(chunks,{type:recorder.mimeType});if(url)URL.revokeObjectURL(url);url=URL.createObjectURL(clip);preview.src=url;save.disabled=false;start.disabled=false;stop.disabled=true;status.textContent='録音終了。再生して確認し、保存してください。'};
  recorder.start();start.disabled=true;stop.disabled=false;let seconds=0;status.textContent='録音中 0 / 15秒';clock=setInterval(()=>{seconds++;status.textContent=`録音中 ${seconds} / 15秒`},1000);timer=setTimeout(()=>{if(recorder.state==='recording')recorder.stop()},15000);
 }catch(error){status.textContent='マイクを開始できませんでした。ブラウザーのマイク権限を確認してください。'}
};
stop.onclick=()=>{if(recorder?.state==='recording')recorder.stop()};
save.onclick=async()=>{if(!clip)return;save.disabled=true;start.disabled=true;status.textContent='このPCへ保存中…';try{const response=await fetch('/capture',{method:'POST',headers:{'Content-Type':clip.type},body:clip});const result=await response.json();if(!response.ok)throw new Error(result.error);status.textContent='保存しました。チャットに戻って「録音した」と伝えてください。'}catch(error){status.textContent=error.message;save.disabled=false;start.disabled=false}};
window.addEventListener('pagehide',()=>{if(recorder?.state==='recording')recorder.stop()});
</script></html>'''


def store_clip(contents, directory=DATA_DIR):
    if not contents or len(contents) > MAX_BYTES:
        raise ValueError('録音サイズが不正です。15秒以内で録音してください。')
    samples = decode_audio(io.BytesIO(contents), sampling_rate=16000)
    if not 16000 <= len(samples) <= 16000 * 20 or not np.isfinite(samples).all():
        raise ValueError('1秒から20秒以内の音声を録音してください。')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'wake-{uuid.uuid4().hex[:12]}.wav'
    pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2')
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm.tobytes())
    path.chmod(0o600)
    return {'path': str(path), 'seconds': round(len(samples) / 16000, 2),
            'rms': round(float(np.sqrt(np.mean(samples ** 2))), 6),
            'peak': round(float(np.max(np.abs(samples))), 6)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8060)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(bytes(32000))
        with TemporaryDirectory() as temporary:
            result = store_clip(buffer.getvalue(), Path(temporary))
            assert result['seconds'] == 1 and Path(result['path']).is_file()
        print('PASS: local WAV conversion and private file storage; no transcription or LLM calls')
        return
    completed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != '/':
                self.send_error(404)
                return
            data = PAGE.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            try:
                if self.path != '/capture' or self.headers.get('Origin') != f'http://127.0.0.1:{args.port}':
                    raise ValueError('この録音画面から保存してください。')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_BYTES or not self.headers.get('Content-Type', '').startswith('audio/'):
                    raise ValueError('録音データが不正です。')
                result = store_clip(self.rfile.read(size))
                status = 200
            except Exception as error:
                result = {'error': str(error)}
                status = 400
            data = json.dumps(result, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            if status == 200:
                print(json.dumps(result, ensure_ascii=False), flush=True)
                completed.set()

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f'Calibration page: http://127.0.0.1:{args.port}/ ; recording requires explicit button press.', flush=True)
    try:
        completed.wait()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()