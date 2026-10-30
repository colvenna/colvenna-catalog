# 本仓库专用的测试环境：实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用一条命令在指定 Region 建起一套只属于本仓库的测试环境，一条命令拆掉。这套环境是兼容矩阵、UAT 与 `install.sh` 验证的落脚点，不与 cove-lake 的 uat、uat2 共享。环境包括：

- VPC；
- EKS 1.36，执行 NetworkPolicy；
- 托管节点组；
- 完整的 AWS Load Balancer Controller。

**Architecture:** 测试环境是 `deploy/aws` 这个 CDK 工程里的另一个入口（`test-env/app.ts`），与产品的 `src/app.ts` 分开，产品的栈里不会混进测试设施。EKS 的建法沿用 cove-lake `foundation-stack.ts` 已经验证过的做法：

- L2 `eks.Cluster` 加 kubectl 层；
- API 端点只放行操作机的 /32 地址；
- `system:public-info-viewer` 只对已认证的身份开放；
- VPC CNI 作为托管插件，打开 network policy。

`tests/environment/up.sh` 与 `down.sh` 负责部署与拆除，以及部署后的核对。

**Tech Stack:** aws-cdk-lib 2.274.0、`@aws-cdk/lambda-layer-kubectl-v36` 2.0.1、EKS 1.36、AL2023 节点、AWS Load Balancer Controller chart 3.6.0（chart 按 SHA-256 固定、镜像按 digest 固定，IAM 策略取上游 v3.6.0 的 `iam_policy.json`，按 SHA-256 固定后入库）、`.tools/` 中的 kubectl 与 helm。

**Spec:** [`docs/design/v1.0-design.md`](../../design/v1.0-design.md) 第 9.2 节"环境"（需求方决定：UAT 与兼容矩阵只在本仓库自己的测试环境里运行；两个 Region 各一套，测时建、测完拆）。

## Global Constraints

- **命名**：栈名 `CoveCatalogTest<Name>`，环境名 `<name>` 用 `envName` 的规则校验（`^[a-z][a-z0-9]{0,7}$`）。AWS 资源都打 `Project=cove-catalog`、`Purpose=test-environment` 标签。
- **门禁**：cdk-nag 与 `ForbiddenResources` 同样适用于测试栈。CDK 的 EKS 提供方需要确认的发现，沿用 cove-lake `nag.ts` 的 `EKS_PROVIDERS` 与 `EKS_MANAGED_POLICIES`，写在资源旁。
- **只放行操作机**：EKS 公网端点的 `publicAccessCidrs` 只有操作机的一个公网 IPv4 /32。API 一律要求 IAM 认证；匿名请求 `/version`、`/healthz` 都必须被拒。
- **网络**：节点在私有子网，经一个 NAT 网关出网；S3 走网关端点。
- **数据**：不存任何数据，拆除时全部删掉。删栈前先删光集群里 `type: LoadBalancer` 的 Service，以免留下 NLB 挡住 VPC 的删除。
- **补丁窗口**：测试窗口避开 00:00 UTC 的补丁重启。本机开跑长任务前，先核对它的 `Patch Group` 标签；节点上的标签不改，要改须另行确认。
- **配额与共用**：部署前预检 VPC 与 EIP 的余量。与 cove-lake 的栈同在一个账号，名字互不冲突；不读、不改 cove-lake 的任何资源。
- **交付**：走 `/impl-and-mr`。真实部署所需的 AWS 资源创建与删除，需求方已授权（设计第 9.2 节）。

## Review Focus

1. **API 端点放行范围过宽**：操作机地址必须是单个公网 IPv4 /32，私网、/0 或 IPv6 一律拒绝。用例在 Task 2 的 `exposes the API to the operator's /32 only`。
2. **匿名访问**：public-info-viewer 的补丁必须让 `system:unauthenticated` 失去访问。Task 2 有用例，Task 4 在真实集群上用无凭证的 curl 核对。
3. **拆除留下残余**：NLB、ENI、日志组、EIP 可能残留。Task 4 的 `down.sh` 结束时核对：没有带本环境标签的负载均衡器与网卡，栈已删除。
4. **误操作 cove-lake 的环境**：`down.sh` 只接受 `CoveCatalogTest` 开头的栈名，并核对栈上的 `Purpose=test-environment` 标签后才删除。
5. **NetworkPolicy 实际没有执行**：up.sh 部署后核对 aws-node 的 network policy agent 已启用，并且一条 deny-all 策略确实挡住了 Pod 之间的连接。

