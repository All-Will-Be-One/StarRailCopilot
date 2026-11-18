"""Optional official CN Android APK update before starting the game (Linux)."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

from filelock import FileLock

from module.exception import RequestHumanTakeover
from module.logger import logger


def update_game(device):
    if not device.config.Emulator_AutoUpdateGame:
        return
    if device.config.is_cloud_game or device.package != 'com.miHoYo.hkrpg':
        logger.info('Automatic APK update: this game channel is not supported, skip')
        return
    if not sys.platform.startswith('linux'):
        logger.error('Automatic APK update requires Linux and bash; disable AutoUpdateGame on this host')
        raise RequestHumanTakeover

    # Shared per device, so multiple profiles cannot install concurrently.
    base = Path('cache/game-update').resolve()
    base.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(device.serial.encode()).hexdigest()[:16]
    stamp = base / (key + '.checked')
    interval = max(0, int(device.config.Emulator_GameUpdateCheckMinutes)) * 60
    with FileLock(str(base / (key + '.lock')), timeout=7200):
        if stamp.exists() and time.time() - stamp.stat().st_mtime < interval:
            logger.info('Automatic APK update: recent successful check, skip')
            return
        env = dict(os.environ, HSR_SERIAL=device.serial, HSR_BASE=str(base / key),
                   HSR_ADB=device.adb_binary, ASSUME_YES='1')
        script = Path(__file__).resolve().parents[2] / 'deploy/Linux/hsr-update.sh'
        started = time.monotonic()
        logger.info('Automatic APK update: checking official CN release')
        try:
            result = subprocess.run(['bash', str(script), 'update'], env=env,
                                    stdin=subprocess.DEVNULL, timeout=7200)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error(f'Automatic APK update failed: {type(exc).__name__}')
            raise RequestHumanTakeover from None
        logger.info(f'Automatic APK update: exit={result.returncode}, elapsed={time.monotonic()-started:.1f}s')
        if result.returncode:
            raise RequestHumanTakeover
        stamp.touch()
