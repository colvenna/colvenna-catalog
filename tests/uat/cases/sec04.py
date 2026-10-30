"""UAT-SEC-04 No Lambda function URL, no public or anonymous resource.

Lists, with the AWS CLI, what the install and the test environment are made of, and what the account
has in the Region: every function's URL configurations (none, ours or not), the stacks' templates (no
AWS::Lambda::Url; no internet gateway, NAT gateway or Elastic IP in a product stack), the REST API
(private, its resource policy, its methods' authorization), load balancers, the buckets, the
repositories (the install's and the release registry's, in the registry's own Region), secrets, the
database instance and, serverless, the identity pool; and makes unsigned requests where a public path
would be. The judging functions take what was collected and nothing else (test_sec04.py)."""

import json
import re
import urllib.error
import urllib.request

from uatlib.aws import Aws
from uatlib.catalog import db_instance

ID = "UAT-SEC-04"
FORMS = ("eks", "serverless")
INTERNET = ("AWS::EC2::InternetGateway", "AWS::EC2::NatGateway", "AWS::EC2::EIP", "AWS::EC2::EgressOnlyInternetGateway")
PULLS = {"ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"}
BLOCKS = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")


def checks(form, args=None):
    names = ["no-function-url", "rest-apis-private", "no-internet-entry", "buckets-private", "repositories-private",
             "secrets-private", "database-private"]
    return names + (["identity-pool-closed"] if form == "serverless" else [])


def statements(policy):
    st = (policy or {}).get("Statement", [])
    return [st] if isinstance(st, dict) else list(st)


def open_to_anyone(statement):
    principal = statement.get("Principal")
    return principal == "*" or "*" in json.dumps(principal)


# --- judging ------------------------------------------------------------------------------------------

def judge_function_urls(functions, with_url, templates):
    """with_url: functions with a URL configuration; templates: {stack: resource types}."""
    url_stacks = [s for s, types in templates.items() if "AWS::Lambda::Url" in types]
    ok = bool(functions) and bool(templates) and not with_url and not url_stacks
    return ok, (f"{len(functions)} functions in the Region, {len(with_url)} with a URL configuration; "
                f"{len(templates)} stacks' templates, {len(url_stacks)} with AWS::Lambda::Url")


def judge_rest_api(form, rest, methods, v2):
    """rest: get-rest-api's answer (serverless); methods: [(method, path, authorization type)]; v2: the
    install's HTTP or WebSocket APIs."""
    problems = []
    if v2:
        problems.append(f"HTTP or WebSocket APIs of the install: {v2}")
    if form == "serverless":
        if (rest or {}).get("endpointConfiguration", {}).get("types") != ["PRIVATE"]:
            problems.append(f"endpoint {(rest or {}).get('endpointConfiguration')}")
        policy = rest_policy(rest)
        allows = [s for s in statements(policy) if s.get("Effect") == "Allow"]
        denies = [s for s in statements(policy) if s.get("Effect") == "Deny"]
        if not allows or any("aws:SourceVpce" not in json.dumps(s.get("Condition", {})) for s in allows):
            problems.append("an Allow without aws:SourceVpce")
        if not any("aws:SourceVpce" in json.dumps(s.get("Condition", {})) for s in denies):
            problems.append("no Deny of other sources")
        if not methods:
            problems.append("no method")
        problems += [f"{m} {p}: {a}" for m, p, a in methods if a != "CUSTOM"]
    ok = not problems
    return ok, "; ".join(problems) or ("PRIVATE, allowed only from aws:SourceVpce and denied otherwise, every method "
                                       "CUSTOM; no HTTP or WebSocket API" if form == "serverless"
                                       else "the EKS form has no API Gateway API; no HTTP or WebSocket API")


def rest_policy(rest):
    """API Gateway gives the policy as JSON text with its quotes escaped."""
    text = (rest or {}).get("policy") or ""
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(json.loads(f'"{text}"'))


def judge_internet(facing, internet):
    ok = not facing and not internet
    return ok, (f"internet-facing load balancers of the install or the test environment: {facing}; internet gateways, "
                f"NAT gateways or Elastic IPs in the product's stacks: {internet}")


def judge_buckets(found):
    """found: {bucket: {"block": settings or None, "policy": policy or None, "unsigned": {what: status}}}."""
    problems = []
    for b, f in found.items():
        block = f.get("block") or {}
        if not all(block.get(k) for k in BLOCKS):
            problems.append(f"{b}: public access block {block or 'none'}")
        if not any(s.get("Effect") == "Deny" and "aws:SecureTransport" in json.dumps(s.get("Condition", {}))
                   for s in statements(f.get("policy"))):
            problems.append(f"{b}: no Deny without TLS")
        problems += [f"{b}: an unsigned {what} got {status}" for what, status in f.get("unsigned", {}).items() if status != 403]
    ok = bool(found) and not problems
    return ok, "; ".join(problems) or f"{len(found)} buckets: every Block Public Access setting on, TLS required, unsigned list and get 403"


