"""Download and verify pinned native packages, licenses, SDK, and source archives.

Does not install a driver, open USB, or modify an external environment.
"""

import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request
import zipfile

import zstandard

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache' / 'native'
BIN = ROOT / 'tools' / 'hackrf' / 'bin'
SDK = ROOT / 'tools' / 'hackrf' / 'sdk'
VENDOR = ROOT / 'vendor'
PACKAGES = [
    ('hackrf', 'hackrf-2024.02.1-hc14e81b_1.conda',
     '67b73e897e3f95db42ad635faf6f295acbc19df916fbe775005b98ee6ab8545e'),
    ('libhackrf0', 'libhackrf0-2024.02.1-h2466b09_1.conda',
     '1a8b5a6103de8ec936c17b7ae68d86475ae155ff8a77669ac379c5dbaae703ff'),
    ('libusb', 'libusb-1.0.30-h6a83c73_0.conda',
     '8c64e0d69eb4458100a94c1d1d4c4a52955d0d4931699962b0c407a2a66836a5'),
    ('libwinpthread', 'libwinpthread-12.0.0.r4.gg4f2fc60ca-h57928b3_10.conda',
     '0fccf2d17026255b6e10ace1f191d0a2a18f2d65088fd02430be17c701f8ffe0'),
    ('winpthreads-devel', 'winpthreads-devel-12.0.0.r4.gg4f2fc60ca-h57928b3_10.conda',
     'd640e8ef78f82cad1584a123f655f0e9fe9a143d8aa5ed503522b04255b0624f'),
    ('vc14_runtime', 'vc14_runtime-14.51.36247-habf1de7_41.conda',
     '4e4cb599cdc41bf2109d1464c127b5bcbddf548ce3e322e612afb691338b48f8'),
]
RELEASE_BINARIES = {'hackrf_transfer.exe', 'hackrf_info.exe', 'hackrf-0.dll',
                    'libusb-1.0.dll', 'libwinpthread-1.dll', 'vcruntime140.dll'}
SOURCES = [
    ('hackrf-2024.02.1.tar.xz',
     'https://github.com/greatscottgadgets/hackrf/releases/download/v2024.02.1/hackrf-2024.02.1.tar.xz',
     'd9ced67e6b801cd02c18d0c4654ed18a4bcb36c24a64330c347dfccbd859ad16'),
    ('libusb-1.0.30.tar.bz2',
     'https://github.com/libusb/libusb/releases/download/v1.0.30/libusb-1.0.30.tar.bz2',
     'fea36f34f9156400209595e300840767ab1a385ede1dc7ee893015aea9c6dbaf'),
]


def download(url, path, expected):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        print(f'Downloading {path.name}', flush=True)
        request = urllib.request.Request(url, headers={'User-Agent': 'NHI-Communicator-build/0.1.0'})
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise RuntimeError(f'Checksum mismatch for {path.name}.')
        path.write_bytes(payload)
    return path


def write_member(archive, member, directory, relative):
    # Package paths never choose a destination outside the named directory.
    target = (directory / relative).resolve()
    if not target.is_relative_to(directory.resolve()):
        raise RuntimeError('Unsafe package path.')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(archive.extractfile(member).read())


def unpack_package(path, label):
    with zipfile.ZipFile(path) as package:
        for entry in package.namelist():
            if not entry.endswith('.tar.zst'):
                continue
            with zstandard.ZstdDecompressor().stream_reader(package.open(entry)) as stream:
                with tarfile.open(fileobj=stream, mode='r|') as archive:
                    for member in archive:
                        if not member.isfile():
                            continue
                        name = member.name.replace('\\', '/')
                        if name.startswith('info/licenses/'):
                            write_member(archive, member, ROOT / 'licenses' / label,
                                         name[len('info/licenses/'):])
                        elif name.startswith('info/recipe/'):
                            write_member(archive, member, VENDOR / 'recipes' / label,
                                         name[len('info/recipe/'):])
                        elif name.startswith('Library/bin/') and Path(name).name in RELEASE_BINARIES:
                            write_member(archive, member, BIN, Path(name).name)
                        elif label in ('libusb', 'winpthreads-devel') and name.startswith('Library/'):
                            relative = name[len('Library/'):]
                            if relative.startswith(('include/', 'lib/')):
                                write_member(archive, member, SDK, relative)


def main():
    provenance = []
    for label, filename, checksum in PACKAGES:
        url = 'https://conda.anaconda.org/conda-forge/win-64/' + filename
        unpack_package(download(url, CACHE / filename, checksum), label)
        provenance.append({'component': label, 'package': filename, 'url': url, 'sha256': checksum})
    for filename, url, checksum in SOURCES:
        download(url, VENDOR / 'source-archives' / filename, checksum)
        provenance.append({'source_archive': filename, 'url': url, 'sha256': checksum})
    VENDOR.mkdir(parents=True, exist_ok=True)
    (VENDOR / 'native-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n', encoding='utf-8')
    missing = RELEASE_BINARIES - {path.name for path in BIN.iterdir()}
    if missing:
        raise RuntimeError(f'Missing native files: {sorted(missing)}')
    print('Native packages, licenses, SDK, and complete corresponding source are ready.')


if __name__ == '__main__':
    main()
