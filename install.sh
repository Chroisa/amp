#!/bin/sh
# install.sh – install amp as the default man pager for the current user
#
# Usage:
#   ./install.sh              # print session export instructions
#   ./install.sh --permanent  # append MANPAGER to ~/.bashrc or ~/.zshrc
#   ./install.sh --session    # same as default
#
# Does NOT set ANIM_* variables – those are per-page / per-session.
# After install, for an animated page:
#   export ANIM_A_TIMING=/path/to/A.timing
#   export ANIM_A_TYPESCRIPT=/path/to/A.typescript
#   man topic
#
# Undo with:  ./uninstall.sh

set -e
DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
AMP="$DIR/amp"
case "${SHELL:-}" in
  *zsh*) SHELL_RC="$HOME/.zshrc" ;;
  *)     SHELL_RC="$HOME/.bashrc" ;;
esac
LINE="export MANPAGER=\"$AMP\""

usage() {
  cat <<'USAGE'
amp install.sh – set MANPAGER to this amp wrapper

  ./install.sh              print export for the current shell
  ./install.sh --session    same as default
  ./install.sh --permanent  append MANPAGER to ~/.bashrc or ~/.zshrc
  ./install.sh -h|--help    this help

After installing, animations still need per-letter paths, for example:

  export ANIM_A_TIMING=/path/to/A.timing
  export ANIM_A_TYPESCRIPT=/path/to/A.typescript
  man yourtopic

Undo with:  ./uninstall.sh
USAGE
}

permanent() {
  if grep -Fqs "$AMP" "$SHELL_RC" 2>/dev/null; then
    echo "Already present in $SHELL_RC"
  else
    printf '\n# amp (animated man page)\n%s\n' "$LINE" >> "$SHELL_RC"
    echo "Appended to $SHELL_RC"
    echo "Run:  source $SHELL_RC"
  fi
  echo "Current session – eval this now:"
  echo "  $LINE"
}

session() {
  echo "Run this in your shell (reverts when the shell exits):"
  echo "  $LINE"
  echo
  echo "One-shot without changing MANPAGER:"
  echo "  man -P \"$AMP\" topic"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --permanent) permanent ;;
  --session|"") session ;;
  *) echo "Unknown option: $1"; usage; exit 2 ;;
esac