def judge_repositories(policies, release_prefix):
    """policies: {repository: its policy or None}. No statement for *; the release registry's are pulls
    by accounts (their root principals) only."""
    problems = []
    for repo, policy in policies.items():
        for st in statements(policy):
            if open_to_anyone(st):
                problems.append(f"{repo}: a statement for *")
            actions = st.get("Action", [])
            actions = {actions} if isinstance(actions, str) else set(actions)
            if repo.startswith(release_prefix):
                principals = st.get("Principal", {}).get("AWS", []) if isinstance(st.get("Principal"), dict) else []
                principals = [principals] if isinstance(principals, str) else principals
                if not principals or any(not re.fullmatch(r"arn:aws[a-z-]*:iam::\d{12}:root", p) for p in principals):
                    problems.append(f"{repo}: principals other than accounts: {principals}")
                if actions - PULLS:
                    problems.append(f"{repo}: actions beyond pulling: {sorted(actions - PULLS)}")
    install = [r for r in policies if not r.startswith(release_prefix)]
    ok = bool(install) and not problems
    return ok, "; ".join(problems) or (f"{len(install)} repositories of the install, {len(policies) - len(install)} of the "
                                       "release registry: none for *; the registry's are pulls by accounts only")


def judge_secrets(policies):
    """policies: {secret name: its resource policy or None}."""
    open_ = [name for name, policy in policies.items() if any(open_to_anyone(st) for st in statements(policy))]
    return bool(policies) and not open_, f"{len(policies)} secrets of the install; open to *: {open_}"


def judge_database(form, db):
    ok = db is not None and not db.get("PubliclyAccessible")
    if form == "serverless":
        ok = ok and bool((db or {}).get("IAMDatabaseAuthenticationEnabled"))
    return ok, (f"publicly accessible: {(db or {}).get('PubliclyAccessible', '?')}; IAM database authentication: "
                f"{(db or {}).get('IAMDatabaseAuthenticationEnabled', '?')}")


def judge_identity_pool(pool, roles, get_id, developer):
    """get_id, developer: (status, __type) of the unsigned calls."""
    providers = pool.get("SupportedLoginProviders") or pool.get("OpenIdConnectProviderARNs") \
        or pool.get("CognitoIdentityProviders") or pool.get("SamlProviderARNs")
    ok = (not pool.get("AllowUnauthenticatedIdentities") and not pool.get("AllowClassicFlow")
          and bool(pool.get("DeveloperProviderName")) and not providers and not roles
          and "NotAuthorizedException" in (get_id[1] or "") and "MissingAuthenticationToken" in (developer[1] or ""))
    return ok, (f"unauthenticated {pool.get('AllowUnauthenticatedIdentities')}, classic flow {pool.get('AllowClassicFlow')}, "
                f"developer provider {bool(pool.get('DeveloperProviderName'))}, other providers {bool(providers)}, "
                f"roles {list(roles)}; unsigned GetId: {get_id}; unsigned GetOpenIdTokenForDeveloperIdentity: {developer}")


# --- the run ------------------------------------------------------------------------------------------

