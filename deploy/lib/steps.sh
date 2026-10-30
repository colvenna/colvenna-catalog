# shellcheck shell=bash
# The entry points' steps. Each announces itself; under set -E, with on_error as the ERR trap, a command
# failing in any of them ends the script with status 1, naming the step. The statuses 2 (arguments,
# configuration, confirmation) and 3 (preflight) come from explicit exits, which the trap leaves alone.

STEP=""
step() {
  STEP="$1"
  echo "== $STEP"
}

# on_error <script>: the ERR trap. In a subshell (a command substitution) it only passes the status on,
# so that the shell running the steps reports the failure once.
on_error() {
  local status=$?
  ((BASH_SUBSHELL == 0)) || exit "$status"
  echo "$1: failed at step \"$STEP\" (status $status); fix the cause and run it again" >&2
  exit 1
}