---

### Task 1: EKS 的 cdk-nag 确认与 kubectl 层依赖

**Files:**

- Modify: `deploy/aws/package.json`、`package-lock.json`（加 `@aws-cdk/lambda-layer-kubectl-v36` 2.0.1）
- Modify: `deploy/aws/src/nag.ts`（加 `EKS_MANAGED_POLICIES`、`EKS_PROVIDERS`，理由按测试环境改写，去掉 EBS CSI 一项）
- Modify: `deploy/aws/tsconfig.json`（`include` 加 `test-env/**/*.ts`）

- [ ] **Step 1: 加依赖与两个常量**

- [ ] **Step 2: 检查**

  Run: `cd deploy/aws && npm ci && npm run check && npm test`

  Expected: tsc 没有输出，测试数与改动前相同，全部通过。

- [ ] **Step 3: 提交**

### Task 2: 测试环境栈 `TestEnvironmentStack`

**Files:**

- Create: `deploy/aws/test-env/stack.ts`
- Create: `deploy/aws/test-env/lbc-iam-policy.json`：上游 v3.6.0 的原文，SHA-256 为 `16f232c9d9f79366fe949c4550ad517a202380058a9e48d45a4e215044a20a6a`
- Test: `deploy/aws/test/test-env.test.ts`

**Interfaces:**

- Produces：`export interface TestEnvironmentStackProps extends StackProps { name: string; sourceSha: string; availabilityZones: string[]; operatorCidr: string; adminPrincipalArn: string }`；`export class TestEnvironmentStack extends Stack`。
  - **VPC**：`10.85.0.0/16`，两个给定的可用区。公有子网 /24，私有子网 /21（带出网）。一个 NAT 网关，一个 S3 网关端点。
  - **EKS**：
    - 1.36，`authenticationMode: API_AND_CONFIG_MAP`，端点为 `PUBLIC_AND_PRIVATE`；
    - `publicAccessCidrs` 只有 `[operatorCidr]`，用 cove-lake 的属性覆盖写法；
    - API、审计、认证三类日志打开，日志组保留 30 天，栈删除时日志组也删除；
    - 管理员访问条目给 `adminPrincipalArn`，`AmazonEKSClusterAdminPolicy`，集群范围；
    - public-info-viewer 的补丁照 cove-lake。
  - **节点组**：一个，2 个 `t3.large`（`AL2023_X86_64_STANDARD`），磁盘 40 GiB。
  - **插件**：`vpc-cni`（`v1.22.4-eksbuild.3`，`enableNetworkPolicy: 'true'`）、`kube-proxy`（`v1.36.0-eksbuild.48`）、`coredns`（`v1.14.7-eksbuild.11`），版本与 cove-lake 一致。后两个依赖节点组。
  - **LBC 角色**：IRSA 角色 `cove-catalog-test-<name>-lbc`，只信任 `kube-system:aws-load-balancer-controller`，策略取自 `lbc-iam-policy.json` 原文。IAM5 在资源旁确认，理由是"上游为该版本发布的策略，只用于测试环境"。
  - **输出**：`ClusterName`、`OidcIssuer`、`ClusterSecurityGroupId`、`VpcId`、`PrivateSubnetIds`、`PrivateSubnetZones`、`LbcRoleArn`、`SourceSha`。

- [ ] **Step 1: 写 `test-env.test.ts`**

  - `exposes the API to the operator's /32 only`：集群资源的 `publicAccessCidrs` 恰好是 `['198.51.100.7/32']`；构造时传 `10.0.0.1/32`、`198.51.100.0/24`、`0.0.0.0/0` 都要抛错。
  - `admits only authenticated users to public info`：KubernetesPatch 的 `applyPatch` 与 `restorePatch` 都把 subjects 设为 `system:authenticated`。
  - `enforces network policy`：vpc-cni 插件的 `ConfigurationValues` 解析后，`enableNetworkPolicy` 为 `'true'`。
  - `gives the admin principal cluster admin only`：访问条目的 `principalArn` 与 `policyArn` 符合预期，`accessScope` 为 `cluster`。
  - `the load balancer controller's role trusts its service account only`。
  - `lbc policy is the pinned upstream file`：文件的 SHA-256 等于上面那个值。
  - `outputs what install.sh and the tests read`：八个输出都在。
  - `passes cdk-nag and the guard`。

