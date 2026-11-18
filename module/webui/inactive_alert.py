"""Remind opted-in profiles that their scheduler is stopped; never starts a task."""
import json
from pathlib import Path
import time

from module.logger import logger


class InactiveMonitor:
    def __init__(self, state_file='config/inactive.alerts.json', notify=None):
        self.path = Path(state_file)
        try:
            self.records = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.records = {}
        if notify is None:
            from module.notify import handle_notify
            notify = handle_notify
        self.notify = notify

    def check_one(self, name, data, state, now):
        error = data.get('Alas', {}).get('Error', {})
        reason = []
        if error.get('NotRunningAlert', False) and state not in (1, 4):
            reason.append('异常停止' if state == 3 else '未启动或已停止')
        watched = str(error.get('NotRunningTasks', '')).split(',')
        for task in (task.strip() for task in watched):
            scheduler = data.get(task, {}).get('Scheduler', {})
            if task and scheduler.get('Enable') is False:
                reason.append(f'{task} 已禁用')
        if not reason:
            if self.records.pop(name, None) is not None:
                logger.info(f'Inactive reminder: {name} recovered or monitoring disabled')
            return
        reason = '；'.join(reason)
        record = self.records.get(name, {})
        if not record:
            record = dict(reason=reason, since=now, last_sent=None, last_attempt=None)
            self.records[name] = record
            logger.warning(f'Inactive reminder: {name}: {reason}')
        else:
            record['reason'] = reason
        grace = max(0, float(error.get('NotRunningGraceMinutes', 30))) * 60
        repeat = max(1, float(error.get('NotRunningRepeatMinutes', 1440))) * 60
        if now - record['since'] < grace:
            return
        if record['last_sent'] is not None and now - record['last_sent'] < repeat:
            return
        if record['last_attempt'] is not None and now - record['last_attempt'] < min(300, repeat):
            return
        record['last_attempt'] = now
        elapsed = int((now - record['since']) / 60)
        try:
            accepted = self.notify(error.get('OnePushConfig', 'provider: null'),
                                   title=f'SRC <{name}> 未启动／禁用提醒',
                                   content=f'<{name}> {reason}，已持续 {elapsed} 分钟。请检查日志、修复后手动启动。')
        except Exception as exc:
            logger.error(f'Inactive reminder delivery failed: {type(exc).__name__}')
            accepted = False
        if accepted:
            record['last_sent'] = now
        logger.info(f'Inactive reminder: {name} accepted={bool(accepted)}, inactive_minutes={elapsed}')

    def check(self):
        from module.config.utils import alas_instance, filepath_config, read_file
        from module.webui.process_manager import ProcessManager
        from module.webui.updater import updater
        # Updating intentionally stops processes. Do not mistake it for a crash.
        if updater.event is not None and updater.event.is_set():
            return
        names = alas_instance()
        status = {}
        for name in names:
            try:
                state = ProcessManager.get_manager(name).state
                status[name] = state
                self.check_one(name, read_file(filepath_config(name)), state, time.time())
            except Exception as exc:
                logger.error(f'Inactive reminder check failed for {name}: {type(exc).__name__}')
        self.records = {name: item for name, item in self.records.items() if name in names}
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.records, ensure_ascii=False))
        temp.replace(self.path)
        status_path = self.path.with_name('maintenance.status.json')
        temp = status_path.with_suffix('.tmp')
        temp.write_text(json.dumps(dict(checked_at=time.time(), profiles=status)))
        temp.replace(status_path)
