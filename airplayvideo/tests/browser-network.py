"""Run in an isolated Linux container with SYS_ADMIN, never with production data."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import socket
import tempfile
import aiohttp
from service.browser import Browser
from service.model import default_setup


async def main():
    with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        Path(directory).chmod(0o755)
        for port in (5900, 9222):
            listener = resources.enter_context(socket.socket())
            listener.bind(("127.0.0.1", port))
            listener.listen()
        async with aiohttp.ClientSession() as session:
            browsers = [Browser(Path(directory) / name, session) for name in ("one", "two")]
            try:
                for number, browser in enumerate(browsers):
                    await browser.start(default_setup())
                    await browser.evaluate(f"document.title='Browser {number}'")
                    reader, writer = await browser.preview_connection()
                    writer.close()
                    await writer.wait_closed()
                assert browsers[0].environment["DISPLAY"] != browsers[1].environment["DISPLAY"]
                ports = {5900, 9222, *(b.vnc_port for b in browsers), *(b.cdp_port for b in browsers)}
                assert len(ports) == 6
                for number, browser in enumerate(browsers):
                    assert (await browser.cdp("Runtime.evaluate", {"expression": "document.title", "returnByValue": True}))["result"]["value"] == f"Browser {number}"
                print("Two isolated browser displays, CDP connections and authenticated previews coexist with occupied default ports")
            finally:
                for browser in browsers:
                    await browser.close()


asyncio.run(asyncio.wait_for(main(), 90))
