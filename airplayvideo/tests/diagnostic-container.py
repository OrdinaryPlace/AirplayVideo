"""Exercise the actual diagnostic job with sandboxed Chrome, no LAN receiver."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import aiohttp
from service.main import Application
from service.model import Store, default_setup


async def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = Store(root)
        settings = default_setup()
        settings['video']['encoder'] = 'libopenh264'
        store.setup(settings)
        async with aiohttp.ClientSession() as session:
            app = Application(store, session)
            (root / 'browser').mkdir()
            sentinel = root / 'browser' / 'preserved'
            sentinel.write_text('untouched profile')
            for i in range(8):
                (app.recordings.root / (str(i)*32 + '.json')).write_text('{}')
            before = (root / 'settings.json').read_bytes()
            await app.diagnostics.start({})
            await asyncio.wait_for(app.diagnostics.task, 170)
            result = app.diagnostics.report
            print(json.dumps(result), flush=True)
            assert result['status'] == 'complete', result
            for key in ('browser_500', 'browser_1500'):
                measured = result['stages'][key][0]
                assert measured['capture']['matched_events'] >= 5
                assert measured['maximum_schedule_error_us'] < 24
                video = measured['capture']['video']
                assert video['frames'] > 0 and video['fps'] > 0 and video['max_gap_ms'] > 0
                assert video['frames'] == measured['wire']['video']['frames']
                # CI host load is variable. Clock-boundary frame loss is tested
                # deterministically in frame-rate.cpp; report actual throughput.
            assert not app.controller.targets and app.controller.stream is None
            assert not app.browser.running and not result['physical_output_measured']
            assert sentinel.read_text() == 'untouched profile'
            assert before == (root / 'settings.json').read_bytes()
            assert len(list(app.recordings.root.glob('*.json'))) == 8
            await app.close()


asyncio.run(main())
