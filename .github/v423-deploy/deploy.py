#!/usr/bin/env python3
from __future__ import annotations

import base64
import gzip
import hashlib
import os
import subprocess
import sys
from pathlib import Path

REPO = Path.cwd()
SOURCE_BRANCH = 'fix/v423-metricduck-returned-sector-taxonomy'
READY_BRANCH = 'fix/v423-metricduck-returned-sector-taxonomy-ready'
PATCH_REL = Path('.github/v423-deploy/patch-01.b64')
OLD_STATE = 'dec23757f67f9a5e948e7495d62e2fa136609b772fc5013c7a687d9924246070'
OLD_MARKET = 'dd20c547ace0f140ffaa29ddb1e1e84cca4eb250e6d7d859fd92875a8b36d1a3'
NEW_STATE = '7397afec8c0202742d31965a259f495c2875b26c137c2641030f7bb971ec74bd'
NEW_MARKET = '70c0686389653bb496f7f7f15fce506d90019b8622c12a6d20a3420bff0319eb'
PATCH_B64_SHA = '8ec509fcfb799086a254302eab4e0630045e2617b6afe9757780c9231b7d8ea5'
PATCH_SHA = '52eb666cc64ba79ab6b0158a191d8070e789cd225ff945a94f02d1338009ec45'
EXPECTED = {
    'screening/config/v42-market-producer-release.json': '70c0686389653bb496f7f7f15fce506d90019b8622c12a6d20a3420bff0319eb',
    'screening/config/v42-state-producer-release.json': '7397afec8c0202742d31965a259f495c2875b26c137c2641030f7bb971ec74bd',
    'screening/engine/v42_runtime.py': 'b772d71f502d4b404cb00a650b05cee1ee9016a8de86a56d6e101be2df3e3d95',
    'screening/qrgf_v42/config/connectors.json': '2da0519062b4260f2f9cb698699e6a05399e0bef0f1cea7c2c970acd5196e8ed',
    'screening/qrgf_v42/config/policy.json': 'fc7fe3ea4bd7969d1486ea7a12a40a2d1367d61c40cfec3ecb9027dbe1e4c70f',
    'screening/qrgf_v42/scripts/bootstrap.py': 'fcbea5037246249623e88543416c6c31cfb77b6d600b6280f9557cdd86afdab8',
    'screening/qrgf_v42/scripts/integrity.py': 'e2d55cca05b97e8e891deb05be3d5af27d64640df49db81452a154b64ef85697',
    'screening/qrgf_v42/scripts/policy.py': '9d52c79e30e6c89e7c7a05b3876fd260ba2d2cab446e9b97c4235378cfeb8707',
    'screening/qrgf_v42/scripts/provenance.py': '4a0cde0c64324b9041bac71cdb002ac75d75bcdb1eb225613cdefa92caf7b420',
    'screening/qrgf_v42/tests/run_tests.py': '4c324f803210135fb4338d30fe626f11dbded779b51b7fc0efffcfbb56989df4',
}


def run(*args: str, capture: bool = False) -> str:
    print('+', ' '.join(args), flush=True)
    p = subprocess.run(args, cwd=REPO, text=True, check=True,
                       capture_output=capture)
    return p.stdout.strip() if capture else ''


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fail(msg: str) -> None:
    print(f'\nFAILED: {msg}', file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    if not (REPO / '.git').exists():
        fail('Run this command from the repository root in GitHub Codespaces.')
    patch_path = REPO / PATCH_REL
    if not patch_path.is_file():
        fail(f'Missing {PATCH_REL}. Open Codespace from branch {SOURCE_BRANCH}.')
    patch_b64 = patch_path.read_bytes()
    if hashlib.sha256(patch_b64).hexdigest() != PATCH_B64_SHA:
        fail('Staged patch hash mismatch.')
    try:
        patch = gzip.decompress(base64.b64decode(patch_b64))
    except Exception as exc:
        fail(f'Cannot decode staged patch: {exc}')
    if hashlib.sha256(patch).hexdigest() != PATCH_SHA:
        fail('Decoded patch hash mismatch.')
    tmp = Path('/tmp/qrgf-v423.patch')
    tmp.write_bytes(patch)

    run('git', 'fetch', '--prune', 'origin')
    run('git', 'reset', '--hard')
    run('git', 'clean', '-fd')
    run('git', 'checkout', '-B', READY_BRANCH, 'origin/main')

    if sha(REPO / 'screening/config/v42-state-producer-release.json') != OLD_STATE:
        fail('Remote main is no longer on the expected V4.2.2 state release. Stop and tell ChatGPT.')
    if sha(REPO / 'screening/config/v42-market-producer-release.json') != OLD_MARKET:
        fail('Remote main is no longer on the expected V4.2.2 market release. Stop and tell ChatGPT.')

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
        fail(f'data/v42 mutation is forbidden: {data_changes}')

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
    run('git', 'commit', '-m', 'Deploy QRGF V4.2.3 MetricDuck sector taxonomy contract fix')
    run('git', 'push', '--force-with-lease', 'origin', f'HEAD:{READY_BRANCH}')

    print('\nSUCCESS')
    print(f'Ready branch: {READY_BRANCH}')
    print('Changed files: 10/10 expected')
    print('data/** changed: 0')
    print('Next: return to ChatGPT; it will inspect, merge, and read back main.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
