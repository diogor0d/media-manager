"""Rebuild credential-free masters with pinned Cherri and Apple's signing kept separate."""

import argparse
import os
import runpy
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def build(compiler):
    compiler = Path(compiler).resolve()
    version = subprocess.check_output([str(compiler), '--version'], text=True)
    if 'v2.3.0' not in version:
        raise RuntimeError('This project requires Cherri 2.3.0')
    private = ROOT / 'private'
    private.mkdir(exist_ok=True, mode=0o700)
    finalize = runpy.run_path(str(ROOT / 'finalize.py'))['finalize']
    with tempfile.TemporaryDirectory(prefix='build-', dir=private) as directory:
        work = Path(directory)
        for name in ('convert.cherri', 'compress.cherri', 'setup.cherri', 'job.cherri'):
            (work / name).write_bytes((ROOT / name).read_bytes())
        for source, name in (('convert.cherri', 'Convert Media'),
                             ('compress.cherri', 'Compress Media')):
            subprocess.run([str(compiler), source, '--skip-sign'],
                           cwd=work, check=True)
            artifact = work / f'{name}_unsigned.shortcut'
            finalize(artifact)
        for name in ('Convert Media', 'Compress Media'):
            artifact = work / f'{name}_unsigned.shortcut'
            destination = ROOT / artifact.name
            destination.write_bytes(artifact.read_bytes())
            destination.chmod(0o644)
    print('Rebuilt both credential-free masters; no signing service contacted.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', default=os.environ.get('CHERRI',
                        str(ROOT / 'private/tools/cherri')))
    build(parser.parse_args().compiler)