- [ ] **Step 2: 运行，确认失败；实现；再运行，确认通过**（`npm run check && npm test`）
- [ ] **Step 3: 提交**

### Task 3: 测试环境的入口与合成门禁

**Files:**

- Create: `deploy/aws/test-env/app.ts`（`buildTestApp(context)`，上下文键为 `name`、`account`、`region`、`sourceSha`、`zones`、`operatorCidr`、`adminPrincipalArn`；缺键时报出键名）
- Test: `deploy/aws/test/test-env-app.test.ts`
- Modify: `scripts/aws-test.sh`（用占位上下文 `npx cdk -a 'npx ts-node test-env/app.ts' synth --quiet` 合成一次）

- [ ] **Step 1: 测试先失败**

  - 缺任一键时抛错，信息含键名；
  - `adminPrincipalArn` 必须是本账号的 role 或 user ARN；
  - `zones` 必须是本 Region 的两个不同可用区。

- [ ] **Step 2: 实现，通过；`scripts/aws-test.sh` 通过**
- [ ] **Step 3: 提交**

### Task 4: `up.sh` 与 `down.sh`，以及一次真实的起与拆

**Files:**

- Create: `tests/environment/up.sh`、`tests/environment/down.sh`、`tests/environment/lib.sh`
- Create: `tests/environment/README.md`

**Interfaces:**

- `tests/environment/up.sh <name> <region>`：
  1. **预检**：本机 `Patch Group` 标签、工具、身份；该 Region 的 CDK bootstrap、VPC 与 EIP 余量。
  2. **取参数**：选出两个提供 `t3.large` 的可用区（`describe-instance-type-offerings`）。操作机 /32 取自 `https://checkip.amazonaws.com`。管理员 ARN 由调用者的 assumed-role ARN 换算成 role ARN。
  3. **部署**：`cdk deploy`。
  4. **集群访问**：kubeconfig 写到 `.state/test-<name>/kubeconfig`，不碰用户的 `~/.kube/config`。
  5. **装 LBC**：chart 3.6.0 校验 SHA-256，镜像按 digest，打开 Service 控制器，不装 IngressClass。
  6. **核对**：
     - 节点 Ready；
     - aws-node 里 network policy agent 已启用；
     - 在临时命名空间里放一条 deny-all 策略，确认 Pod 之间连不上，然后删掉；
     - 不带凭证 curl API 的 `/version`，得到 401 或 403。
  7. **证据**：写到 `.state/test-<name>/evidence/up-<时间>/`。
- `tests/environment/down.sh <name> <region>`：
  1. 栈名必须以 `CoveCatalogTest` 开头，且带 `Purpose=test-environment` 标签，否则拒绝；
  2. 删光集群里 `type: LoadBalancer` 的 Service，等待 LBC 回收负载均衡器；
  3. 卸载 LBC；
  4. `cdk destroy`；
  5. 核对没有带本环境标签的负载均衡器与网卡残留，栈状态为 `DELETE_COMPLETE`。

- [ ] **Step 1: 写脚本，shellcheck 通过**
- [ ] **Step 2: 在 `us-west-2` 真实执行 `up.sh ct1 us-west-2`**

  Expected: 各项核对全部通过，证据目录里有每项的输出。

- [ ] **Step 3: 执行 `down.sh ct1 us-west-2`**

  Expected: 栈 `DELETE_COMPLETE`，没有残留。

- [ ] **Step 4: 在 `us-east-2` 重复 Step 2 与 Step 3**

  两个 Region 都要能起能拆，这是 UAT 的前提。

- [ ] **Step 5: README 写明用法、费用量级、补丁窗口与配额预检；提交**
