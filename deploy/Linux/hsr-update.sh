#!/usr/bin/env bash
# hsr-update.sh — 崩坏：星穹铁道 (CN) updater for redroid.
#
# Fully automatic: resolves the official APK via miHoYo's download_porter
# endpoint, downloads it resumably, verifies it, and installs over adb.

set -uo pipefail

PKG="com.miHoYo.hkrpg"
SERIAL="${HSR_SERIAL:-127.0.0.1:5555}"
BASE="${HSR_BASE:-$HOME/android}"
ADB="${HSR_ADB:-adb}"
INBOX="$BASE/apk-inbox"
ARCHIVE="$BASE/apk-archive"
LOG="$BASE/hsr-update.log"

# Official APK redirector. 302s to the current CN Android build on autopatchcn.bhsr.com.
PORTER="https://act-api-takumi.mihoyo.com/event/download_porter/link/hkrpg_cn/official/android_default"
# Cross-check source for the live version tag.
BRANCH_API="https://hyp-api.mihoyo.com/hyp/hyp-connect/api/getGameBranches?launcher_id=jGHBHlcOq1"
BIZ="hkrpg_cn"

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; BLU=$'\e[34m'; DIM=$'\e[2m'; RST=$'\e[0m'

log()  {
  if [[ -f "$LOG" && $(stat -c %s "$LOG") -ge 1048576 ]]; then
    mv -f "$LOG.2" "$LOG.3" 2>/dev/null || :
    mv -f "$LOG.1" "$LOG.2" 2>/dev/null || :
    mv -f "$LOG" "$LOG.1"
  fi
  printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }
say()  { printf '%s\n' "$*"; }
info() { printf '%s==>%s %s\n' "$BLU" "$RST" "$*"; }
ok()   { printf '%s ok %s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s warn%s %s\n' "$YEL" "$RST" "$*"; }
die()  { printf '%sfail%s %s\n' "$RED" "$RST" "$*" >&2; log "FAIL: $*"; exit 1; }

need() { command -v "$1" >/dev/null || die "missing required tool: $1"; }

human() { awk -v b="${1:-0}" 'BEGIN{ if(b<1024){print b" B"} else if(b<1048576){printf "%.1f KiB",b/1024} else if(b<1073741824){printf "%.1f MiB",b/1048576} else {printf "%.2f GiB",b/1073741824} }'; }

adb_connect() {
  need "$ADB"
  if ! "$ADB" -s "$SERIAL" get-state >/dev/null 2>&1; then
    "$ADB" connect "$SERIAL" >/dev/null 2>&1; sleep 1
  fi
  "$ADB" -s "$SERIAL" get-state >/dev/null 2>&1 \
    || die "cannot reach device $SERIAL (is the redroid container running?)"
}

installed_version() {
  "$ADB" -s "$SERIAL" shell dumpsys package "$PKG" 2>/dev/null \
    | awk -F= '/versionName=/{print $2; exit}' | tr -d '\r'
}
installed_code() {
  "$ADB" -s "$SERIAL" shell dumpsys package "$PKG" 2>/dev/null \
    | awk '/versionCode=/{for(i=1;i<=NF;i++) if($i ~ /^versionCode=/){split($i,a,"="); print a[2]; exit}}' | tr -d '\r'
}

branch_version() {
  need curl; need jq
  curl -fsS -m 25 "$BRANCH_API" 2>/dev/null \
    | jq -r --arg biz "$BIZ" '.data.game_branches[]? | select(.game.biz==$biz) | .main.tag' 2>/dev/null
}

