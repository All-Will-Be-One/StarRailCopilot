# Maintained SRC fork

Upstream release: `LmeSzinc/StarRailCopilot`, `master`. Development features remain in upstream `dev` until released.
Maintained deployment: `All-Will-Be-One/StarRailCopilot`, `master`.
The existing `feat/relic-full-notification` branch and upstream PR #1066 stay separate; its patch is merged into maintained master.

## Automatic game version updates

In each profile's Emulator settings, enable **AutoUpdateGame** (default off), and set
**GameUpdateCheckMinutes** (default 360; 0 checks on every launch).
Before the next game launch, Linux runs `deploy/Linux/hsr-update.sh update` using the selected device's ADB binary and serial.
The updater uses the official miHoYo redirect, resumable download,
length/ZIP validation, Android signing-key verification and installed version readback. It refuses downgrades.
It supports official CN Android (`com.miHoYo.hkrpg`); cloud/other channels skip with a log entry.
Failure stops the launch and uses SRC's existing error notification. No uninstallation, data deletion or automatic salvage.
The main game resources are still downloaded by the normal login flow.

APK cache and archived APKs: `cache/game-update/<device-hash>/`; successful check timestamps are shared per device.
Update log: `hsr-update.log`, rotated at 1 MiB with 3 previous files. APK archives are retained for manual rollback.
The script can also be run manually with `HSR_SERIAL`, `HSR_ADB`, `HSR_BASE` and `ASSUME_YES=1`.
`status` only inspects versions; `update` installs a newer official APK.

## Reminders

Each profile's Error settings expose:

- **NotRunningAlert**: opt in to reminders for not-started, manually stopped, or crashed schedulers; default off.
- **NotRunningGraceMinutes**: default 30 minutes before the first reminder.
- **NotRunningRepeatMinutes**: default 1440 minutes (24 hours) between reminders while stopped.
- **NotRunningTasks**: optional comma-separated internal task IDs to watch for disabled schedules, e.g. `Dungeon,DailyQuest`.
  Empty means no task-switch monitoring, so intentionally disabled Rogue tasks remain quiet.

Existing `OnePushConfig` is reused. The GUI checks every minute even with no browser connected.
Recovery clears reminders. Update pauses are skipped. Delivery failure retries after 5 minutes without resetting the original stopped time.
Cooldown/state survives GUI restarts in ignored `config/inactive.alerts.json`.
`config/maintenance.status.json` contains only check time and profile state (1 running, 2 inactive, 3 crash, 4 update).
The GUI must be running; this feature does not detect a completely stopped GUI/system service.
With the initial migration flag, game updates and the src profile reminder are enabled at 360/30/1440 minutes respectively.
No game task is started by monitoring or deployment.

## Check, merge, test, publish

Run the following commands from your maintained checkout with the SRC Python 3.10 environment active.

```sh
git status --short --branch
git fetch origin master
git fetch upstream master
git log --oneline master..upstream/master
git merge-base --is-ancestor upstream/master master # 0 means release already included
# Only when the worktree is clean, on master, and local master matches published origin/master:
git merge --no-edit upstream/master
python -m unittest discover -s tests -v
bash -n deploy/Linux/hsr-update.sh
git diff --check
git push origin master
gh run list --repo All-Will-Be-One/StarRailCopilot --branch master --workflow maintenance-tests.yml
```

Config schema regeneration (Python 3.10 and requirements.txt):
`python -m module.config.config_updater`; inspect generated diff before committing.
Tests mock all Android installation/game operations; never run `src.py`, game task loops, or `--run` as a test.

## Deploy and rollback

Built-in SRC updater checks the maintained fork every 5 minutes and applies it at configured AutoRestartTime.
`global`/`cn` aliases resolve to the maintained fork; upstream CDN packs are disabled for this fork.
For prompt, tested deployment, run `deploy/Linux/deploy-fork.py` on the target Linux host with its SRC Python:

```sh
python deploy/Linux/deploy-fork.py FULL_PUBLISHED_MASTER_SHA
# Initial migration only: append --enable-maintenance to enable requested profile settings.
```

It refuses active game workers, configured startup profiles or an update marker, runs the offline regression suite,
preserves user configs, swaps the checkout at `~/automation/StarRailCopilot`, retains the old checkout as
`StarRailCopilot.previous-<UTC timestamp>`, and rolls back if systemd/HTTP/SHA checks fail.
The existing user service `starrailcopilot` and network/security settings are preserved.
Logs remain linked to the first retained deployment; keep referenced backups while in use.
Verify `git rev-parse HEAD`, `git remote -v`, sanitized deploy Repository/Branch, service active, HTTP 22367 returns 200,
and a fresh `config/maintenance.status.json` snapshot after at least a minute.

To roll back while idle, stop the user service, rename the current checkout to a retained failed/newer path,
rename the recorded previous checkout back to `~/automation/StarRailCopilot`, then start and verify the service.
Never delete the backup or overwrite its configs to roll back. APK rollback is separate and may be refused by Android.

Stop and report: merge conflict, dirty/unexpected checkout, upstream outside released master, failing tests/CI,
fork head changing during deployment, active game workers, update/reload marker, auto-run profile configured,
APK source/integrity/signature/version failure, failed health checks, unavailable SSH or denied authorization.
Do not force-push, reset unrelated work, start game tasks, alter network/security settings, or print OnePush credentials.
