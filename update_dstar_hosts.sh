#!/usr/bin/env bash
# Refresh ircDDBGateway host tables with pre-resolved Pi-Star IPv4 addresses.
set -euo pipefail

BASE_URL="https://www.pistar.uk/downloads"
DEST_DIR="/usr/share/ircddbgateway"
RESTART=1
FILES=(DExtra_Hosts.txt DPlus_Hosts.txt DCS_Hosts.txt)
PREFIXES=(XRF REF DCS)

die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
usage() { echo "Usage: $0 [--no-restart] [--base-url URL] [--dest-dir DIRECTORY]"; }
while (( $# )); do
  case "$1" in
    --no-restart) RESTART=0; shift ;;
    --base-url|--dest-dir)
      (( $# >= 2 )) && [[ -n "$2" ]] || die "$1 requires a value"
      if [[ "$1" == "--base-url" ]]; then BASE_URL="$2"; else DEST_DIR="$2"; fi
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "Unknown argument: $1" ;;
  esac
done
[[ "$BASE_URL" == https://* || "$BASE_URL" == http://* ]] || die "URL must use HTTP or HTTPS"
for command in curl awk flock mktemp; do
  command -v "$command" >/dev/null || die "Required command not found: $command"
done
mkdir -p -- "$DEST_DIR"
exec 9>"$DEST_DIR/.opendvm-dstar.lock"
flock -n 9 || die "Another D-Star host update is already running"

for file in "${FILES[@]}"; do
  [[ ! -d "$DEST_DIR/$file" && ! -d "$DEST_DIR/$file.bak" ]] || die "Host and backup paths must be files: $file"
done
STAGING="$(mktemp -d "$DEST_DIR/.opendvm-dstar.XXXXXX")"
trap 'rm -rf -- "$STAGING"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Validate every download before changing any active host file. Names can be
# reflectors (including alphanumeric IDs), XLX aliases, or D-Plus repeater calls.
for index in "${!FILES[@]}"; do
  file="${FILES[$index]}"
  curl --fail --location --silent --show-error --connect-timeout 10 --max-time 30 \
    --output "$STAGING/$file" -- "${BASE_URL%/}/$file" || die "Download failed: $file; existing host lists preserved"
  count="$(awk -v prefix="${PREFIXES[$index]}" '
    function ipv4(ip, octets, n, i) {
      n = split(ip, octets, ".")
      if (n != 4 || ip == "0.0.0.0" || ip == "255.255.255.255") return 0
      for (i=1; i<=4; i++) {
        if (octets[i] !~ /^[0-9]+$/ || octets[i] + 0 > 255 ||
            (length(octets[i]) > 1 && substr(octets[i],1,1) == "0")) return 0
      }
      return 1
    }
    { sub(/\r$/, "") }
    /^[[:space:]]*(#|$)/ { next }
    {
      if (NF < 2 || NF > 3 || $1 !~ /^[A-Z0-9]+$/ || length($1) < 3 || length($1) > 8 ||
          !ipv4($2) || (NF == 3 && $3 != "L")) invalid = 1
      else { count++; if (substr($1,1,3) == prefix) native++ }
    }
    END { if (invalid || native < 100) exit 1; print count }
  ' "$STAGING/$file")" || die "Invalid IPv4 host list or fewer than 100 ${PREFIXES[$index]} entries: $file; existing host lists preserved"
  echo "Validated $file: $count IPv4 records"
done

CHANGED=0
for file in "${FILES[@]}"; do
  if [[ -f "$DEST_DIR/$file" ]] && cmp -s -- "$STAGING/$file" "$DEST_DIR/$file"; then
    chmod 644 "$DEST_DIR/$file"
    continue
  fi
  if [[ -e "$DEST_DIR/$file" ]]; then
    cp -p -- "$DEST_DIR/$file" "$STAGING/$file.bak"
    mv -f -- "$STAGING/$file.bak" "$DEST_DIR/$file.bak"
  fi
  chmod 644 "$STAGING/$file"
  mv -f -- "$STAGING/$file" "$DEST_DIR/$file"
  CHANGED=1
done
if (( CHANGED && RESTART )); then
  systemctl restart ircddbgateway.service || die "Host lists updated, but restarting ircddbgateway failed"
fi

# Per-user lists are also read before the system lists. Preserve custom entries,
# but point out that hostnames in those files can still delay startup.
if command -v getent >/dev/null; then
  DSTAR_HOME="$(getent passwd mmdvm | cut -d: -f6 || true)"
  if [[ -n "$DSTAR_HOME" && "$DSTAR_HOME" != "$DEST_DIR" ]]; then
    for file in "${FILES[@]}"; do
      if [[ -r "$DSTAR_HOME/$file" ]]; then
        printf '[WARN] Additional per-user list %s is also loaded; check it for stale hostnames.\n' "$DSTAR_HOME/$file" >&2
      fi
    done
  fi
fi
if (( CHANGED )); then echo "D-Star host lists updated: $DEST_DIR";
else echo "D-Star host lists unchanged; restart skipped."; fi
awk '$1 == "XRF055" { print }' "$DEST_DIR/DExtra_Hosts.txt"
