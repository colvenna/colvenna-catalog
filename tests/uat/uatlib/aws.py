"""The AWS CLI, as the harness calls it: always with the operator's explicit profile (AWS_PROFILE; the
harness refuses to run without one, never falling back to a default identity), in the case's region,
with JSON output. Credentials a case was given (a table's per-table credentials, say) are used by
calls made with them alone, the profile taken away, so that nothing else can stand in for them."""

import json
import os
import subprocess


class AwsError(RuntimeError):
    def __init__(self, args, code, stderr):
        super().__init__(f"aws {' '.join(args[:3])} ... failed ({code}): {stderr.strip()[-1500:]}")
        self.code = code
        self.stderr = stderr


class Aws:
    def __init__(self, region, profile=None):
        self.region = region
        self.profile = profile or os.environ.get("AWS_PROFILE", "")
        if not self.profile:
            raise RuntimeError("Set AWS_PROFILE: the harness never uses a default profile")

    def in_region(self, region):
        """The same calls, with the same profile, in another region."""
        return Aws(region, self.profile)

    def env(self, credentials=None):
        env = dict(os.environ, AWS_REGION=self.region, AWS_DEFAULT_REGION=self.region, AWS_PAGER="")
        if credentials:
            for key in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
                env.pop(key, None)
            env.update(AWS_ACCESS_KEY_ID=credentials["accessKeyId"], AWS_SECRET_ACCESS_KEY=credentials["secretAccessKey"],
                       AWS_SESSION_TOKEN=credentials.get("sessionToken", ""),
                       # Nothing else of this host's: no config or credential files, no instance role.
                       AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
                       AWS_EC2_METADATA_DISABLED="true")
        else:
            env["AWS_PROFILE"] = self.profile
        return env

    def raw(self, *args, credentials=None, input=None, timeout=300, check=True):
        """The CLI's completed process; raises AwsError when check and it failed."""
        p = subprocess.run(["aws", *args, "--region", self.region], input=input, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, env=self.env(credentials), timeout=timeout)
        if check and p.returncode != 0:
            raise AwsError(list(args), p.returncode, p.stderr)
        return p

    def __call__(self, *args, credentials=None, timeout=300):
        """The call's JSON output (None for no output)."""
        out = self.raw(*args, "--output", "json", credentials=credentials, timeout=timeout).stdout.strip()
        return json.loads(out) if out else None

    def text(self, *args, timeout=300):
        return self.raw(*args, "--output", "text", timeout=timeout).stdout.strip()

    def account(self):
        return self("sts", "get-caller-identity")["Account"]

    def outputs(self, stack):
        """A stack's outputs as {key: value}; {} when the stack does not exist."""
        p = self.raw("cloudformation", "describe-stacks", "--stack-name", stack, "--output", "json", check=False)
        if p.returncode != 0:
            if "does not exist" in p.stderr:
                return {}
            raise AwsError(["cloudformation", "describe-stacks", stack], p.returncode, p.stderr)
        stack = json.loads(p.stdout)["Stacks"][0]
        return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
