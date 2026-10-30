#!/usr/bin/env bash
# helm_upgrade, the one way the install scripts change a Helm release (from colvenna-lake; install.sh
# sources this file; it needs STATE_DIR, the environment's local state, .state/<env>/).
#
# helm_upgrade <release> <chart> <helm upgrade arguments, --namespace among them>...
#   runs `helm upgrade --install <release> <chart> <arguments>`, holding the release's lock in this
#   workspace's local state (helm-<namespace>-<release>.lock) the whole time: a second run of this
#   workspace waits for the first (HELM_LOCK_WAIT seconds at most, default 3600), and the helm it
#   starts holds the lock too, so the lock is free only once that helm is gone. The lock is the
#   workspace's: two workspaces (worktrees) driving one environment are not serialized by it; one
#   environment is installed from one workspace at a time.
#
#   Before each change it makes (a recovery's rollback, then the upgrade), it records in <lock>.run
#   the revision that change is about to create, as running, and marks the record finished once that
#   helm has returned by itself (whatever its exit status: Helm then recorded the outcome). A helm
#   killed by a signal, or a run killed while its helm ran (a lost SSH session, a Ctrl-C, a host
#   restart), leaves the record running; found so when the lock is free again, the revision it names
#   is that killed run's.
#
#   A release found in pending-install, pending-upgrade or pending-rollback (Helm then refuses every
#   later operation with "another operation (install/upgrade/rollback) is in progress") is recovered
#   only when its pending revision is the one this workspace's killed run recorded: a pending-upgrade
#   or pending-rollback is rolled back to the last deployed revision, a pending-install that never
#   deployed is uninstalled, so that the upgrade can install it again. Any other pending revision (a
#   helm of another workspace, or one run by hand, perhaps still running) is left alone, and the call
#   fails saying how to recover it by hand. Each recovery is logged on stderr.

# helm_pending_action <status> <history-json> <record>: what to do about the release, one line of
# three tab-separated fields, the action (upgrade, rollback, uninstall or refuse), the revision to
# roll back to (- for the others) and why. <record> is the previous run's "<state> <revision> <pid>
# <host>", or empty.
helm_pending_action() {
  python3 - "$@" <<'PY'
import json, sys
status, history, record = sys.argv[1], json.loads(sys.argv[2] or "[]"), sys.argv[3].split()
if not status.startswith("pending-"):
    print("upgrade\t-\t-")
    sys.exit()
latest = max(history, key=lambda r: r["revision"])
pending = latest["revision"]
ours = len(record) >= 2 and record[0] == "running" and record[1] == str(pending)
deployed = [r["revision"] for r in history if r["status"] == "deployed" and r["revision"] != pending]
superseded = [r["revision"] for r in history if r["status"] == "superseded" and r["revision"] != pending]
target = max(deployed) if deployed else max(superseded) if superseded else None
if not ours:
    last = (f"this workspace's last run {record[0]} revision {record[1]}" if len(record) >= 2
            else "this workspace has no record of a run of it")
    by_hand = f"helm rollback <release> {target}" if target else "helm uninstall <release>"
    print(f"refuse\t-\trevision {pending} is {status} and not this workspace's ({last}): another workspace or a "
          f"helm run by hand began it, and it may still be running; once it is not, recover it by hand ({by_hand}) "
          f"and run this step again")
elif status == "pending-install" and target is None:
    print(f"uninstall\t-\trevision {pending} pending-install, left by this workspace's killed run; never deployed")
elif target is not None and status in ("pending-upgrade", "pending-rollback"):
    print(f"rollback\t{target}\trevision {pending} {status}, left by this workspace's killed run")
else:
    print(f"refuse\t-\trevision {pending} is {status}, left by this workspace's killed run, with no deployed revision "
          f"to go back to: recover it by hand")
PY
}

helm_upgrade() {
  local release="$1" chart="$2" namespace="" previous="" arg
  shift 2
  for arg in "$@"; do
    [[ "$previous" == --namespace || "$previous" == -n ]] && namespace="$arg"
    [[ "$arg" != --namespace=* ]] || namespace="${arg#--namespace=}"
    previous="$arg"
  done
  [[ -n "$namespace" ]] || { echo "helm_upgrade $release: no --namespace" >&2; return 1; }
  local lock="$STATE_DIR/helm-$namespace-$release.lock"
  (
    exec 9>"$lock"
    if ! flock -n 9; then
      echo "helm_upgrade: $namespace/$release is locked by another run of this workspace; waiting (the last record: $(cat "$lock.run" 2>/dev/null || echo none))" >&2
      flock -w "${HELM_LOCK_WAIT:-3600}" 9 || {
        echo "helm_upgrade: $namespace/$release still locked after ${HELM_LOCK_WAIT:-3600} s" >&2
        exit 1
      }
    fi
    local record="" status="" out err history action revision why latest
    [[ ! -s "$lock.run" ]] || record="$(<"$lock.run")"
    # changing <revision> <helm arguments>...: helm making that revision, recorded running until
    # helm returns by itself. Killed by a signal (status above 128), or this shell killed meanwhile
    # (no trap marks it: bash would run one on SIGHUP too), the record stays running.
    changing() {
      local made="$1" code=0
      shift
      printf 'running %s %s %s\n' "$made" "$BASHPID" "$(hostname)" >"$lock.run"
      helm "$@" || code=$?
      ((code > 128)) || sed -i '1s/^running /finished /' "$lock.run"
      return "$code"
    }
    err="$(mktemp)"
    if out="$(helm status "$release" --namespace "$namespace" -o json 2>"$err")"; then
      status="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["status"])' <<<"$out")"
    elif ! grep -q 'release: not found' "$err"; then
      echo "helm_upgrade: cannot read $namespace/$release: $(tr '\n' ' ' <"$err")" >&2
      rm -f "$err"
      exit 1
    fi
    rm -f "$err"
    history="[]"
    [[ -z "$status" ]] || history="$(helm history "$release" --namespace "$namespace" --max 256 -o json)"
    IFS=$'\t' read -r action revision why < <(helm_pending_action "$status" "$history" "$record")
    [[ -n "$action" ]] || { echo "helm_upgrade: $namespace/$release: no decision about its state" >&2; exit 1; }
    latest="$(python3 -c 'import json,sys; print(max([r["revision"] for r in json.loads(sys.argv[1])] or [0]))' "$history")"
    case "$action" in
      upgrade) ;;
      rollback)
        echo "helm_upgrade: $namespace/$release: $why; rolling back to revision $revision" >&2
        changing "$((latest + 1))" rollback "$release" "$revision" --namespace "$namespace" --wait --timeout 10m >&2
        latest=$((latest + 1))
        ;;
      uninstall)
        echo "helm_upgrade: $namespace/$release uninstalled ($why), to install it again" >&2
        helm uninstall "$release" --namespace "$namespace" --wait --timeout 10m >&2
        latest=0
        ;;
      *)
        echo "helm_upgrade: $namespace/$release left alone: $why" >&2
        exit 1
        ;;
    esac
    changing "$((latest + 1))" upgrade --install "$release" "$chart" "$@"
  )
}
