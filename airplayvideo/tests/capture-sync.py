"""Record a real 1080p sandboxed Chrome/X11/Pulse pipeline, without any TV.

The page schedules white flashes and 1 kHz beeps on the same AudioContext
clock. Each event is a 100 ms pulse, repeated every second. This distinguishes
captured content alignment from merely having similar end timestamps.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import uuid
import aiohttp
from aiohttp import web
from service.browser import Browser
from service.controller import Stream
from service.model import default_setup


PAGE = '''<!doctype html><title>A/V clock fixture</title>
<style>html,body{margin:0;background:#000;width:100%;height:100%;overflow:hidden}canvas{width:100%;height:100%}</style>
<canvas id="screen" width="1920" height="1080"></canvas><script>
const audio=new AudioContext({sampleRate:48000}), canvas=document.getElementById('screen').getContext('2d');
const origin=audio.currentTime+3;
let scheduled=0;
function schedule(){
  while(origin+scheduled<audio.currentTime+2){
    const start=origin+scheduled++, oscillator=audio.createOscillator(), gain=audio.createGain();
    oscillator.frequency.value=1000;gain.gain.value=0.3;oscillator.connect(gain).connect(audio.destination);
    oscillator.start(start);oscillator.stop(start+0.1);
  }
}
function draw(){
 const t=audio.currentTime-origin, on=t>=0 && t%1<0.1;
 canvas.fillStyle=on?'#fff':'#000';canvas.fillRect(0,0,1920,1080);
 requestAnimationFrame(draw);
}
audio.resume().then(()=>{setInterval(schedule,30);schedule();draw();fetch('/ready',{method:'POST'})});
</script>'''


async def main():
    os.umask(0o077)
    output=Path(os.environ.get('CAPTURE_OUTPUT','/captures'));output.mkdir(exist_ok=True)
    ready=asyncio.Event()
    async def loaded(request):
        ready.set();return web.Response(text='ready')
    app=web.Application();app.router.add_get('/',lambda _:web.Response(text=PAGE,content_type='text/html'))
    app.router.add_post('/ready',loaded)
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    async with aiohttp.ClientSession() as session:
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary).chmod(0o755)
            browser=Browser(Path(temporary)/'browser',session);stream=None
            finished=asyncio.get_running_loop().create_future()
            async def event(_,name,fields):
                if name=='recording_finished' and not finished.done():finished.set_result(fields)
                if name in ('source_error','fatal') and not finished.done():finished.set_exception(RuntimeError(fields['message']))
            try:
                settings=default_setup()
                await browser.navigate('http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])+'/',settings)
                await asyncio.wait_for(ready.wait(),20)
                config={'source':{'kind':'browser','display':browser.environment['DISPLAY'],'pulse':'airplayvideo.monitor'},'width':1920,'height':1080,'fps':30,'bitrate':8000000,'encoder':'libopenh264','audio':True,'latency_ms':500}
                stream=Stream(output,config,browser.environment,event);await stream.start()
                capture_id=uuid.uuid4().hex
                await stream.command({'action':'record','id':capture_id,'seconds':15})
                report=await asyncio.wait_for(finished,30)
                print(json.dumps(report),flush=True)
                assert report['status']=='complete',report
            finally:
                if stream:await stream.close()
                await browser.close()
    await runner.cleanup()

asyncio.run(asyncio.wait_for(main(),80))