# Resolve the porter redirect. Sets APK_URL / APK_VER / APK_NAME / APK_SIZE.
resolve_apk() {
  need curl
  APK_URL=$(curl -s -m 25 -o /dev/null -w '%{redirect_url}' "$PORTER")
  [[ "$APK_URL" == https://*.apk ]] || die "porter did not return an APK URL (got: ${APK_URL:-empty})"
  case "$APK_URL" in
    https://autopatchcn.bhsr.com/*|https://autopatchos.starrails.com/*) : ;;
    *) die "porter redirected to an unexpected host: $APK_URL" ;;
  esac
  APK_NAME="${APK_URL##*/}"
  APK_VER=$(sed -nE 's#.*/client/([0-9]+(\.[0-9]+)+)/.*#\1#p' <<<"$APK_URL")
  [[ -n "$APK_VER" ]] || APK_VER=$(sed -nE 's#.*StarRail_([0-9]+(\.[0-9]+)+)\.apk#\1#p' <<<"$APK_NAME")
  # HEAD is 405 on this CDN; a 1-byte ranged GET yields the total via content-range.
  APK_SIZE=$(curl -s -m 25 -r 0-0 -D - -o /dev/null "$APK_URL" \
             | awk 'tolower($0) ~ /^content-range:/{n=split($0,a,"/"); gsub(/[\r\n ]/,"",a[n]); print a[n]}')
}

# Compare the APK's signing certificate against the installed package's.
# Android reports SHA-256 of the raw X.509 DER; we compute the same and diff.
# Returns 0 match, 1 mismatch, 2 undeterminable (not installed / no v1 block).
verify_signature() {
  local apk="$1"
  command -v openssl >/dev/null || return 2
  local dump; dump=$("$ADB" -s "$SERIAL" shell dumpsys package "$PKG" 2>/dev/null)
  local dev; dev=$(printf '%s\n' "$dump" | sed -n 's/.*Signatures: \[\([^]]*\)\].*/\1/p' \
    | sed -n 1p | tr -d ': \r')
  [[ -n "$dev" ]] || return 2
  local names; names=$(unzip -Z1 "$apk" 2>/dev/null)
  local cert; cert=$(printf '%s\n' "$names" | grep -iE '^META-INF/.*\.(RSA|DSA|EC)$' | sed -n 1p)
  [[ -n "$cert" ]] || return 2
  local sig; sig=$(unzip -p "$apk" "$cert" 2>/dev/null \
    | openssl pkcs7 -inform DER -print_certs -outform PEM 2>/dev/null \
    | openssl x509 -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1 | tr 'a-z' 'A-Z')
  [[ -n "$sig" ]] || return 2
  SIG_APK="$sig"; SIG_DEV="$dev"
  [[ "$sig" == "$dev" ]]
}

find_apk() {
  find "$INBOX" -maxdepth 1 -type f -name '*.apk' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | head -1 | cut -d' ' -f2-
}

cmd_status() {
  mkdir -p "$INBOX" "$ARCHIVE"
  adb_connect
  local inst code branch; inst=$(installed_version); code=$(installed_code); branch=$(branch_version)
  resolve_apk

  say ""
  say "  device     ${DIM}$SERIAL${RST}"
  say "  package    ${DIM}$PKG${RST}"
  say "  installed  ${inst:-${RED}not installed${RST}}${code:+ ${DIM}(versionCode $code)${RST}}"
  say "  available  ${APK_VER:-?}  ${DIM}$(human "${APK_SIZE:-0}") — $APK_NAME${RST}"
  say "  branch tag ${branch:-${YEL}unknown${RST}}"
  local apk; apk=$(find_apk)
  [[ -n "$apk" ]] && say "  inbox      $(basename "$apk") ${DIM}($(du -h "$apk" | cut -f1))${RST}"
  say ""

  if [[ -n "$branch" && -n "$APK_VER" && "$branch" != "$APK_VER" ]]; then
    warn "porter APK ($APK_VER) differs from branch tag ($branch) — APK may lag a fresh release"
  fi
  if [[ -n "$inst" && -n "$APK_VER" ]]; then
    if [[ "$inst" == "$APK_VER" ]]; then ok "up to date"; return 0; fi
    local newest; newest=$(printf '%s\n%s\n' "$inst" "$APK_VER" | sort -V | tail -1)
    if [[ "$newest" == "$APK_VER" ]]; then
      warn "update available: $inst -> $APK_VER"
      say "     ${DIM}run: $0 update${RST}"
      return 10
    fi
    ok "installed ($inst) is newer than the published APK ($APK_VER)"
  fi
  return 0
}

cmd_download() {
  mkdir -p "$INBOX" "$ARCHIVE"
  resolve_apk
  local dest="$INBOX/$APK_NAME"

  if [[ -f "$dest" ]]; then
    local have; have=$(stat -c %s "$dest")
    if [[ -n "$APK_SIZE" && "$have" == "$APK_SIZE" ]]; then
      ok "already downloaded: $APK_NAME ($(human "$have"))"; DL_PATH="$dest"; return 0
    fi
    info "resuming partial download ($(human "$have") of $(human "${APK_SIZE:-0}"))"
  fi

  info "downloading $APK_NAME  $(human "${APK_SIZE:-0}")"
  say "  ${DIM}$APK_URL${RST}"
  log "download start: $APK_URL"

  if command -v aria2c >/dev/null; then
    aria2c -x8 -s8 -k4M -c --console-log-level=warn --summary-interval=0 \
           -d "$INBOX" -o "$APK_NAME" "$APK_URL" \
      || die "download failed (aria2c). Re-run to resume."
  else
    curl -fL -C - --retry 3 -o "$dest" "$APK_URL" \
      || die "download failed (curl). Re-run to resume."
  fi

  local got; got=$(stat -c %s "$dest" 2>/dev/null || echo 0)
  if [[ -n "$APK_SIZE" && "$got" != "$APK_SIZE" ]]; then
    die "size mismatch: got $got bytes, expected $APK_SIZE. File left at $dest — re-run to resume."
  fi
  [[ "$(head -c4 "$dest" | xxd -p)" == "504b0304" ]] \
    || die "downloaded file is not a ZIP/APK (bad magic bytes): $dest"

  ok "downloaded and verified: $APK_NAME ($(human "$got"))"
  log "download OK: $APK_NAME ($got bytes)"
  DL_PATH="$dest"
}

do_install() {
  local apk="$1"
  adb_connect
  need unzip
  info "validating $(basename "$apk")"
  local listing; listing=$(unzip -Z1 "$apk" 2>/dev/null)
  [[ "$listing" == *AndroidManifest.xml* ]] \
    || die "not a valid APK (no AndroidManifest.xml): $apk"
  ok "APK structure valid ($(du -h "$apk" | cut -f1))"

  local before; before=$(installed_version)
  say "  installed before: ${before:-none}"

  verify_signature "$apk"; local vs=$?
  case $vs in
    0) ok "signing key matches the installed package ${DIM}(${SIG_APK:0:16}...)${RST}" ;;
    1) die "SIGNATURE MISMATCH — refusing to install.
     apk      : $SIG_APK
     installed: $SIG_DEV
     This is not the same publisher key as the installed game." ;;
    2) warn "could not pre-verify signature; relying on adb's own check" ;;
  esac

  if [[ "${ASSUME_YES:-0}" != "1" ]]; then
    printf '\n  Install %s onto %s? [y/N] ' "$(basename "$apk")" "$SERIAL"
    read -r reply </dev/tty
    [[ "$reply" =~ ^[Yy]$ ]] || { say "aborted."; exit 0; }
  fi

  if "$ADB" -s "$SERIAL" shell pidof "$PKG" >/dev/null 2>&1; then
    info "game is running — force-stopping"
    "$ADB" -s "$SERIAL" shell am force-stop "$PKG"
  fi

  info "installing (copies the APK into the container; takes a minute)"
  log "install start: $apk (installed=$before)"
  local out rc
  out=$("$ADB" -s "$SERIAL" install -r "$apk" 2>&1); rc=$?
  say "$DIM$out$RST"

  if [[ $rc -ne 0 || "$out" == *Failure* ]]; then
    log "install FAILED: $out"
    case "$out" in
      *INSTALL_FAILED_UPDATE_INCOMPATIBLE*|*INSTALL_FAILED_SHARED_USER_INCOMPATIBLE*)
        die "signature mismatch — this APK is signed by a different key than the
     installed game, so it is not the same official build. Not forcing it:
     uninstalling to sideload would risk your local data." ;;
      *INSTALL_FAILED_VERSION_DOWNGRADE*) die "that APK is older than what is installed ($before)." ;;
      *INSTALL_FAILED_INSUFFICIENT_STORAGE*) die "not enough space in the container." ;;
      *) die "install failed (exit $rc)." ;;
    esac
  fi

  local after; after=$(installed_version)
  [[ "$after" == "$APK_VER" ]] || die "installed version ${after:-unknown} does not match $APK_VER"
  ok "installed: ${before:-none} -> ${after:-unknown}"
  log "install OK: ${before:-none} -> ${after:-unknown} ($(basename "$apk"))"
  mv -- "$apk" "$ARCHIVE/" && say "  ${DIM}archived to $ARCHIVE/$(basename "$apk")${RST}"
}

