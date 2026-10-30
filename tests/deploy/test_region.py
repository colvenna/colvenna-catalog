"""Every AWS call of the installer and the uninstaller goes to the install's Region (#27): a call without
--region goes to the AWS CLI's default Region (AWS_REGION, AWS_DEFAULT_REGION or the profile's), which need
not be COLVENNA_REGION. The EKS form's database step read its two secrets that way, and an install in
us-east-2 from a host whose default was another Region failed there. Only global services' calls (IAM, STS's
caller identity, S3's bucket location) may go without one."""
import re
import unittest

from helpers import REPO

FILES = [REPO / "install.sh", REPO / "uninstall.sh", *sorted((REPO / "deploy/lib").glob("*.sh"))]
CALL = re.compile(r"\baws ([a-z0-9-]+) ([a-z0-9-]+)")
# Calls that need no Region: global services, or one that answers for a bucket in any Region.
GLOBAL = {("sts", "get-caller-identity"), ("s3api", "get-bucket-location")}
GLOBAL_SERVICES = {"iam"}


def logical_lines(text):
    """(first line number, the line with its backslash continuations joined)."""
    out, current, start = [], "", None
    for number, line in enumerate(text.splitlines(), 1):
        line = line.rstrip()
        start = start or number
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        out.append((start, current + line))
        current, start = "", None
    return out


def calls_without_region(text):
    """(line, service, operation) of each aws command the script runs without --region. A command written
    inside a string (a hint the script prints) is not run, so it is not one; one in $( ) is."""
    found = []
    for number, line in logical_lines(text):
        if line.lstrip().startswith("#") or re.search(r"\btools=\(", line):
            continue
        code = line.split(" #")[0]
        for m in CALL.finditer(code):
            before = code[:m.start()]
            if before.count('"') % 2 == 1 and not before.endswith("$("):
                continue
            service, operation = m.group(1), m.group(2)
            if (service, operation) in GLOBAL or service in GLOBAL_SERVICES:
                continue
            if "--region" not in code[m.start():].split("|")[0]:
                found.append((number, service, operation))
    return found


class EveryCallNamesTheRegion(unittest.TestCase):
    def test_the_installer_and_the_uninstaller(self):
        missing = [f"{f.relative_to(REPO)}:{n} aws {s} {o}" for f in FILES for n, s, o in calls_without_region(f.read_text())]
        self.assertEqual(missing, [])

    def test_the_check_itself(self):
        script = "\n".join([
            'aws secretsmanager get-secret-value --secret-id "$A" --query SecretString --output text >a',  # 1: no Region
            'aws secretsmanager get-secret-value --region "$R" --secret-id "$A" \\',                         # 2: has it
            '  --output text >b',
            'x="$(aws ecr describe-images --repository-name r \\',                                           # 4: no Region, in $( )
            '  --query x)"',
            'refuse "turn it off (aws rds modify-db-instance --db-instance-identifier x)"',                  # a hint, not run
            'aws sts get-caller-identity --query Account --output text',                                     # global
            'aws iam list-open-id-connect-providers --output json',                                          # global
            '# aws lambda invoke --function-name f',                                                         # a comment
            'aws ecr get-login-password | docker login --username AWS --password-stdin r',                   # 10: no Region
        ])
        self.assertEqual(calls_without_region(script), [(1, "secretsmanager", "get-secret-value"),
                                                        (4, "ecr", "describe-images"), (10, "ecr", "get-login-password")])


if __name__ == "__main__":
    unittest.main()
