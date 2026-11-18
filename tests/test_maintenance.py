import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from module.device.game_update import update_game
from module.exception import RequestHumanTakeover
from module.webui.inactive_alert import InactiveMonitor


class InactiveAlertTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.send = Mock(return_value=True)
        self.monitor = InactiveMonitor(self.path, self.send)
        self.data = {'Alas': {'Error': dict(NotRunningAlert=True,
            NotRunningGraceMinutes=30,
            OnePushConfig='provider: telegram')}}

    def test_grace_repeat_and_recovery(self):
        self.monitor.check_one('src', self.data, 2, 100)
        self.monitor.check_one('src', self.data, 2, 1899)
        self.send.assert_not_called()
        self.monitor.check_one('src', self.data, 2, 1900)
        self.assertEqual(self.send.call_count, 1)
        # An inactive profile may change its reported reason across a GUI restart.
        self.monitor.check_one('src', self.data, 3, 9100)
        self.monitor.check_one('src', self.data, 2, 1900 + 86400 - 1)
        self.assertEqual(self.send.call_count, 1)
        self.monitor.check_one('src', self.data, 2, 1900 + 86400)
        self.assertEqual(self.send.call_count, 2)
        self.monitor.check_one('src', self.data, 1, 88400)
        self.assertNotIn('src', self.monitor.records)
        self.monitor.check_one('src', self.data, 3, 88500)
        self.monitor.check_one('src', self.data, 3, 90300)
        self.assertIn('异常停止', self.send.call_args.kwargs['content'])

    def test_opt_out_and_update_are_quiet(self):
        for state in (1, 2, 3, 4):
            self.monitor.check_one('off', {'Alas': {'Error': {}}}, state, 10000)
        self.monitor.check_one('updating', self.data, 4, 10000)
        self.send.assert_not_called()

    def test_only_selected_disabled_tasks(self):
        self.data['Rogue'] = {'Scheduler': {'Enable': False}}
        self.monitor.check_one('src', self.data, 1, 10)
        self.assertFalse(self.monitor.records)
        self.data['Alas']['Error']['NotRunningTasks'] = 'Rogue, DailyQuest'
        self.monitor.check_one('src', self.data, 1, 20)
        self.monitor.check_one('src', self.data, 1, 1820)
        self.assertIn('Rogue 已禁用', self.send.call_args.kwargs['content'])
        self.data['Rogue']['Scheduler']['Enable'] = True
        self.monitor.check_one('src', self.data, 1, 2000)
        self.assertFalse(self.monitor.records)

    def test_failed_delivery_retries_and_persists(self):
        self.send.return_value = False
        self.monitor.check_one('src', self.data, 2, 100)
        self.monitor.check_one('src', self.data, 2, 1900)
        self.monitor.check_one('src', self.data, 2, 1960)
        self.assertEqual(self.send.call_count, 1)
        self.path.write_text(json.dumps(self.monitor.records))
        restored = InactiveMonitor(self.path, self.send)
        self.send.return_value = True
        restored.check_one('src', self.data, 2, 2200)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(restored.records['src']['last_sent'], 2200)


class ProfileDiscoveryTest(unittest.TestCase):
    def test_monitor_files_are_not_discovered_as_profiles(self):
        from module.config.utils import alas_instance
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            try:
                os.chdir(tmp)
                Path('config').mkdir()
                for name in ('src.json', 'template.json', 'inactive.alerts.json', 'maintenance.status.json'):
                    Path('config', name).write_text('{}')
                self.assertEqual(alas_instance(), ['src'])
            finally:
                os.chdir(old)


class GameUpdateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, old)
        self.device = SimpleNamespace(serial='fake-device', package='com.miHoYo.hkrpg',
            adb_binary='/fake/adb', config=SimpleNamespace(Emulator_AutoUpdateGame=True,
            Emulator_GameUpdateCheckMinutes=360, is_cloud_game=False))

    def test_disabled_and_other_channels_never_run(self):
        with patch('module.device.game_update.subprocess.run') as run:
            self.device.config.Emulator_AutoUpdateGame = False
            update_game(self.device)
            self.device.config.Emulator_AutoUpdateGame = True
            self.device.package = 'com.miHoYo.hkrpg.bilibili'
            update_game(self.device)
            self.device.package = 'com.miHoYo.hkrpg'
            self.device.config.is_cloud_game = True
            update_game(self.device)
            run.assert_not_called()

    def test_success_cached_per_device(self):
        with patch('module.device.game_update.sys.platform', 'linux'), \
             patch('module.device.game_update.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            update_game(self.device)
            update_game(self.device)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs['env']['HSR_ADB'], '/fake/adb')
            self.device.serial = 'other-device'
            update_game(self.device)
            self.assertEqual(run.call_count, 2)

    def test_failure_is_not_cached_or_followed_by_game_actions(self):
        with patch('module.device.game_update.sys.platform', 'linux'), \
             patch('module.device.game_update.subprocess.run', return_value=SimpleNamespace(returncode=1)) as run:
            for _ in range(2):
                with self.assertRaises(RequestHumanTakeover):
                    update_game(self.device)
            self.assertEqual(run.call_count, 2)
            self.assertFalse(list(Path('cache').rglob('*.checked')))

    def test_timeout_requires_attention(self):
        with patch('module.device.game_update.sys.platform', 'linux'), \
             patch('module.device.game_update.subprocess.run', side_effect=subprocess.TimeoutExpired('bash', 7200)):
            with self.assertRaises(RequestHumanTakeover):
                update_game(self.device)


class ApkScriptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.version = self.base / 'version'
        self.version.write_text('1.0.0')
        self.apk = self.base / 'release.apk'
        with zipfile.ZipFile(self.apk, 'w') as archive:
            archive.writestr('AndroidManifest.xml', 'fake manifest for offline test')
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
            HSR_ADB=str(self.bin/'adb'), HSR_BASE=str(self.base/'game'), ASSUME_YES='1',
            TEST_VERSION=str(self.version), TEST_APK=str(self.apk), TEST_INSTALL_LOG=str(self.base/'installed'))
        self.script = Path(__file__).resolve().parents[1] / 'deploy/Linux/hsr-update.sh'
        self.tool('adb', '''#!/usr/bin/env python3
import os,sys,pathlib
args=sys.argv[1:]
if 'get-state' in args: print('device')
elif 'dumpsys' in args: print('versionName='+pathlib.Path(os.environ['TEST_VERSION']).read_text())
elif 'pidof' in args: sys.exit(1)
elif 'install' in args:
 pathlib.Path(os.environ['TEST_INSTALL_LOG']).write_text(' '.join(args))
 if os.environ.get('TEST_REJECT'): print('Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]');sys.exit(1)
 pathlib.Path(os.environ['TEST_VERSION']).write_text('2.0.0');print('Success')
''')
        self.tool('curl', '''#!/usr/bin/env python3
import os,sys,pathlib,json
args=sys.argv[1:]
if '%{redirect_url}' in args: print('https://autopatchcn.bhsr.com/client/2.0.0/StarRail_2.0.0.apk',end='')
elif '0-0' in args: print('Content-Range: bytes 0-0/'+str(pathlib.Path(os.environ['TEST_APK']).stat().st_size))
else: print(json.dumps({'data':{'game_branches':[]}}))
''')
        self.tool('aria2c', '''#!/usr/bin/env python3
import os,sys,pathlib,shutil
args=sys.argv[1:];dest=pathlib.Path(args[args.index('-d')+1])/args[args.index('-o')+1]
shutil.copyfile(os.environ['TEST_APK'],dest)
''')

    def tool(self, name, content):
        p = self.bin/name
        p.write_text(content)
        p.chmod(0o755)

    def run_update(self):
        return subprocess.run(['bash', str(self.script), 'update'], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def test_upgrade_verifies_readback_and_archives(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(self.version.read_text(), '2.0.0')
        self.assertTrue(list((self.base/'game/apk-archive').glob('*.apk')))
        self.assertNotIn(' -d ', (self.base/'installed').read_text())

    def test_equal_or_newer_version_never_installs(self):
        for version in ('2.0.0', '3.0.0'):
            self.version.write_text(version)
            result = self.run_update()
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertFalse((self.base/'installed').exists())

    def test_invalid_download_never_installs(self):
        self.apk.write_bytes(b'not a zip')
        self.assertNotEqual(self.run_update().returncode, 0)
        self.assertFalse((self.base/'installed').exists())

    def test_android_signature_rejection_keeps_existing_version(self):
        self.env['TEST_REJECT'] = '1'
        self.assertNotEqual(self.run_update().returncode, 0)
        self.assertEqual(self.version.read_text(), '1.0.0')


if __name__ == '__main__':
    unittest.main()
