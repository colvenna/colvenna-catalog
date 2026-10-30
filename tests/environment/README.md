# Test environments

The repository's own environments for the compatibility matrix, UAT and installer tests. They are never shared with other repositories' environments. Bring one up for a test window and take it down afterwards: nothing in it is kept.

```bash
AWS_PROFILE=<profile> tests/environment/up.sh <name> <region>     # e.g. ct1 us-west-2
AWS_PROFILE=<profile> tests/environment/down.sh <name> <region>
```

`<name>` follows environment naming (a lowercase letter, then lowercase letters and digits, eight at most). The stack is `ColvennaCatalogTest<Name>` (`deploy/aws/test-env/stack.ts`); its local state (kubeconfig, outputs, evidence) is in `.state/test-<name>-<region>/`, which git ignores.

## What up.sh makes and proves

- A VPC in two zones that offer `t3.large` and `t3.xlarge` and that EKS supports, with private nodes behind one NAT gateway, an S3 gateway endpoint and a week of flow logs. Its subnet groups: `public` (the NAT gateway), `nodes` (everything of the cluster selects these by name) and `clients` (below).
- EKS 1.36. Its public endpoint admits only this host's public address (a /32, from `checkip.amazonaws.com`), and every request needs IAM authentication. The caller's role is the cluster admin.
- Two node groups, told apart by the label `colvenna-catalog/test-pool` (outputs `CatalogNodegroup`, `ClientNodegroup`):
  - **the catalogs' nodes**: two `t3.large`, as the [performance terms](../../docs/performance/terms.md) (section 2) name them, untainted. Only the EKS installs' pods and the per-node DaemonSets run on them, so that each node keeps room for a rolling update's extra catalog pod ([UAT cases](../../docs/uat/cases.md), section 3.2, has the arithmetic);
  - **the clients' nodes**: two `t3.xlarge`, tainted `colvenna-catalog/test-pool=clients:NoSchedule`, which no catalog pod tolerates. Everything else runs there: the engines, probes, test drivers and load, and the cluster's CoreDNS and the AWS Load Balancer Controller. A client's pod selects them and tolerates the taint: the stack's outputs `ClientNodeSelector` and `ClientNodeTaint`, which the UAT harness gives every pod it makes and the compatibility matrix takes as `--node-selector` and `--toleration`. For a heavy window the group may be raised by hand to four nodes (`aws eks update-nodegroup-config`); verify.sh asks only for its minimum.
- The VPC CNI with its network policy agent on.
- The AWS Load Balancer Controller v3.6.0: the chart is checked by SHA-256, the image is pinned by digest, and the IAM policy is upstream's for that version, kept verbatim.

And what the UAT cases need of it besides ([UAT cases](../../docs/uat/cases.md), section 3.1), each named by a stack output:

- **An `execute-api` interface endpoint** with private DNS in the nodes subnets, admitting HTTPS from the VPC (`ApiEndpointId`). Pods reach the serverless installs' private APIs through it: each install lists it in `COLVENNA_API_ENDPOINT_IDS`. It is also the endpoint of a serverless install into this VPC (`COLVENNA_NETWORK=existing`).
- **An existing database and bucket**, as a user would bring them: RDS PostgreSQL 16 (`db.t4g.small`, single-AZ, encrypted, TLS only, IAM database authentication on), its port open to the VPC only, its master user `colvenna_test_admin` in the generated secret (`colvenna-catalog-test-<name>/existing-db-admin`); and a private bucket (`ExistingDbEndpoint`, `ExistingDbPort`, `ExistingDbInstanceId`, `ExistingDbAdminSecretArn`, `ExistingDbSecurityGroupId`, `ExistingBucket`). The UAT's existing installs (E3, S3) take them with `COLVENNA_DB=existing`, `COLVENNA_DB_ADMIN_SECRET_ARN` and `COLVENNA_BUCKET`, each with a database, user and warehouse prefix of its own (`COLVENNA_DB_NAME`, `COLVENNA_DB_USER`, `COLVENNA_WAREHOUSE_PREFIX`).
- **Client subnets** outside the cluster's: one `/24` in each zone, isolated, routed to S3 through the gateway endpoint and nowhere out of the VPC, with a security group for their clients that admits nothing and may reach the VPC and HTTPS (`ClientSubnetIds`, `ClientSubnetCidrs`, `ClientSubnetZones`, `ClientSecurityGroupId`; the nodes subnets' CIDRs are `NodeSubnetCidrs`). UAT-DEP-11's client of the internal NLB is the test driver (`tests/serverless/driver`), which its harness deploys there and removes.

Before it reports success, up.sh (its last step, `verify.sh`, which also runs on its own against a running environment) checks:

