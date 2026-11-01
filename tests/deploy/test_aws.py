"""The AWS CLI throttled (deploy/lib/aws.sh, #94): aws_retry tries a throttled call again, with exponential backoff and
jitter, for THROTTLE_SECONDS at most, saying each retry, and any other failure returns at once; aws_cli_retries sets
the CLI's own retries for a run of install.sh or uninstall.sh. The waits are CLOCK's (helpers): none is real."""
import unittest

from helpers import CLOCK, REPO, run_bash, slept
from stubs import Stubs

CALL = "apigateway create-deployment --region us-west-2 --rest-api-id abc --stage-name catalog"


def throttled(code="TooManyRequestsException", operation="CreateDeployment"):
    """A throttled call's answer, as the CLI prints it once its own attempts are spent."""
    return {"stderr": f"aws: [ERROR]: An error occurred ({code}) when calling the {operation} operation "
                      "(reached max retries: 2): Too Many Requests\n", "code": 254}


def failed(code):
    return {"stderr": f"An error occurred ({code}) when calling the CreateDeployment operation: no\n", "code": 254}


def retried(answers, script=f"aws_retry {CALL}"):
    """The script with the stub answering create-deployment so (a list: one answer per call, the last again)."""
    stubs = Stubs({"aws": {"apigateway create-deployment": answers}})
    out = run_bash(CLOCK + script, env=stubs.env, path_prepend=stubs.bin)
    return out, stubs.calls(), slept(stubs)


def notes(out):
    """aws_retry's own lines on stderr."""
    return [line for line in out.stderr.splitlines() if line.startswith("  aws ")]


