"""A signed local Chrome extension, using normal extension APIs, never CDP."""
import asyncio
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import zipfile
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import pkcs1_15
from Cryptodome.Hash import SHA256
from .model import UserError, check

SOURCE = Path(__file__).resolve().parent.parent / 'companion'
HOST_NAME = 'com.ordinaryplace.airplayvideo'


def varint(number):
    data = bytearray()
    while number > 127:
        data.append((number & 127) | 128)
        number >>= 7
    return bytes(data + bytes([number]))


def field(number, value):
    return varint(number * 8 + 2) + varint(len(value)) + value


def write_file(path, data, mode=0o644, owner=None):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        if owner is not None:
            os.fchown(fd, owner, owner)
        with os.fdopen(fd, 'wb', closefd=False) as output:
            output.write(data)
    finally:
        os.close(fd)


def install(root, source=SOURCE, registration=Path('/usr/share/google-chrome/extensions'), packages=Path('/opt/airplayvideo/installed')):
    """Preserve the private signing key so updates keep the extension identity."""
    root = Path(root)
    key_path = root.parent / 'browser-controls.pem'
    if not key_path.exists():
        write_file(key_path, RSA.generate(2048).export_key(), 0o600)
    key = RSA.import_key(key_path.read_bytes())
    public = key.public_key().export_key(format='DER')
    digest = hashlib.sha256(public).digest()[:16]
    identity = ''.join(chr(97 + int(char, 16)) for char in digest.hex())
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(source.iterdir()):
            if path.is_file():
                bundle.writestr(path.name, path.read_bytes())
    payload = archive.getvalue()
    signed = field(1, digest)
    proof = b'CRX3 SignedData\0' + struct.pack('<I', len(signed)) + signed + payload
    signature = pkcs1_15.new(key).sign(SHA256.new(proof))
    header = field(2, field(1, public) + field(2, signature)) + field(10000, signed)
    version = json.loads((source / 'manifest.json').read_text())['version']
    for directory in (registration, packages):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
    package = packages / (identity + '.crx')
    write_file(package, b'Cr24' + struct.pack('<II', 3, len(header)) + header + payload)
    write_file(registration / (identity + '.json'), json.dumps({'external_crx': str(package), 'external_version': version}).encode())
    # User-specific registration follows the actual --user-data-dir, allowing
    # concurrent private browsers to connect only to their own app instance.
    profile = root / 'profile'
    profile.mkdir(exist_ok=True, mode=0o700)
    check(profile.is_dir() and not profile.is_symlink(), 'Invalid browser profile directory')
    os.chown(profile, 1000, 1000)
    hosts = profile / 'NativeMessagingHosts'
    hosts.mkdir(exist_ok=True, mode=0o700)
    check(hosts.is_dir() and not hosts.is_symlink(), 'Invalid browser control directory')
    os.chown(hosts, 1000, 1000)
    manifest = {'name': HOST_NAME, 'description': 'AirplayVideo private browser controls', 'path': str(Path(__file__).with_name('companion_host.py')), 'type': 'stdio', 'allowed_origins': ['chrome-extension://' + identity + '/']}
    write_file(hosts / (HOST_NAME + '.json'), json.dumps(manifest).encode(), 0o600, 1000)
    return identity


async def read_message(reader):
    size = struct.unpack('=I', await reader.readexactly(4))[0]
    check(0 < size <= 65536, 'Invalid browser control message')
    message = json.loads(await reader.readexactly(size))
    check(isinstance(message, dict), 'Invalid browser control message')
    return message


class Companion:
    def __init__(self, root, identity):
        self.path = Path(root) / 'controls.sock'
        self.identity = identity
        self.server = None
        self.writer = None
        self.clients = set()
        self.tasks = set()
        self.ready = asyncio.Event()
        self.pending = {}
        self.sequence = 0
        self.lock = asyncio.Lock()

    async def start(self):
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()  # Only our stale runtime socket, never profile data.
        self.server = await asyncio.start_unix_server(self.accept, path=self.path, limit=65540)
        os.chmod(self.path, 0o600)
        os.chown(self.path, 1000, 1000)

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.clients.add(writer)
        try:
            hello = await asyncio.wait_for(read_message(reader), 5)
            check(hello.get('hello') == self.identity and self.writer is None, 'Browser control connection rejected')
            self.writer = writer
            self.ready.set()
            while True:
                message = await read_message(reader)
                check(type(message.get('id')) is int, 'Invalid browser control reply')
                check('error' in message or isinstance(message.get('result'), dict), 'Invalid browser control reply')
                future = self.pending.get(message['id'])
                if future is not None and not future.done():
                    future.set_result(message)
        except (UserError, OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            if self.writer is writer:
                self.writer = None
                self.ready.clear()
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(UserError('Browser controls disconnected; reopen the browser'))
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.clients.discard(writer)
            self.tasks.discard(task)

    async def wait_ready(self):
        try:
            await asyncio.wait_for(self.ready.wait(), 30)
            await self.call('ready')
        except asyncio.TimeoutError as exc:
            raise UserError('Browser controls did not connect; close and reopen the browser') from exc

    async def call(self, action, **arguments):
        async with self.lock:
            check(self.writer is not None, 'Browser controls are not ready')
            self.sequence += 1
            sequence, writer = self.sequence, self.writer
            future = asyncio.get_running_loop().create_future()
            self.pending[sequence] = future
            payload = json.dumps({'id': sequence, 'action': action, **arguments}).encode()
            try:
                check(len(payload) <= 65536, 'Browser control request is too large')
                writer.write(struct.pack('=I', len(payload)) + payload)
                await writer.drain()
                response = await asyncio.wait_for(future, 10)
                check('error' not in response, 'Browser control could not finish')
                return response.get('result', {})
            except (OSError, asyncio.TimeoutError) as exc:
                # An operation might have run before the reply was lost. Do not
                # retry it automatically or accept a delayed reply as new work.
                writer.close()
                raise UserError('Browser control did not respond; reopen the browser') from exc
            finally:
                self.pending.pop(sequence, None)

    async def close(self):
        if self.server:
            self.server.close()
        for writer in list(self.clients):
            writer.close()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.server:
            await self.server.wait_closed()
        self.server = None
