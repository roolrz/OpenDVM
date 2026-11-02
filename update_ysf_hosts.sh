#!/usr/bin/env bash
# Refresh the YSFGateway host list from Pi-Star.
set -euo pipefail

URL="https://www.pistar.uk/downloads/YSF_Hosts.txt"
DEST="/usr/local/etc/YSFHosts.txt"
RESTART=1

die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
usage() {
  echo "Usage: $0 [--no-restart] [--url URL] [--dest FILE]"
  echo "Default: download the Pi-Star list, back up YSFHosts.txt and restart ysfgateway."
}

while (( $# )); do
  case "$1" in
    --no-restart) RESTART=0; shift ;;
    --url|--dest)
      (( $# >= 2 )) && [[ -n "$2" ]] || die "$1 requires a value"
      if [[ "$1" == "--url" ]]; then URL="$2"; else DEST="$2"; fi
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "Unknown argument: $1" ;;
  esac
done

[[ "$URL" == https://* || "$URL" == http://* ]] || die "URL must use HTTP or HTTPS"
[[ ! -d "$DEST" ]] || die "Destination must be a file"
[[ ! -d "${DEST}.bak" ]] || die "Backup destination must be a file"
for command in curl awk flock mktemp; do
  command -v "$command" >/dev/null || die "Required command not found: $command"
done

mkdir -p -- "$(dirname "$DEST")"
# Serialize manual runs and installer runs that target the same file.
exec 9>"${DEST}.lock"
flock -n 9 || die "Another YSF host update is already running"

TMP=""
BACKUP_TMP=""
cleanup() { rm -f -- "$TMP" "$BACKUP_TMP"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Keep temporary files beside the destination so the final rename is atomic.
TMP="$(mktemp "${DEST}.tmp.XXXXXX")"
curl --fail --location --silent --show-error \
  --connect-timeout 10 --max-time 30 \
  --output "$TMP" -- "$URL" || die "Download failed; existing host list preserved"

# Count usable records, not comments or HTML lines. The gateway needs a numeric
# five-digit ID, name, address and valid UDP port in the first five fields.
COUNT="$(awk -F';' '
  { sub(/\r$/, "") }
  /^[[:space:]]*(#|$)/ { next }
  {
    if (NF < 5 || length($1) != 5 || $1 !~ /^[0-9]+$/ ||
        $2 == "" || $4 == "" || $4 ~ /[[:space:]]/ ||
        $5 !~ /^[0-9]+$/ || $5 + 0 < 1 || $5 + 0 > 65535) {
      invalid = 1
    } else {
      count++
    }
  }
  END { if (invalid || count <= 500) exit 1; print count }
' "$TMP")" || die "Invalid host list or fewer than 501 valid records; existing host list preserved"

show_china_reflector() { awk -F';' '$1 == "46004" { print }' "$DEST"; }
if [[ -f "$DEST" ]] && cmp -s -- "$TMP" "$DEST"; then
  echo "YSFHosts unchanged ($COUNT records); restart skipped."
  show_china_reflector
  exit 0
fi

if [[ -e "$DEST" ]]; then
  BACKUP_TMP="$(mktemp "${DEST}.bak.tmp.XXXXXX")"
  cp -p -- "$DEST" "$BACKUP_TMP"
  mv -f -- "$BACKUP_TMP" "${DEST}.bak"
fi
chmod 644 "$TMP"
mv -f -- "$TMP" "$DEST"

if (( RESTART )); then
  systemctl try-restart ysfgateway.service || die "Host list updated, but restarting ysfgateway failed"
fi
echo "YSFHosts updated: $DEST ($COUNT records)"
show_china_reflector
