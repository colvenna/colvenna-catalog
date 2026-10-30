"""What the harness needs to know of the install a case runs on, and of the test environment its
clients run in, read from the install's configuration (deploy/config/<env>.env, with
deploy/config/example.env's defaults), the stacks' outputs (CloudFormation) and, in the EKS form, the
catalog's namespace. Nothing is read from a local state directory: any checkout of the candidate on
any host with the operator's profile finds the same."""

import base64
import json
import re
from pathlib import Path

from .kube import placement

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = REPO / "deploy" / "config" / "example.env"
LINE = re.compile(r"^(COLVENNA_[A-Z0-9_]+)=(.*)$")
# The Secrets the EKS form keeps in the catalog's namespace (deploy/lib/catalog.sh).
CA_SECRET = "colvenna-catalog-ca-key"  # pragma: allowlist secret - the name of a Kubernetes Secret of the EKS form, not a value
ADMIN_TOKEN_SECRET = "colvenna-catalog-admin-token"  # pragma: allowlist secret - the name of a Kubernetes Secret of the EKS form, not a value
DB_SECRET = "colvenna-catalog-db"  # pragma: allowlist secret - the name of a Kubernetes Secret of the EKS form, not a value
DB_CA_CONFIGMAP = "colvenna-catalog-db-ca"
API_PORT, HEALTH_PORT, METRICS_PORT = 8181, 8182, 9100


def parse_env(text):
    """KEY=value lines (values literal, never expanded), comments and blank lines skipped."""
    out = {}
    for line in text.splitlines():
        m = LINE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def stack_suffix(name):
    return name[:1].upper() + name[1:]


class Install:
    """An install of the catalog: its configuration, stacks and, in the EKS form, namespace."""

    def __init__(self, config_path, aws, kube=None):
        text = Path(config_path).read_text(encoding="utf-8")
        self.config = dict(parse_env(EXAMPLE.read_text(encoding="utf-8")), **parse_env(text))
        self.config_path = Path(config_path)
        self.env = self.config["COLVENNA_ENV"]
        self.form = self.config.get("COLVENNA_FORM") or "eks"
        self.region = self.config["COLVENNA_REGION"]
        self.aws = aws
        self.kube = kube
        self._outputs = {}
        if aws.region != self.region:
            raise ValueError(f"the install {self.env} is in {self.region}, not {aws.region}")

    def get(self, key):
        return self.config.get(key, "")

    def use(self, config_path):
        """The configuration read again, from another file: the install now runs on another database
        (UAT-DEP-10's restore), and what a case does next goes there. The stacks' outputs are read again."""
        text = Path(config_path).read_text(encoding="utf-8")
        self.config = dict(parse_env(EXAMPLE.read_text(encoding="utf-8")), **parse_env(text))
        self.config_path = Path(config_path)
        self.refresh()

    # --- names --------------------------------------------------------------------------------------

    def stack(self, part):
        """ColvennaCatalog<Env><Part>: Storage, Eks, Network, Images or Serverless."""
        return f"ColvennaCatalog{stack_suffix(self.env)}{part}"

    def refresh(self):
        """Forgets the stacks' outputs read so far: a case that installs, or installs again, reads them anew."""
        self._outputs.clear()

    def outputs(self, part):
        if part not in self._outputs:
            self._outputs[part] = self.aws.outputs(self.stack(part))
        return self._outputs[part]

    def output(self, part, key):
        value = self.outputs(part).get(key)
        if not value:
            raise KeyError(f"{self.stack(part)} has no output {key}")
        return value

    @property
    def namespace(self):
        return self.get("COLVENNA_NAMESPACE")

    @property
    def release(self):
        return self.get("COLVENNA_RELEASE_NAME")

    @property
    def warehouse(self):
        return self.get("COLVENNA_WAREHOUSE_NAME")

    @property
    def client_label(self):
        """(key, value) of the label a client namespace carries (EKS form)."""
        key, _, value = self.get("COLVENNA_CLIENT_NAMESPACE_LABEL").partition("=")
        return key, value

    @property
    def metrics_label(self):
        key, _, value = self.get("COLVENNA_METRICS_NAMESPACE_LABEL").partition("=")
        return key, value

    @property
    def uri(self):
        """The catalog's REST URI as clients in the test environment reach it."""
        if self.form == "eks":
            return f"https://{self.release}.{self.namespace}.svc:{API_PORT}"
        return self.output("Serverless", "ApiUrl").rstrip("/")

    @property
    def host(self):
        return re.sub(r"^https://([^/:]+).*$", r"\1", self.uri)

    @property
    def admin_base(self):
        return self.uri + "/colvenna/admin/v1"

    @property
    def rest_base(self):
        return f"{self.uri}/v1/{self.warehouse}"

    @property
    def bucket(self):
        return self.get("COLVENNA_BUCKET") or self.output("Storage", "BucketName")

    @property
    def prefix(self):
        return self.get("COLVENNA_WAREHOUSE_PREFIX")

    # --- secrets: read when needed, never kept on disk ----------------------------------------------

    def ca(self):
        """The catalog's CA certificate (PEM), EKS form; None in the serverless form (an AWS certificate)."""
        if self.form != "eks":
            return None
        return self._secret_key(CA_SECRET, "ca.crt")

    def admin_token(self):
        if self.form == "eks":
            return self._secret_key(ADMIN_TOKEN_SECRET, "token").strip()
        arn = self.output("Serverless", "AdminTokenSecretArn")
        return self.aws.raw("secretsmanager", "get-secret-value", "--secret-id", arn, "--query", "SecretString",
                            "--output", "text").stdout.strip()

    def _secret_key(self, secret, key):
        data = json.loads(self.kube.kubectl("get", "secret", secret, "-n", self.namespace, "-o", "json").stdout)["data"]
        return base64.b64decode(data[key]).decode()

    def describe(self):
        """What a run records of the install (no secret, no account)."""
        return {"env": self.env, "form": self.form, "region": self.region, "warehouse": self.warehouse,
                "namespace": self.namespace if self.form == "eks" else None,
                "dataTokens": self.get("COLVENNA_DATA_TOKENS"), "dataTokenDuration": self.get("COLVENNA_DATA_TOKEN_DURATION")}


class TestEnvironment:
    """The repository's test environment (tests/environment): its cluster hosts every client."""

    def __init__(self, name, aws):
        self.name = name
        self.aws = aws
        self.stack = f"ColvennaCatalogTest{stack_suffix(name)}"
        self.outputs = aws.outputs(self.stack)
        if not self.outputs:
            raise ValueError(f"no test environment {name} in {aws.region} ({self.stack})")

    @property
    def cluster(self):
        return self.outputs["ClusterName"]

    def output(self, key):
        value = self.outputs.get(key)
        if not value:
            raise KeyError(f"{self.stack} has no output {key}")
        return value

    def client_placement(self):
        """Where the run's pods go: the clients' nodes (outputs ClientNodeSelector, ClientNodeTaint),
        never beside the catalogs. An environment made before it had them is brought up again first."""
        try:
            return placement(self.output("ClientNodeSelector"), self.output("ClientNodeTaint"))
        except KeyError as e:
            raise ValueError(f"{e.args[0]}: the test environment {self.name} has no clients' node group; "
                             f"bring it up again (tests/environment/up.sh {self.name} {self.aws.region})") from None

    def kubeconfig(self, path):
        """Writes a kubeconfig of the cluster to path (IAM-authenticated through the profile)."""
        self.aws.raw("eks", "update-kubeconfig", "--name", self.cluster, "--kubeconfig", str(path))
        return Path(path)
