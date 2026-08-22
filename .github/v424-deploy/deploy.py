#!/usr/bin/env python3
from __future__ import annotations

import base64
import gzip
import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path.cwd()
SOURCE_BRANCH = 'fix/v424-metricduck-rounded-projection-boundary'
READY_BRANCH = 'fix/v424-metricduck-rounded-projection-boundary-ready'
PATCH_REL = Path('.github/v424-deploy/patch-01.b64')
OLD_STATE = '7397afec8c0202742d31965a259f495c2875b26c137c2641030f7bb971ec74bd'
OLD_MARKET = '70c0686389653bb496f7f7f15fce506d90019b8622c12a6d20a3420bff0319eb'
NEW_STATE = '9a90ef0df925ed992ec392846452e7e661c436c910b422dcd2f9334b424ccf4a'
NEW_MARKET = '9985b2d16357bb174b2535fad45eec0ba0bed66fe0d39bfda5a738dca02a6548'
PATCH_B64_SHA = 'e5c0e9f99871a3a1a88215e4734d76fe460b686ae4d2ceb8665e4e1ab9cfecd0'
PATCH_SHA = '19ce16047be48ad36aba4d8e1ea44336783c62cf80a9630f38e2154eb533f615'
EXPECTED = {
    'screening/config/v42-market-producer-release.json': '9985b2d16357bb174b2535fad45eec0ba0bed66fe0d39bfda5a738dca02a6548',
    'screening/config/v42-state-producer-release.json': '9a90ef0df925ed992ec392846452e7e661c436c910b422dcd2f9334b424ccf4a',
    'screening/engine/v42_runtime.py': 'ae708fea007f947b2e279b99dc857c3e3ce6043f8089f5322b00ae32ead8f83c',
    'screening/qrgf_v42/config/connectors.json': '7dd69059c29edf1fe33939f7165ae27754b92bd6e6deea65caf80b9ad5d07ae6',
    'screening/qrgf_v42/config/policy.json': '41d8bea373a913adc0c571f6d53888bd48018a0d4522f4e483ba5f3e261d9f04',
    'screening/qrgf_v42/scripts/integrity.py': '0d8240cb9b1365360ef9553218e41f616dc4368263d4e5e9e1b9e878fb000e27',
    'screening/qrgf_v42/scripts/policy.py': '6496207822db969051068255abfc3a1d16be25e60011c5c49f6479157a26db4b',
    'screening/qrgf_v42/scripts/provenance.py': 'd4ef5036e915be450b61862aa5ed6dd47b349b2a35d5154fcddd386346fc310c',
    'screening/qrgf_v42/tests/run_tests.py': '7d57c6fe7cb794a3a3bc6e0d58f7b73ff6f1117bdcca7f142d46ad7a4b0da93f',
}


def run(*args: str, capture: bool = False) -> str:
    print('+', ' '.join(args), flush=True)
    p = subprocess.run(args, cwd=REPO, text=True, check=True, capture_output=capture)
    return p.stdout.strip() if capture else ''


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fail(msg: str) -> None:
    print(f'\nFAILED: {msg}', file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    if not (REPO / '.git').exists():
        fail('Run from repository root in GitHub Codespaces.')
    patch_path = REPO / PATCH_REL
    if not patch_path.is_file():
        fail(f'Missing {PATCH_REL}. Checkout {SOURCE_BRANCH} first.')
    patch_b64 = patch_path.read_bytes()
    if hashlib.sha256(patch_b64).hexdigest() != PATCH_B64_SHA:
        fail('Staged patch hash mismatch.')
    try:
        patch = gzip.decompress(base64.b64decode(patch_b64))
    except Exception as exc:
        fail(f'Cannot decode staged patch: {exc}')
    if hashlib.sha256(patch).hexdigest() != PATCH_SHA:
        fail('Decoded patch hash mismatch.')
    tmp = Path('/tmp/qrgf-v424.patch')
    tmp.write_bytes(patch)

    run('git', 'fetch', '--prune', 'origin')
    run('git', 'reset', '--hard')
    run('git', 'clean', '-fd')
    run('git', 'checkout', '-B', READY_BRANCH, 'origin/main')

    if sha(REPO / 'screening/config/v42-state-producer-release.json') != OLD_STATE:
        fail('Remote main state release is not the expected V4.2.3. Stop and tell ChatGPT.')
    if sha(REPO / 'screening/config/v42-market-producer-release.json') != OLD_MARKET:
        fail('Remote main market release is not the expected V4.2.3. Stop and tell ChatGPT.')

    run('git', 'apply', '--check', str(tmp))
    run('git', 'apply', str(tmp))

    for rel, expected in EXPECTED.items():
        got = sha(REPO / rel)
        if got != expected:
            fail(f'Target hash mismatch for {rel}: {got} != {expected}')

    changed = set(filter(None, run('git', 'diff', '--name-only', capture=True).splitlines()))
    if changed != set(EXPECTED):
        fail(f'Unexpected changed files: {sorted(changed)}')
    data_changes = run('git', 'diff', '--name-only', '--', 'data', capture=True)
    if data_changes:
        fail(f'data mutation is forbidden: {data_changes}')

    run('python3', 'screening/qrgf_v42/tests/run_tests.py')
    verify_code = (
        "import sys; sys.path.insert(0,'screening/engine'); import v42_runtime; "
        f"assert v42_runtime.verify_release('screening/config/v42-state-producer-release.json','master_core500_v42')=='{NEW_STATE}'; "
        f"assert v42_runtime.verify_release('screening/config/v42-market-producer-release.json','market_view_v42')=='{NEW_MARKET}'"
    )
    run('python3', '-c', verify_code)

    run('git', 'config', 'user.name', 'qrgf-market-data-bot')
    run('git', 'config', 'user.email', 'qrgf-market-data-bot@users.noreply.github.com')
    run('git', 'add', *sorted(EXPECTED))
    run('git', 'commit', '-m', 'Deploy QRGF V4.2.4 rounded MetricDuck projection contract fix')
    run('git', 'push', '--force-with-lease', 'origin', f'HEAD:{READY_BRANCH}')

    print('\nSUCCESS')
    print(f'Ready branch: {READY_BRANCH}')
    print('Changed files: 9/9 expected')
    print('data/** changed: 0')
    print('Next: return to ChatGPT; it will inspect, merge, and read back main.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