cmd_install() {
  mkdir -p "$INBOX" "$ARCHIVE"
  local apk; apk=$(find_apk)
  [[ -n "$apk" ]] || die "no .apk in $INBOX — run '$0 download' or '$0 update'"
  resolve_apk
  [[ "${apk##*/}" == "$APK_NAME" ]] || die "inbox APK is not the current official release"
  do_install "$apk"
}

cmd_update() {
  adb_connect
  local inst; inst=$(installed_version)
  resolve_apk
  if [[ -n "$inst" && "$inst" == "$APK_VER" ]]; then
    ok "already up to date ($inst)"; return 0
  fi
  [[ -n "$inst" && -n "$APK_VER" ]] || die "cannot determine installed or official version"
  local newest; newest=$(printf '%s\n%s\n' "$inst" "$APK_VER" | sort -V | tail -1)
  if [[ "$newest" != "$APK_VER" ]]; then
    ok "installed ($inst) is newer than official APK ($APK_VER), skip"; return 0
  fi
  say "  ${inst:-none} -> ${APK_VER:-?}"
  DL_PATH=""
  cmd_download
  [[ -n "$DL_PATH" ]] || die "download produced no file"
  do_install "$DL_PATH"
}

cmd_help() {
  cat <<EOF
hsr-update.sh — 崩坏：星穹铁道 (CN) updater for redroid

USAGE
  $0 status      Installed vs available version (default)
  $0 download    Fetch the official APK into the inbox (resumable)
  $0 install     Install the newest APK already in the inbox
  $0 update      download + install in one step
  $0 help        This text

PATHS
  inbox    $INBOX
  archive  $ARCHIVE
  log      $LOG

ENV
  HSR_SERIAL   adb serial   (default 127.0.0.1:5555)
  HSR_BASE     base dir     (default \$HOME/android)
  ASSUME_YES=1 skip the install prompt

SOURCES
  APK       $PORTER
            302s to the current official build on autopatchcn.bhsr.com. The
            script refuses any redirect that leaves miHoYo's CDN.
  Version   getGameBranches .main.tag, used as a cross-check. Note that
            getGamePackages .main.major.version is NOT reliable — it tracks
            full-package rebuilds and lagged the live version by a release.

  Integrity: exact content-length match + ZIP magic bytes, then adb's own
  signature check against the installed package, which is the real guarantee
  that the build is genuinely miHoYo's.
EOF
}

case "${1:-status}" in
  status|-s|--status)     cmd_status ;;
  download|-d|--download) cmd_download ;;
  install|-i|--install)   cmd_install ;;
  update|-u|--update)     cmd_update ;;
  help|-h|--help)         cmd_help ;;
  *) die "unknown command: $1 (try: $0 help)" ;;
esac