- the catalogs' two nodes are Ready and untainted, and the clients' nodes are Ready, at least their node group's minimum (two), each with the taint; every node is of one of the two groups;
- each catalogs' node's room (allocatable CPU less the DaemonSets' and the installs' requests) is recorded in `catalog-nodes.txt`, with a warning for a pod there that requests CPU and is neither an install's nor a DaemonSet's;
- NetworkPolicy is enforced in fact, as the installs' policies work, for pods made after the policy too: a server on a catalogs' node admits one labelled namespace; a client on a clients' node, made before the policy in a namespace without the label, reaches the server and then cannot; a client made after the policy in the labelled namespace gets through once the agent has applied the policy to it, which verify.sh waits for (up to 3 minutes) and records. When the network policy agent is off, it says so, and how to turn it on;
- anonymous requests to the API's `/version` and health paths are refused;
- the execute-api endpoint is available with private DNS, and an API's name resolves from a pod to its addresses inside the VPC;
- the existing database is available, private, encrypted and has IAM database authentication on; the existing bucket blocks every kind of public access;
- the client subnets are none of the cluster's, route to S3 through the gateway endpoint, and route nowhere out of the VPC.

Rerunning it updates the stack in place: an environment made before the additions keeps its cluster, nodes and subnets, and gains them.

## Switching NetworkPolicy enforcement off and on

UAT-DEP-05 needs a cluster that does not enforce NetworkPolicy, to see `install.sh` refuse it:

```bash
AWS_PROFILE=<profile> tests/environment/network-policy.sh <name> <region> off      # enforcement off
AWS_PROFILE=<profile> tests/environment/network-policy.sh <name> <region> status   # which it is
AWS_PROFILE=<profile> tests/environment/network-policy.sh <name> <region> on       # on again, and verify.sh
```

Each switch sets the VPC CNI managed add-on's `enableNetworkPolicy`, waits for the update and for `aws-node` to roll out on every node, and reads the network policy agent's `--enable-network-policy` back, which is what `install.sh`'s preflight reads.

The switch does not reach pods that are already running, as #70 observed in us-west-2 on 2026-10-29: after `off` they keep filtering with the eBPF programs the agent attached before, and those programs no longer learn of new pods, so that a client made since is dropped. So each switch then starts every install's catalog pods again, a rolling restart of each Deployment labelled `app.kubernetes.io/name=colvenna-catalog`, one after the other:

- after `off`, the catalog pods run without the programs, and nothing filters traffic to them or to any pod made from now on. Another pod that a NetworkPolicy selected before the switch still filters as it did until it starts again: restart it too (`kubectl rollout restart`) if a test needs it unfiltered;
- after `on`, the catalog pods start with the programs attached afresh, and `verify.sh` proves enforcement in fact. It also waits until the policy applies to a client made after the switch, which takes about a minute.

Each restart is a rolling update, which needs room on the catalogs' nodes for one more catalog pod: switch only while at most two installs are in the cluster ([UAT cases](../../docs/uat/cases.md), section 3.2). Only a stack named `ColvennaCatalogTest<Name>` and tagged `Purpose=test-environment` is switched. While it is off, every install in the cluster is protected by authentication only: switch it on again before anything else runs.

## Before a run

- **Patch window.** up.sh refuses to run while this host carries the `Patch Group` tag, which reboots it at 00:00 UTC. Plan test windows away from 00:00 UTC; the nodes' own tags are not changed by these scripts.
- **Region setup.** The region must be CDK-bootstrapped, with one VPC and one Elastic IP to spare for a new environment; up.sh checks both.
- **Cost.** An environment costs about $0.71 an hour while it runs (us-west-2 on-demand prices): the EKS control plane ($0.10), two `t3.large` nodes (about $0.17), two `t3.xlarge` nodes for the clients (about $0.33), the NAT gateway (about $0.05), the database (`db.t4g.small` and 20 GB of gp3, about $0.04), the execute-api endpoint in two zones ($0.02), and a little data. Take it down when the window ends.

## What down.sh removes

down.sh deletes only a stack named `ColvennaCatalogTest<Name>` that is tagged `Purpose=test-environment`. It removes things in this order:

1. every Service of type LoadBalancer, waiting until the controller has deleted their load balancers;
2. the controller;
3. every object in the existing bucket, whatever the existing installs left there;
4. the stack, the database without a final snapshot and without its automated backups among it.

It then checks that nothing is left: the stack, the VPC (so its subnets and endpoints), any load balancer resource, the cluster's and the stack's functions' log groups, the database, its snapshots and automated backups, the bucket, and the database's admin secret, which it deletes without a recovery window if the stack's deletion only scheduled it.