class Retry(unittest.TestCase):
    def test_throttled_twice_then_answered(self):
        out, calls, waits = retried([throttled(), throttled(), {"stdout": "new1\n"}])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "new1\n")
        self.assertEqual(calls, [f"aws {CALL}"] * 3)
        # The first wait 5 s or up to half less, the second twice as long, each at random in its half.
        self.assertEqual(len(waits), 2)
        self.assertTrue(3 <= waits[0] <= 5 and 5 <= waits[1] <= 10, waits)
        self.assertEqual(notes(out), [
            f"  aws apigateway create-deployment: throttled (TooManyRequestsException); trying again in {waits[0]} s (attempt 2)",
            f"  aws apigateway create-deployment: throttled (TooManyRequestsException); trying again in {waits[1]} s (attempt 3)"])
        # What the throttled attempts printed is not passed on: the call succeeded.
        self.assertNotIn("Too Many Requests", out.stderr)

    def test_throttled_throughout_fails_once_the_time_is_spent(self):
        out, calls, waits = retried(throttled())
        self.assertEqual(out.returncode, 254, out.stderr)
        self.assertEqual(out.stdout, "")
        self.assertEqual(len(calls), len(waits) + 1)
        # 5, 10, 20, then 40 at most, each half that or more; none past the 180 seconds, and the next would have been.
        for wait, limit in zip(waits, [5, 10, 20] + [40] * len(waits)):
            self.assertTrue(limit - limit // 2 <= wait <= limit, (waits, limit))
        self.assertLessEqual(sum(waits), 180)
        self.assertGreater(sum(waits) + 40, 180 - 10, waits)  # 10 s of the run's own, at most
        self.assertRegex(notes(out)[-1], rf"^  aws apigateway create-deployment: throttled \(TooManyRequestsException\) "
                                         rf"{len(calls)} time\(s\) in [0-9]+ s; not tried again$")
        # Then the CLI's error, as it printed it, for the step to fail on.
        self.assertTrue(out.stderr.endswith(throttled()["stderr"]), out.stderr)

    def test_any_other_failure_returns_at_once(self):
        for code in ("BadRequestException", "NotFoundException", "AccessDeniedException", "ValidationError", "UnauthorizedException"):
            with self.subTest(code):
                out, calls, waits = retried([failed(code), {"stdout": "new1\n"}])
                self.assertEqual((out.returncode, len(calls), waits, notes(out)), (254, 1, [], []), out.stderr)
                self.assertIn(f"({code})", out.stderr)
        # The code decides, not the message.
        out, calls, _ = retried([{"stderr": "An error occurred (BadRequestException) when calling the CreateDeployment "
                                            "operation: Throttling, TooManyRequestsException\n", "code": 254}])
        self.assertEqual((out.returncode, len(calls)), (254, 1), out.stderr)

    def test_the_throttling_codes_of_the_services_it_calls(self):
        # API Gateway's two (LimitExceededException is its "exceeded the rate limit"), CloudFormation's and ELB's,
        # CloudWatch Logs' and Lambda's, EC2's, S3's.
        for code in ("TooManyRequestsException", "LimitExceededException", "Throttling", "ThrottlingException",
                     "RequestLimitExceeded", "SlowDown"):
            with self.subTest(code):
                out, calls, waits = retried([throttled(code), {"stdout": "new1\n"}])
                self.assertEqual((out.returncode, out.stdout, len(calls), len(waits)), (0, "new1\n", 2, 1), out.stderr)
                self.assertIn(f"throttled ({code})", out.stderr)

    def test_errors_go_into_the_file_the_caller_reads_and_the_retries_to_stderr(self):
        out, calls, _ = retried([throttled(), failed("AccessDeniedException")],
                                f'aws_retry --errors "$STUB_DIR/errors" {CALL} || echo "status $?"; cat "$STUB_DIR/errors"')
        self.assertEqual(len(calls), 2)
        self.assertEqual(out.stdout, "status 254\n" + failed("AccessDeniedException")["stderr"])
        self.assertEqual(out.stderr.splitlines(), notes(out))
        self.assertEqual(len(notes(out)), 1)

    def test_each_attempt_is_the_cli_s_default_three(self):
        # The run's own setting (aws_cli_retries) is not an attempt's: the long waits are aws_retry's.
        script = (f'aws() {{ echo "$AWS_MAX_ATTEMPTS" >>"$STUB_DIR/attempts"; command aws "$@"; }}; export AWS_MAX_ATTEMPTS=10;'
                  f' aws_retry {CALL}; echo "after $AWS_MAX_ATTEMPTS"')
        stubs = Stubs({"aws": {"apigateway create-deployment": [throttled(), {"stdout": "new1\n"}]}})
        out = run_bash(CLOCK + script, env=stubs.env, path_prepend=stubs.bin)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((stubs.dir / "attempts").read_text().split(), ["3", "3"])
        self.assertEqual(out.stdout.splitlines()[-1], "after 10")


class RunRetries(unittest.TestCase):
    def test_the_standard_mode_with_ten_attempts_unless_the_environment_says(self):
        out = run_bash('aws_cli_retries; printenv AWS_RETRY_MODE AWS_MAX_ATTEMPTS', env={"AWS_RETRY_MODE": "", "AWS_MAX_ATTEMPTS": ""})
        self.assertEqual(out.stdout.split(), ["standard", "10"], out.stderr)
        out = run_bash('aws_cli_retries; printenv AWS_RETRY_MODE AWS_MAX_ATTEMPTS', env={"AWS_RETRY_MODE": "legacy", "AWS_MAX_ATTEMPTS": "4"})
        self.assertEqual(out.stdout.split(), ["legacy", "4"], out.stderr)

    def test_install_and_uninstall_set_them_first(self):
        for script in ("install.sh", "uninstall.sh"):
            with self.subTest(script):
                out = run_bash(f'source "{REPO}/{script}"; parse_args() {{ printenv AWS_RETRY_MODE AWS_MAX_ATTEMPTS; exit 0; }}; main',
                               env={"AWS_RETRY_MODE": "", "AWS_MAX_ATTEMPTS": ""})
                self.assertEqual((out.returncode, out.stdout.split()), (0, ["standard", "10"]), out.stderr)


if __name__ == "__main__":
    unittest.main()
