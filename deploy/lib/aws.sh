# shellcheck shell=bash
# The AWS CLI as the entry points call it, throttled (#94). AWS limits each account's calls to each API in
# each Region, and whatever else runs there draws on the same quota: another install, CloudFormation and
# CDK themselves. API Gateway takes CreateDeployment once every 5 seconds per account, a quota that
# cannot be raised (https://docs.aws.amazon.com/apigateway/latest/developerguide/limits.html), and two
# installs in one Region at once had one of them fail on it.

# The CLI's standard retry mode tries a throttled call again, and a transient error (a 5xx, a lost
# connection), with exponential backoff, at most 20 seconds a wait, 3 attempts in all unless configured,
# and fails at once on any other error (https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-retries.html).
AWS_CLI_ATTEMPTS=3
# aws_cli_retries: install.sh and uninstall.sh run every aws command in the standard mode with
# AWS_RUN_ATTEMPTS attempts, unless the environment sets AWS_RETRY_MODE or AWS_MAX_ATTEMPTS. Not the
# adaptive mode: the CLI calls it experimental, and what it adds, a rate limit of the client's own, would
# last one command, each aws command being a client of its own. These retries are silent. The calls API
# Gateway limits hardest, and the reads a wait polls, where one failure would end a long wait, go by
# aws_retry, which says each retry and waits longer. A Lambda Invoke (lambda_invoke) keeps the CLI's
# AWS_CLI_ATTEMPTS, as before #94: it runs a function, and is no control-plane call.
AWS_RUN_ATTEMPTS=10
aws_cli_retries() {
  export AWS_RETRY_MODE="${AWS_RETRY_MODE:-standard}" AWS_MAX_ATTEMPTS="${AWS_MAX_ATTEMPTS:-$AWS_RUN_ATTEMPTS}"
}

# The error codes the standard mode retries as throttling (the CLI's page above). API Gateway's
# LimitExceededException is one: "The request exceeded the rate limit" (HTTP 429, its API reference:
# https://docs.aws.amazon.com/apigateway/latest/api/API_CreateDeployment.html).
THROTTLE_CODES='Throttling|ThrottlingException|ThrottledException|RequestThrottledException|TooManyRequestsException'
THROTTLE_CODES+='|ProvisionedThroughputExceededException|TransactionInProgressException|RequestLimitExceeded'
THROTTLE_CODES+='|BandwidthLimitExceeded|LimitExceededException|RequestThrottled|SlowDown|EC2ThrottledException'
# aws_retry's waits: the first THROTTLE_FIRST_WAIT seconds, each later one twice the one before, at most
# THROTTLE_MAX_WAIT; each of them half that or more, at random (two installs throttled together do not
# try again together); none ending later than THROTTLE_SECONDS after the first attempt began.
THROTTLE_FIRST_WAIT=5
THROTTLE_MAX_WAIT=40
THROTTLE_SECONDS=180

# aws_retry [--errors <file>] <aws arguments...>: aws <arguments>, tried again while AWS throttles it (its
# error names one of THROTTLE_CODES), waiting as above, each retry said on stderr; any other failure
# returns at once. Each attempt is the CLI's AWS_CLI_ATTEMPTS, so that a throttle of a second or two
# passes within it and the long waits are these, said and bounded: THROTTLE_SECONDS, and one attempt.
# Prints the last attempt's output and returns its status; its errors (the CLI's, never the retries')
# go to stderr, or into <file>, for a caller that reads them.
aws_retry() {
  local errors="" out err status code wait limit="$THROTTLE_FIRST_WAIT" attempt=1 began="$SECONDS"
  local throttled="\\(($THROTTLE_CODES)\\)"
  if [[ "${1:-}" == --errors ]]; then
    errors="$2"
    shift 2
  fi
  out="$(mktemp)" err="$(mktemp)"
  while :; do
    status=0
    AWS_MAX_ATTEMPTS="$AWS_CLI_ATTEMPTS" aws "$@" >"$out" 2>"$err" || status=$?
    # The CLI names the error's code in parentheses: "An error occurred (<code>) when calling ...".
    if ((status == 0)) || ! [[ "$(<"$err")" =~ $throttled ]]; then
      break
    fi
    code="${BASH_REMATCH[1]}"
    wait=$((limit - limit / 2 + RANDOM % (limit / 2 + 1)))
    if ((SECONDS - began + wait > THROTTLE_SECONDS)); then
      echo "  aws $1 $2: throttled ($code) $attempt time(s) in $((SECONDS - began)) s; not tried again" >&2
      break
    fi
    echo "  aws $1 $2: throttled ($code); trying again in $wait s (attempt $((attempt + 1)))" >&2
    sleep "$wait"
    attempt=$((attempt + 1))
    limit=$((limit * 2 < THROTTLE_MAX_WAIT ? limit * 2 : THROTTLE_MAX_WAIT))
  done
  cat "$out"
  if [[ -n "$errors" ]]; then
    cat "$err" >"$errors"
  else
    cat "$err" >&2
  fi
  rm -f "$out" "$err"
  return "$status"
}
