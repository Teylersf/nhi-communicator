"""Package an already-built standalone app and its corresponding source."""

import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.1'


def add_tree(archive, directory, prefix):
    for path in sorted(directory.rglob('*')):
        if path.is_file():
            archive.write(path, str(Path(prefix) / path.relative_to(directory)).replace('\\', '/'))


def main():
    output = ROOT / 'dist' / 'NHI Communicator'
    if not (output / 'NHI Communicator.exe').is_file():
        raise RuntimeError('Build the standalone executable before packaging.')
    licenses = ROOT / 'licenses'
    for package, filename, label in [('numpy', 'LICENSE.txt', 'numpy'),
                                      ('pyinstaller', 'licenses/COPYING.txt', 'pyinstaller')]:
        distribution = importlib.metadata.distribution(package)
        license_file = Path(distribution._path) / filename
        if not license_file.is_file():
            raise RuntimeError(f'Missing license for {package}.')
        target = licenses / label
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(license_file, target / 'LICENSE.txt')
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    (licenses / 'python').mkdir(parents=True, exist_ok=True)
    shutil.copyfile(python_license, licenses / 'python' / 'LICENSE.txt')
    for filename in ['Start NHI Communicator.cmd', 'Start Shielded Lab.cmd', 'Start Preview.cmd',
                     'Start-NHICommunicator.ps1', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md']:
        shutil.copyfile(ROOT / filename, output / filename)
    shutil.copytree(licenses, output / 'licenses', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'docs', output / 'docs', dirs_exist_ok=True)
    manifest = {'application': 'NHI Communicator', 'version': VERSION,
                'python': sys.version.split()[0], 'numpy': importlib.metadata.version('numpy'),
                'pyinstaller': importlib.metadata.version('pyinstaller'), 'platform': 'Windows x64',
                'native_files': {}, 'sources': 'third-party-source-v' + VERSION + '.zip'}
    for folder in [output / '_internal' / 'tools' / 'hackrf' / 'bin',
                   output / '_internal' / 'dashboard']:
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() in ('.dll', '.exe'):
                manifest['native_files'][path.relative_to(output).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (output / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    release = ROOT / 'release'
    release.mkdir(exist_ok=True)
    binary_zip = release / f'nhi-communicator-v{VERSION}-windows-x64.zip'
    with zipfile.ZipFile(binary_zip, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        add_tree(archive, output, 'NHI Communicator')
    source_zip = release / f'third-party-source-v{VERSION}.zip'
    with zipfile.ZipFile(source_zip, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        add_tree(archive, ROOT / 'vendor', 'vendor')
        add_tree(archive, licenses, 'licenses')
        add_tree(archive, ROOT / 'scripts', 'scripts')
        archive.write(ROOT / 'dashboard' / 'radio_control_native.c', 'dashboard/radio_control_native.c')
        archive.write(ROOT / 'dashboard' / 'Build-RadioControl.ps1', 'dashboard/Build-RadioControl.ps1')
        archive.write(ROOT / 'docs' / 'BUILD.md', 'docs/BUILD.md')
        archive.write(ROOT / 'THIRD_PARTY_NOTICES.md', 'THIRD_PARTY_NOTICES.md')
        archive.write(ROOT / 'LICENSE', 'LICENSE')
    checksum_lines = [hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name
                      for path in [binary_zip, source_zip]]
    (release / 'SHA256SUMS.txt').write_text('\n'.join(checksum_lines) + '\n', encoding='ascii')
    print('\n'.join(checksum_lines))


if __name__ == '__main__':
    main()
