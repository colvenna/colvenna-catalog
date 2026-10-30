#!/usr/bin/env bash
# The serverless form's maintenance and purge functions (#13, #69): the catalog's own image with this
# script as its entry point, a Lambda runtime of its own (the Runtime API loop below). Each invocation runs
# one of the server's maintenance commands, with the function's settings, and answers with its exit status:
#   "migrate"          Main migrate: the database schema brought to this image's version. install.sh
#                      runs it before it updates the server function, which never migrates (Lambda's init
#                      must not wait on the database).
#   "bootstrap-admin"  Main bootstrap-admin admin P90D <file>: the admin principal if missing, and a new
#                      token for it. The token goes into the answer, the only place it is ever written;
#                      install.sh puts it straight into the admin token secret.
#   "purge"            Main purge --until <ms>: the files of dropped tables, as recorded in the database,
#                      deleted until a minute before the invocation's deadline (the Runtime API's
#                      Lambda-Runtime-Deadline-Ms). The purge function's schedule invokes it; a purge that
#                      fails stays recorded, and the invocation reports an error (below). Its output goes
#                      to the function's log too, its summary a line of embedded metric format.
# A function runs only the commands COLVENNA_MAINTENANCE_COMMANDS names (space-separated; by default
# "migrate bootstrap-admin", the maintenance function's): the purge function's is "purge" alone.
# The answer is {"command": ..., "exitCode": n, "log": "<the output's last 8 KiB, base64>"}, plus
# "token" for bootstrap-admin. A purge that exits non-zero is posted as the invocation's error instead,
# {"errorMessage", "errorType": "PurgeFailed", "exitCode", "log"}, so that Lambda counts it (the Errors
# metric, which the purge alarm watches) and retries an asynchronous invocation; migrate and
# bootstrap-admin always answer, since install.sh reads their exitCode. The functions are reached only
# by IAM-authenticated Invokes (the purge function's also by its schedule's role): no API, no Function
# URL, no other trigger. The Lambda Web Adapter in /opt/extensions is started here too; it takes
# invocations only once its readiness check passes, and nothing here ever listens on its port, so every
# invocation is this loop's.
set -uo pipefail

api="http://${AWS_LAMBDA_RUNTIME_API:?not a Lambda execution environment}/2018-06-01/runtime"
classpath="/opt/colvenna-catalog/colvenna-catalog.jar:/opt/colvenna-catalog/lib/*"
admin_ttl=P90D
allowed="${COLVENNA_MAINTENANCE_COMMANDS:-migrate bootstrap-admin}"
# What a purge leaves of the invocation's time: the JVM's exit and the answer.
purge_margin_ms=60000

# catalog <command> <arguments...>: the server's Main, as the image's entry point runs it.
catalog() {
  java -XX:MaxRAMPercentage=75 -Djava.io.tmpdir=/tmp -cp "$classpath" com.colvenna.catalog.Main "$@"
}

# runs <command>: whether this function runs the command.
runs() {
  local c
  for c in $allowed; do
    [[ "$c" == "$1" ]] && return 0
  done
  return 1
}

# header <name>: the value of a header of the invocation/next response.
header() {
  tr -d '\r' <"$work/headers" | awk -F': *' -v name="$1" 'tolower($1) == tolower(name) {print $2; exit}'
}

while true; do
  work="$(mktemp -d)"
  chmod 700 "$work"
  # The next invocation; when the Runtime API itself fails, the environment is done (Lambda starts another).
  curl -sS -D "$work/headers" -o "$work/event" "$api/invocation/next" || exit 1
  id="$(header lambda-runtime-aws-request-id)"
  [[ -n "$id" ]] || exit 1
  event="$(tr -d '[:space:]' <"$work/event")"
  token=""
  command=unknown
  case "$event" in
    '"migrate"') command=migrate ;;
    '"bootstrap-admin"') command=bootstrap-admin ;;
    '"purge"') command=purge ;;
  esac
  if [[ "$command" == unknown ]] || ! runs "$command"; then
    echo "unknown command: this function runs ${allowed// /, } (the payload is the command as a JSON string)" >"$work/log"
    command=unknown
    code=2
  else
    case "$command" in
      migrate)
        catalog migrate >"$work/log" 2>&1
        code=$?
        ;;
      bootstrap-admin)
        catalog bootstrap-admin admin "$admin_ttl" "$work/token" >"$work/log" 2>&1
        code=$?
        if ((code == 0)); then
          token="$(cat "$work/token" 2>/dev/null)"
          # A token is colvenna_ and URL-safe base64: nothing in it needs escaping in JSON.
          [[ "$token" =~ ^colvenna_[A-Za-z0-9_-]+$ ]] || { token="" code=1; echo "bootstrap-admin wrote no token" >>"$work/log"; }
        fi
        ;;
      purge)
        deadline="$(header lambda-runtime-deadline-ms)"
        if [[ "$deadline" =~ ^[0-9]+$ ]]; then
          catalog purge --until "$((deadline - purge_margin_ms))" >"$work/log" 2>&1
        else
          catalog purge >"$work/log" 2>&1
        fi
        code=$?
        # Into the function's own log as well: nobody reads a scheduled run's answer. The summary,
        # its last line, is CloudWatch embedded metric format, which the backlog alarm reads.
        cat "$work/log"
        ;;
    esac
  fi
  log="$(tail -c 8192 "$work/log" | base64 -w0)"
  if [[ "$command" == purge ]] && ((code != 0)); then
    printf '{"errorMessage":"purge exited %d","errorType":"PurgeFailed","exitCode":%d,"log":"%s"}' "$code" "$code" "$log" >"$work/answer"
    curl -sS -o /dev/null -X POST -H "Lambda-Runtime-Function-Error-Type: PurgeFailed" --data-binary "@$work/answer" \
      "$api/invocation/$id/error" || exit 1
  else
    if [[ -n "$token" ]]; then
      printf '{"command":"%s","exitCode":%d,"log":"%s","token":"%s"}' "$command" "$code" "$log" "$token" >"$work/answer"
    else
      printf '{"command":"%s","exitCode":%d,"log":"%s"}' "$command" "$code" "$log" >"$work/answer"
    fi
    curl -sS -o /dev/null -X POST --data-binary "@$work/answer" "$api/invocation/$id/response" || exit 1
  fi
  rm -rf "$work"
done