def unsigned(url, method="GET", body=None, headers=None):
    """An anonymous request: (status, error type or text); (None, error) when nothing answered."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # nosec B310 - an https URL the case builds
            return r.status, ""
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            kind = json.loads(text).get("__type", "") or e.headers.get("x-amzn-ErrorType", "")
        except (ValueError, AttributeError):
            kind = e.headers.get("x-amzn-ErrorType", "") or text[:80]
        return e.code, kind
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)


def stacks(ctx):
    """The install's stacks that exist, and the test environment's."""
    parts = ("Storage", "Eks") if ctx.form == "eks" else ("Network", "Storage", "Images", "Serverless")
    return [ctx.install.stack(p) for p in parts if ctx.install.outputs(p)], [ctx.testenv.stack]


def run(ctx):
    aws, inst = ctx.aws, ctx.install
    product, test = stacks(ctx)

    functions = [f["FunctionName"] for f in aws("lambda", "list-functions", timeout=900).get("Functions", [])]
    with_url = [fn for fn in functions
                if aws("lambda", "list-function-url-configs", "--function-name", fn).get("FunctionUrlConfigs")]
    templates, internet = {}, []
    for stack in product + test:
        body = aws("cloudformation", "get-template", "--stack-name", stack, "--template-stage", "Processed")["TemplateBody"]
        template = body if isinstance(body, dict) else json.loads(body)
        templates[stack] = [r.get("Type") for r in template.get("Resources", {}).values()]
        if stack in product:
            internet += [f"{stack}: {t}" for t in templates[stack] if t in INTERNET]
    ctx.check("no-function-url", *judge_function_urls(functions, with_url, templates))

    rest, methods = None, []
    if ctx.form == "serverless":
        api = inst.output("Serverless", "ApiId")
        rest = aws("apigateway", "get-rest-api", "--rest-api-id", api)
        for resource in aws("apigateway", "get-resources", "--rest-api-id", api)["items"]:
            for method in (resource.get("resourceMethods") or {}):
                m = aws("apigateway", "get-method", "--rest-api-id", api, "--resource-id", resource["id"], "--http-method", method)
                methods.append((method, resource["path"], m.get("authorizationType")))
    v2 = [a["Name"] for a in aws("apigatewayv2", "get-apis").get("Items", []) if f"colvenna-catalog-{inst.env}" in a["Name"]]
    ctx.check("rest-apis-private", *judge_rest_api(ctx.form, rest, methods, v2))

    elbs = aws("elbv2", "describe-load-balancers")["LoadBalancers"]
    arns = [e["LoadBalancerArn"] for e in elbs if e["Scheme"] != "internal"]
    facing = []
    for i in range(0, len(arns), 20):
        for d in aws("elbv2", "describe-tags", "--resource-arns", *arns[i:i + 20])["TagDescriptions"]:
            tags = {t["Key"]: t["Value"] for t in d["Tags"]}
            if tags.get("elbv2.k8s.aws/cluster") == ctx.testenv.cluster or tags.get("Environment") == inst.env:
                facing.append(d["ResourceArn"].rsplit("/", 2)[-2])
    ctx.check("no-internet-entry", *judge_internet(facing, internet))

    buckets = [inst.bucket]
    for stack in product:
        for r in aws("cloudformation", "list-stack-resources", "--stack-name", stack)["StackResourceSummaries"]:
            if r["ResourceType"] == "AWS::S3::Bucket" and r["PhysicalResourceId"] not in buckets:
                buckets.append(r["PhysicalResourceId"])
    found = {}
    for b in buckets:
        p = aws.raw("s3api", "get-public-access-block", "--bucket", b, "--output", "json", check=False)
        block = json.loads(p.stdout)["PublicAccessBlockConfiguration"] if p.returncode == 0 else None
        p = aws.raw("s3api", "get-bucket-policy", "--bucket", b, "--output", "json", check=False)
        policy = json.loads(json.loads(p.stdout)["Policy"]) if p.returncode == 0 else None
        host = f"https://{b}.s3.{aws.region}.amazonaws.com"
        found[b] = {"block": block, "policy": policy,
                    "unsigned": {"list": unsigned(f"{host}/")[0], "get": unsigned(f"{host}/{inst.prefix}/no-such-key")[0]}}
    ctx.check("buckets-private", *judge_buckets(found))

    policies = {}
    registries = [(aws, f"colvenna-catalog-{inst.env}/")]
    registry = inst.get("COLVENNA_RELEASE_REGISTRY")
    m = re.match(r"^\d{12}\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com$", registry or "")
    release_aws = Aws(m.group(1)) if m else aws  # the release registry is in its own Region
    registries.append((release_aws, "colvenna-catalog-release/"))
    for client, prefix in registries:
        for r in client("ecr", "describe-repositories")["repositories"]:
            if r["repositoryName"].startswith(prefix):
                p = client.raw("ecr", "get-repository-policy", "--repository-name", r["repositoryName"], "--output", "json",
                               check=False)
                policies[r["repositoryName"]] = json.loads(json.loads(p.stdout)["policyText"]) if p.returncode == 0 else None
    ctx.check("repositories-private", *judge_repositories(policies, "colvenna-catalog-release/"))

    secrets = {}
    for s in aws("secretsmanager", "list-secrets", "--filters", f"Key=name,Values=colvenna-catalog-{inst.env}/")["SecretList"]:
        text = aws("secretsmanager", "get-resource-policy", "--secret-id", s["ARN"]).get("ResourcePolicy")
        secrets[s["Name"]] = json.loads(text) if text else None
    ctx.check("secrets-private", *judge_secrets(secrets))

    ctx.check("database-private", *judge_database(ctx.form, db_instance(ctx)))

    if ctx.form == "serverless":
        pool = inst.output("Serverless", "IdentityPoolId")
        described = aws("cognito-identity", "describe-identity-pool", "--identity-pool-id", pool)
        roles = aws("cognito-identity", "get-identity-pool-roles", "--identity-pool-id", pool).get("Roles", {})
        endpoint = f"https://cognito-identity.{aws.region}.amazonaws.com/"
        headers = {"Content-Type": "application/x-amz-json-1.1"}
        get_id = unsigned(endpoint, "POST", json.dumps({"IdentityPoolId": pool}).encode(),
                          dict(headers, **{"X-Amz-Target": "AWSCognitoIdentityService.GetId"}))
        developer = unsigned(endpoint, "POST", json.dumps({"IdentityPoolId": pool,
                                                           "Logins": {described.get("DeveloperProviderName", "x"): "uat"}}).encode(),
                             dict(headers, **{"X-Amz-Target": "AWSCognitoIdentityService.GetOpenIdTokenForDeveloperIdentity"}))
        ctx.check("identity-pool-closed", *judge_identity_pool(described, roles, get_id, developer))
