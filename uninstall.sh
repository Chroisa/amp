#!/bin/sh
# uninstall.sh – remove amp MANPAGER settings for the current user
#
# Usage:
#   ./uninstall.sh              # print unset + strip shell rc
#   ./uninstall.sh --session    # only print unset commands
#   ./uninstall.sh --permanent  # only edit ~/.bashrc / ~/.zshrc

set -e
DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
AMP="$DIR/amp"
case "${SHELL:-}" in
  *zsh*) SHELL_RC="$HOME/.zshrc" ;;
  *)     SHELL_RC="$HOME/.bashrc" ;;
esac

usage() {
  cat <<'USAGE'
amp uninstall.sh – clear MANPAGER pointing at this amp

  ./uninstall.sh              session instructions + strip from shell rc
  ./uninstall.sh --session    print unset for current shell only
  ./uninstall.sh --permanent  remove amp MANPAGER lines from shell rc
  ./uninstall.sh -h|--help    this help
USAGE
}

strip_rc() {
  if [ ! -f "$SHELL_RC" ]; then
    echo "No $SHELL_RC to edit"
    return
  fi
  tmp=$(mktemp)
  awk -v amp="$AMP" '
    /^# amp \(animated man page\)$/ { next }
    index($0, amp) && $0 ~ /MANPAGER/ { next }
    { print }
  ' "$SHELL_RC" > "$tmp"
  if cmp -s "$SHELL_RC" "$tmp"; then
    echo "No amp MANPAGER lines found in $SHELL_RC"
    rm -f "$tmp"
  else
    mv "$tmp" "$SHELL_RC"
    echo "Removed amp MANPAGER lines from $SHELL_RC"
    echo "Open a new terminal or:  source $SHELL_RC"
  fi
}

session() {
  cat <<'EOF'
Run in your shell:
  unset MANPAGER
  unset ANIM_A_TIMING ANIM_A_TYPESCRIPT ANIM_A_DIVISOR ANIM_A_LOOP
  unset ANIM_B_TIMING ANIM_B_TYPESCRIPT ANIM_B_DIVISOR ANIM_B_LOOP
  # … and any other ANIM_L_* you set
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --session) session ;;
  --permanent) strip_rc ;;
  "") session; echo; strip_rc ;;
  *) echo "Unknown option: $1"; usage; exit 2 ;;
esac
