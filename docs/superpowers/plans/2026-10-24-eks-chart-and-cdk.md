# EKS 形态的部署物（一）：通用 chart 与 CDK 栈：实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 EKS 形态备好两样不依赖 cove-lake 的部署物：一个通用的 Helm chart，以及一个独立的 CDK 应用（EKS 身份栈、存储栈、发布仓库栈），二者都有自动测试，并纳入 CI。

**Architecture:** chart 从 cove-lake 原样导入，再做三处通用化：标签、可选的内部 NLB、NetworkPolicy 的 CIDR 放行。CDK 应用沿用 cove-lake `deploy/aws` 的工程结构、依赖版本与 cdk-nag 门禁；三个栈各管一件事，由 `-c stack=<名字>` 选择要合成哪一个。所有输入都经 `src/context.ts` 校验后才能用。chart 的测试与 CDK 的测试同在 `deploy/aws/test/`，用 node 的测试框架（chart 测试调用 `helm template`）。

**Tech Stack:** Helm 3.19.0、kubectl 1.36.5、crane 0.22.1（`scripts/install-tools.sh` 按版本与校验和装进 `.tools/`）；Node 22、TypeScript 5.9.3、aws-cdk-lib 2.274.0、cdk-nag 2.38.2、constructs 10.8.1、aws-cdk 2.1146.0、ts-node 10.9.2、yaml 2.9.1。

**Spec:** [`docs/design/v1.0-design.md`](../../design/v1.0-design.md)：第 6.1 节、第 6.3 节、第 9.1 节"发行物从哪里装"，以及第 3 节关于 cove-lake 原地接管的约束。

## 本计划在路线中的位置

设计稿第 11 节第 2 步（EKS 形态的部署物）拆成两份计划、四个 issue：

- **本计划**：issue A（通用 chart、工具安装与 node 工程骨架）、issue B（CDK 栈）。
- **下一份计划**：issue C（`install.sh` 与 `uninstall.sh`）、issue D（发布清单与私有发布仓库）。C 与 D 要在真实的 EKS 集群上验证，所以要等路线第 3 步（本仓库专用的测试环境）就绪之后再做。
- **路线顺序因此调整为**：2A → 2B → 3 → 2C → 2D → 4……
- **issue C 必须在真集群上验证内部 NLB**：允许的来源能连上、不在 `allowedCidrs` 里的来源被拒；Pod 看到的是客户端地址；健康检查在填入 `healthCheckCidrs` 后变为健康；服务端证书包含 NLB 的 DNS 名。chart 在本计划里只经过渲染测试。

## Global Constraints

- **交付方式**：每个 issue 走 `/impl-and-mr`，各有专用 worktree `.worktrees/issue-<n>-<slug>` 与分支 `codex/issue-<n>-<slug>`。本地全套是 `scripts/ci-local.sh`；新增的 CI 作业要同时加进 `.github/workflows/ci.yml` 和 `scripts/ci-local.sh`，两边作业名一致。
- **版本固定**：依赖版本与 cove-lake `deploy/aws/package.json` 一致；GitHub Actions 按完整 SHA 固定，后面注释版本号；`package-lock.json` 入库，CI 与 `ci-local.sh` 用 `npm ci`。
- **只用 IRSA，不建 OIDC 提供方**：不创建任何 IAM 用户、访问密钥或 `AWS::Lambda::Url`。CDK 应用不创建集群的 OIDC 提供方，只引用它（`arn:aws:iam::<account>:oidc-provider/<issuer 去掉 https://>`）。
- **cdk-nag 是门禁**：每个栈都过 AwsSolutions 检查。确实要放过的发现，在资源旁写明理由加以确认，不能整体关闭。
- **可配置的名字**：Kubernetes 的命名空间、Helm release 名、服务账号名、数据库名与数据库用户名。默认值与 cove-lake 现有的名字相同（`cove-catalog`、`cove-catalog`、`cove-catalog`、`cove_catalog`、`cove_catalog`），这样 cove-lake 切换时可以原地接管。AWS 资源（IAM 角色、Secret、ECR 仓库）属于本仓库的栈，名字以 `cove-catalog-<env>` 开头；cove-lake 切换时新建这些资源，不复用 cove-lake 的。
- **环境名**：`^[a-z][a-z0-9]{0,7}$`；`release` 保留给发布仓库。
- **Region**：只接受标准分区 `^[a-z]{2}-[a-z]+-[1-9]$`，且不以 `cn-` 开头（GovCloud 的名字有四段，自然被排除）。
- **脱敏**：测试与示例里只用 `111122223333`、`example.com`、`bucket` 一类占位。

## Review Focus

1. **NLB 放行范围过宽**：`internalLoadBalancer.allowedCidrs` 为空或含 `0.0.0.0/0`、`::/0` 时，`helm template` 必须失败并说明原因。用例在 Task A3 的 `rejects an open or empty NLB allow list`。
2. **桶前缀写法不一致**：`warehouse`、`warehouse/`、`/warehouse` 必须被同样对待，不能产出访问整个桶的策略。`context.ts` 的 `warehousePrefix` 只接受不带首尾斜杠的写法，用例在 Task A2；EKS 栈的策略资源只到 `<prefix>/*`，用例在 Task B2。
3. **存储栈拆除时连带删掉数据**：桶与数据库都必须在删栈后保留。桶 `RETAIN`；数据库 `SNAPSHOT` 加删除保护。用例在 Task B3 的 `keeps the data when the stack goes`。
4. **发布仓库向任意账号开放**：拉取账号列表不能出现 `*`，也不能有非 12 位的条目；授权只给拉取所需的三个动作。用例在 Task B4。
5. **合成时漏传上下文**：缺必填的 `-c` 值时，合成必须失败，错误信息里写出缺的键名，不能用默认值悄悄补上。用例在 Task B5 的 `app refuses a stack without its context`。

---

## Issue A：通用 chart、工具安装与 node 工程骨架

### Task A1: 固定版本的工具安装

**Files:**

- Create: `scripts/install-tools.sh`（可执行）
- Modify: `.gitignore`（加 `/.tools/`）

**Interfaces:**

- Produces: `scripts/install-tools.sh`，不带参数。把 kubectl v1.36.5、helm v3.19.0、crane v0.22.1 装进仓库根目录的 `.tools/`，每个文件下载后先核对 SHA-256，核对失败就退出并且不留下该文件；已经装好且版本正确时跳过下载。后续脚本用 `PATH="$repo/.tools:$PATH"` 调用这三个工具。

- [ ] **Step 1: 移植脚本**

  以 cove-lake `deploy/scripts/foundation/install-tools.sh`（`SRC` 时的版本）为蓝本，改成独立运行：不 source 任何库，目标目录是 `$(git rev-parse --show-toplevel)/.tools`。版本与 crane 的两个校验和照抄；kubectl 与 helm 的校验和照原样从官方 `.sha256` 文件读取。支持 arm64 与 amd64，其他架构退出 1。

- [ ] **Step 2: 运行并核对**

  Run: `scripts/install-tools.sh && .tools/helm version --short && .tools/kubectl version --client -o json | jq -r .clientVersion.gitVersion && .tools/crane version`

  Expected: 依次打印 `v3.19.0+g…`、`v1.36.5`、`0.22.1`（或带 `v` 前缀）。再运行一次，日志里不应出现任何下载。

- [ ] **Step 3: 提交**（`chore: pinned kubectl, Helm and crane in .tools`，`Refs #3`）

### Task A2: node 工程骨架与上下文校验

**Files:**

- Create: `deploy/aws/package.json`、`deploy/aws/package-lock.json`、`deploy/aws/tsconfig.json`、`deploy/aws/cdk.json`
- Create: `deploy/aws/src/context.ts`
- Test: `deploy/aws/test/context.test.ts`

**Interfaces:**

- Produces（`src/context.ts`，全部是纯函数，参数为 `unknown`，校验通过时返回规范化后的值，否则抛出 `Error`，错误信息以 `-c <key>` 开头）：
  - `envName(v): string`：环境名，规则见 Global Constraints，`release` 被拒绝；
  - `account(v): string`：12 位数字；
  - `standardRegion(v): string`；
  - `commitSha(v): string`：40 位小写十六进制；
  - `oidcIssuer(v, region: string): string`：`https://oidc.eks.<region>.amazonaws.com/id/<32 位大写十六进制>`；
  - `bucketName(v): string`：S3 桶名规则，3–63 位，小写字母、数字、`-`、`.`，首尾为字母或数字，不能是 IP 形式；
  - `warehousePrefix(v): string`：1–64 位，由 `[a-z0-9_-]` 段组成、以 `/` 分隔，**不带首尾斜杠**；
  - `k8sName(v): string`：DNS-1123 label，最长 63 位；
  - `databaseIdentifier(v): string`：`^[a-z_][a-z0-9_]{0,62}$`；
  - `vpcId(v): string`：`vpc-` 加 8 或 17 位十六进制；
  - `subnetIds(v): string[]`：逗号分隔，每个都是 `subnet-` 加 8 或 17 位十六进制，至少两个，不重复；
  - `securityGroupId(v): string`：`sg-` 加 8 或 17 位十六进制；
  - `accountList(v): string[]`：空或未设置时返回 `[]`，否则逗号分隔的 12 位账号，不重复、不能有 `*`；
  - `flag(v): boolean`：只接受 `'true'` 与 `'false'`。
- package.json 的脚本：`check`（`tsc --noEmit`）、`test`（`node --test --require ts-node/register test/*.test.ts`）、`synth`（`cdk synth`）；依赖版本见 Tech Stack。

- [ ] **Step 1: 写 `test/context.test.ts`**

  每个函数至少一组"接受"和一组"拒绝"。必须包含以下用例：

  - `warehousePrefix`：拒绝 `'warehouse/'`、`'/warehouse'`、`''`、`'a//b'`、`'Ware'`，接受 `'warehouse'` 与 `'lake/warehouse'`；
  - `envName`：拒绝 `'release'`；
  - `standardRegion`：拒绝 `'cn-north-1'` 与 `'us-gov-west-1'`；
  - `accountList`：拒绝 `'*'` 与 `'1111222233334'`；`accountList(undefined)` 等于 `[]`；
  - `subnetIds`：拒绝单个子网和重复子网；
  - `oidcIssuer`：拒绝另一个 region 的 issuer。

- [ ] **Step 2: 运行，确认失败**

  Run: `cd deploy/aws && npm ci && npm test`

  Expected: 全部失败，原因是 `Cannot find module '../src/context'`。

- [ ] **Step 3: 实现 `src/context.ts`**

- [ ] **Step 4: 运行，确认通过**

  Run: `npm run check && npm test`

  Expected: tsc 没有输出，`# fail 0`。

- [ ] **Step 5: 提交**（`build(aws): the CDK project and its validated context`）

### Task A3: 导入并通用化 chart

**Files:**

- Create: `deploy/helm/cove-catalog/**`（先从 cove-lake `SRC:deploy/helm/cove-catalog` 原样导入）
- Modify: `Chart.yaml`、`values.yaml`、`templates/_helpers.tpl`、`templates/networkpolicy.yaml`
- Create: `templates/service-nlb.yaml`
- Test: `deploy/aws/test/chart.test.ts`

**Interfaces:**

- **Consumes**：`.tools/helm`（Task A1）。测试通过 `HELM` 环境变量取得 helm 路径，未设置时默认 `<repo>/.tools/helm`。
- **Produces**：
  - chart 的 values 契约（下一份计划里 `install.sh` 用它）：沿用现有的 values 键，另加 `internalLoadBalancer.enabled`（默认 `false`）与 `internalLoadBalancer.allowedCidrs`（默认 `[]`）。
  - 默认值改为：`clientNamespaceLabel.key: cove-catalog/client`、`metricsClientLabel: cove-catalog/monitoring`。
  - `app.kubernetes.io/part-of` 改为 `cove-catalog`。
  - 启用 NLB 时额外渲染一个 Service：`<release>-nlb`，`type: LoadBalancer`，只开 8181 端口（TCP 透传，TLS 仍由服务端终止）。注解为：`service.beta.kubernetes.io/aws-load-balancer-type: external`、`service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: ip`、`service.beta.kubernetes.io/aws-load-balancer-scheme: internal`。这些注解需要集群里装有 AWS Load Balancer Controller，由下一份计划的预检负责核实。
  - NetworkPolicy 对 8181 端口多一条规则，按 `allowedCidrs` 的每一项各给一个 `ipBlock`。
  - （评审后补充）NLB 保留客户端地址（`aws-load-balancer-target-group-attributes: preserve_client_ip.enabled=true`），`loadBalancerSourceRanges` 等于 `allowedCidrs`。新增 `internalLoadBalancer.healthCheckCidrs`，即 NLB 自己的地址，只接受 /32 或 /128，由 `install.sh` 在 NLB 建好后填入，并一同放行到 8181。每个 CIDR 都校验格式，前缀为 0 的一律拒绝。

- [ ] **Step 1: 原样导入，核对 blob**

  ```bash
  git -C <Colvenna Lake 的克隆> archive "$SRC" deploy/helm/cove-catalog | tar -x -C "$WT"
  git -C "$WT" add deploy/helm/cove-catalog
  ```

  导入后按第 1 份计划 Task 1 Step 3 的方法核对 blob 哈希，Expected: `IDENTICAL`。然后单独提交：`import: the cove-catalog chart from cove-lake@<SRC>`。`SRC` 用 cove-lake 当时 `origin/main` 的 SHA，记进 issue。

- [ ] **Step 2: 写 `test/chart.test.ts`（全部先失败）**

  把 cove-lake `deploy/aws/test/charts.test.ts` 中关于 cove-catalog 的两条 spread 用例原样移植过来。必填值沿用那份测试里的 `catalog` 常量；`serviceAccount.roleArn` 用 `arn:aws:iam::111122223333:role/r`。再加下面这些用例：

  - `labels say part-of cove-catalog`：Deployment 与 Service 的 `app.kubernetes.io/part-of` 等于 `cove-catalog`。
  - `admits client and monitoring namespaces by the new default labels`：NetworkPolicy 的两条 `namespaceSelector.matchLabels` 分别是 `{"cove-catalog/client": "true"}` 和 `{"cove-catalog/monitoring": "true"}`。
  - `keeps cove-lake's labels when set`：设 `clientNamespaceLabel.key=cove-lake.io/catalog-client`、`metricsClientLabel=cove-lake.io/monitoring` 后，渲染结果与 cove-lake 现状一致。
  - `renders no load balancer by default`：没有 `type: LoadBalancer` 的 Service，NetworkPolicy 没有 `ipBlock`。
  - `an internal NLB on the API port only`：设 `internalLoadBalancer.enabled=true`、`internalLoadBalancer.allowedCidrs={10.0.0.0/16}`，期望：
    - `t-nlb` 的 `spec.type` 是 `LoadBalancer`，三个注解与上面 Produces 中写的完全一致；
    - `ports` 只有 `{port: 8181, targetPort: https, protocol: TCP}`；
    - NetworkPolicy 多出 `{ipBlock: {cidr: 10.0.0.0/16}}`，只对 8181 端口生效。
  - `rejects an open or empty NLB allow list`：`allowedCidrs` 为空、为 `{0.0.0.0/0}` 或 `{::/0}` 时，`helm template` 的退出码非 0，stderr 含 `internalLoadBalancer.allowedCidrs`。
  - `requires what the server cannot start without`：分别去掉 `image.digest`、`warehouse.root`、`database.url`、`serviceAccount.roleArn`、`region`，退出码非 0，stderr 含该键名。

- [ ] **Step 3: 运行，确认只有新用例失败**

  Run: `cd deploy/aws && npm test`

  Expected:
  - 两条移植来的 spread 用例和 `requires…` 通过，因为现有 chart 已经具备这些行为；
  - 其余新用例失败，失败原因与用例名一致，比如 part-of 仍是 `cove-lake`、找不到 `t-nlb`。

- [ ] **Step 4: 修改 chart**

  - `Chart.yaml` 的 description 改为 `Paimon REST catalog server backed by PostgreSQL.`，版本仍是 `0.1.0`。
  - `values.yaml` 的注释去掉对 cove-lake 安装器与节点组的引用，`eligibleNodes` 改述为"节点选择器允许的节点数，由安装脚本数出"。
  - NetworkPolicy 与新模板的校验用 Helm 的 `fail`，信息为 `internalLoadBalancer.allowedCidrs: …`。

- [ ] **Step 5: 运行，确认全部通过，并跑 helm lint**

  Run: `cd deploy/aws && npm test && ../../.tools/helm lint ../helm/cove-catalog --set image.repository=r --set image.digest=sha256:0 --set warehouse.root=s3://bucket/warehouse --set region=us-west-2 --set database.url=jdbc:postgresql://db/cove_catalog --set serviceAccount.roleArn=arn:aws:iam::111122223333:role/r`

  Expected: `# fail 0`，以及 `1 chart(s) linted, 0 chart(s) failed`。

- [ ] **Step 6: 提交**（`feat(chart): a generic chart: its own labels, an optional internal NLB`）

### Task A4: CI 作业 `aws-test`

**Files:**

- Modify: `.github/workflows/ci.yml`、`scripts/ci-local.sh`、`docs/development.md`、`.gitignore`（加 `node_modules/`、`cdk.out/`）

**Interfaces:**

- Produces: 作业 `aws-test`，在 CI 与 `ci-local.sh` 中做同样的事：
  1. `scripts/install-tools.sh`；
  2. `cd deploy/aws && npm ci && npm run check && npm test`；
  3. 用 Task A3 Step 5 的参数跑 `helm lint`。

  CI 用 `actions/setup-node`（固定 SHA，Node 22，`cache: npm`，`cache-dependency-path: deploy/aws/package-lock.json`）。`ci-local.sh` 的工具检查加上 `node` 与 `npm`。`ci.yml` 顶部注释写明作业清单与 `scripts/ci-local.sh` 保持一致；这也顺手处理了上一份计划暂缓的那条小问题。

- [ ] **Step 1: 改 CI 与本地全套，更新 `docs/development.md` 的 CI 一节**

- [ ] **Step 2: 本地全套**

  Run: `git commit` 之后执行 `scripts/ci-local.sh`

  Expected: `pre-commit`、`shellcheck`、`aws-test`、`verify` 四项都是 PASS，`exit 0`。

- [ ] **Step 3: 提交**（`ci: the aws-test job: the CDK project's checks and the chart's tests`）

---

## Issue B：CDK 栈

### Task B1: cdk-nag 辅助

**Files:**

- Create: `deploy/aws/src/nag.ts`
- Test: `deploy/aws/test/nag.test.ts`

**Interfaces:**

- Produces：
  - `nagChecks(scope)`、`acknowledge(construct, ...findings)`、`objectsUnder(bucket, prefix)`、`acknowledgeLogRetention(stack)`：函数体照搬 cove-lake `deploy/aws/src/nag.ts`。
  - 常量 `RDS_DESIGN`：只含 `RDS3` 与 `RDS11`，理由改写为本产品的说法。不含 `RDS10`，因为数据库会开删除保护。
  - 常量 `MASTER_SECRET`、`SECRET_ROTATION`：理由改写为本产品的说法。
  - 测试辅助 `test/nag-helpers.ts` 的 `unacknowledged(stack): string[]`：返回该栈上 cdk-nag 报出的全部 Error 与 Warning。

- [ ] **Step 1: 写 `nag.test.ts` 的辅助与一条自检用例**

  用例 `reports an unacknowledged finding`：在一个空栈里建一个不带 `enforceSSL` 的 `s3.Bucket`，调用 `nagChecks`，断言 `unacknowledged(stack)` 中含 `AwsSolutions-S10`。

- [ ] **Step 2: 运行，确认失败，然后实现 `nag.ts`，再运行确认通过**（`npm test`，Expected: `# fail 0`）
- [ ] **Step 3: 提交**（`build(aws): cdk-nag as a gate`）

### Task B2: EKS 身份栈 `CatalogEksStack`

**Files:**

- Create: `deploy/aws/src/eks-stack.ts`
- Test: `deploy/aws/test/eks-stack.test.ts`

**Interfaces:**

- Consumes: `context.ts` 的校验函数，以及 `nag.ts`。
- Produces：`export interface CatalogEksStackProps extends StackProps { environment: string; sourceSha: string; oidcIssuer: string; namespace: string; serviceAccount: string; bucketName: string; warehousePrefix: string }`；`export class CatalogEksStack extends Stack`。
  - 栈名由 `app.ts` 定为 `CoveCatalog<Env>Eks`，例如 `CoveCatalogUatEks`。
  - **资源**：
    - **服务端角色** `cove-catalog-<env>-server`：只信任 `system:serviceaccount:<namespace>:<serviceAccount>`，条件里同时有 `aud=sts.amazonaws.com` 与 `sub`。权限是 `s3:GetBucketLocation`、带前缀条件的 `s3:ListBucket` 与 `s3:ListBucketMultipartUploads`（条件为 `s3:prefix` 匹配 `<prefix>` 或 `<prefix>/*`），以及 `<bucket>/<prefix>/*` 上的 `GetObject`、`PutObject`、`DeleteObject`、`AbortMultipartUpload`、`ListMultipartUploadParts`。
    - **数据角色** `cove-catalog-<env>-data`：信任与权限都和服务端角色相同，`maxSessionDuration` 为 12 小时。
    - **数据库用户 Secret** `cove-catalog-<env>/db-user`：用 `generateSecretString` 生成，模板为 `{"username": <数据库用户>}`，键 `password`，40 位、不含标点。
    - **ECR 仓库** `cove-catalog-<env>/cove-catalog`：推送时扫描，tag 不可改，只保留最近 10 个镜像，删栈时连同镜像一起删除。
  - **输出**：`ServerRoleArn`、`DataRoleArn`、`DbUserSecretArn`、`RepositoryUri`、`SourceSha`。
  - **标签**：`Project=cove-catalog`、`Environment=<env>`、`SourceSha=<sha>`。
  - 数据库用户名是 props 的 `databaseUser`（`app.ts` 默认传 `cove_catalog`），由 `databaseIdentifier` 校验。

- [ ] **Step 1: 写 `eks-stack.test.ts`**

  用 `Template.fromStack` 断言：

  - `trusts only the catalog's service account`：两个角色的 AssumeRolePolicyDocument 只有一条 `sts:AssumeRoleWithWebIdentity`，条件键为 `<issuerPath>:aud` 与 `<issuerPath>:sub`，值为 `sts.amazonaws.com` 与 `system:serviceaccount:cove-catalog:cove-catalog`。
  - `reaches the warehouse prefix and nothing else`：对象动作的 Resource 恰好是 `arn:<partition>:s3:::bucket/warehouse/*`；没有任何语句以 `arn:…:s3:::bucket/*` 或 `*` 为对象资源；ListBucket 带着 `s3:prefix` 条件。
  - `the data role allows twelve-hour sessions`：数据角色的 `MaxSessionDuration` 为 43200，服务端角色没有这个属性。
  - `names its resources after the environment`：角色名、Secret 名、仓库名依次是 `cove-catalog-uat-server`、`cove-catalog-uat-data`、`cove-catalog-uat/db-user`、`cove-catalog-uat/cove-catalog`。
  - `has no function URL and no IAM user`：模板里没有 `AWS::Lambda::Url`、`AWS::IAM::User`、`AWS::IAM::AccessKey`。
  - `passes cdk-nag`：`unacknowledged(stack)` 为 `[]`。
  - `rejects a prefix with slashes`：构造时传入 `warehousePrefix: 'warehouse/'`，期望抛错。

- [ ] **Step 2: 运行，确认失败；实现；再运行，确认通过**（`npm run check && npm test`，Expected: `# fail 0`）
- [ ] **Step 3: 提交**（`feat(aws): the EKS form's identities, database user secret and image repository`）

### Task B3: 存储栈 `CatalogStorageStack`

**Files:**

- Create: `deploy/aws/src/storage-stack.ts`
- Test: `deploy/aws/test/storage-stack.test.ts`

**Interfaces:**

- Produces：`export interface CatalogStorageStackProps extends StackProps { environment: string; sourceSha: string; createBucket: boolean; database?: { vpcId: string; subnetIds: string[]; clientSecurityGroupId: string } }`；`export class CatalogStorageStack extends Stack`。栈名 `CoveCatalog<Env>Storage`。`createBucket` 为 false 且没有 `database` 时，构造函数抛错，信息为"nothing to create"。
  - **`createBucket`**：
    - **warehouse 桶**：名字由 CloudFormation 生成；S3 托管加密，阻止一切公开访问，`enforceSSL`，`BUCKET_OWNER_ENFORCED`；未完成的分段上传 7 天后清理；`RemovalPolicy.RETAIN`。
    - **访问日志桶**：warehouse 桶的服务器访问日志写进这个独立的桶（经 `serverAccessLogsBucket`）。它本身保留 365 天、`RETAIN`，并确认 `AwsSolutions-S1`，理由为"它就是访问日志的去处"。
    - **输出**：`BucketName`。
  - **`database`**：
    - **实例**：RDS PostgreSQL 16（`VER_16`），`t4g.small`，20 GiB；存储加密；不可公开访问；放在给定子网；备份保留 7 天；`deletionProtection: true`；`RemovalPolicy.SNAPSHOT`。
    - **参数组**：`rds.force_ssl=1`。
    - **日志**：导出 `postgresql` 日志，保留一年。
    - **主账号**：用户名 `cove_admin`，凭证由 RDS 生成并存进 Secret。
    - **安全组**：只放行 `clientSecurityGroupId` 访问 5432 端口，出站不加规则。
    - **输出**：`DbEndpoint`、`DbPort`、`DbAdminSecretArn`。
  - **cdk-nag 确认**：`RDS_DESIGN`、`MASTER_SECRET` 与日志保留函数都在资源旁确认。

- [ ] **Step 1: 写 `storage-stack.test.ts`**

  - `keeps the data when the stack goes`：两个桶的 `DeletionPolicy` 与 `UpdateReplacePolicy` 都是 `Retain`；数据库实例的是 `Snapshot`，且 `DeletionProtection` 为 true。
  - `the database admits only the cluster`：安全组的入站规则恰好一条，端口 5432，来源是给定的安全组；没有 `0.0.0.0/0`。
  - `the database requires TLS`：参数组的 `rds.force_ssl` 为 `'1'`；`PubliclyAccessible` 为 false；`StorageEncrypted` 为 true。
  - `the warehouse bucket logs access and refuses plain HTTP`：桶有 `LoggingConfiguration.DestinationBucketName`，桶策略里有 `aws:SecureTransport` 为 false 时 Deny 的语句。
  - `bucket only` 与 `database only`：两种组合都能合成，输出只出现对应的那几个。
  - `refuses to create nothing`。
  - `passes cdk-nag`：两种组合下 `unacknowledged` 都为 `[]`。

- [ ] **Step 2: 运行，确认失败；实现；再运行，确认通过**（`npm run check && npm test`，Expected: `# fail 0`）
- [ ] **Step 3: 提交**（`feat(aws): optional warehouse bucket and metadata database, both kept when the stack goes`）

### Task B4: 发布仓库栈 `CatalogReleaseStack`

**Files:**

- Create: `deploy/aws/src/release-stack.ts`
- Test: `deploy/aws/test/release-stack.test.ts`

**Interfaces:**

- Produces：`export interface CatalogReleaseStackProps extends StackProps { sourceSha: string; pullAccounts: string[] }`；`export class CatalogReleaseStack extends Stack`。栈名固定为 `CoveCatalogRelease`，必须给出 `env.account` 与 `env.region`。
  - **资源**：一个 ECR 仓库 `cove-catalog-release/cove-catalog`，推送时扫描，tag 不可改，`RETAIN`，不设生命周期规则。
  - **拉取授权**：`pullAccounts` 非空时加一条仓库策略，`Sid: ReleasePull`，主体为每个账号的 `AccountPrincipal`，动作恰好是 `ecr:BatchGetImage`、`ecr:GetDownloadUrlForLayer`、`ecr:BatchCheckLayerAvailability`。
  - **输出**：`RepositoryUri`。

- [ ] **Step 1: 写 `release-stack.test.ts`**

  - `no policy without pull accounts`。
  - `pull accounts may pull and do nothing else`：动作恰好是上面三个，主体是两个账号的 root，没有 `*`。
  - `rejects a wildcard or a malformed account`：传入 `['*']` 或 `['1']` 时抛错。
  - `keeps every image`：`DeletionPolicy` 为 `Retain`，没有 `LifecyclePolicy`。
  - `passes cdk-nag`。

- [ ] **Step 2: 运行，确认失败；实现；再运行，确认通过**（`npm run check && npm test`）
- [ ] **Step 3: 提交**（`feat(aws): the release registry, pullable by listed accounts only`）

### Task B5: `app.ts` 与合成门禁

**Files:**

- Create: `deploy/aws/src/app.ts`
- Test: `deploy/aws/test/app.test.ts`
- Modify: `.github/workflows/ci.yml`、`scripts/ci-local.sh`（`aws-test` 作业加合成步骤）

**Interfaces:**

- Produces：`app.ts` 只合成 `-c stack=<eks|storage|release>` 指定的那一个栈，并对它启用 `nagChecks`。各栈需要的上下文键：
  - **公共**：`stack`、`account`、`region`、`sourceSha`。
  - **eks**：`env`、`oidcIssuer`、`bucket`、`warehousePrefix`；可选 `namespace`、`serviceAccount`、`databaseUser`，默认 `cove-catalog`、`cove-catalog`、`cove_catalog`。
  - **storage**：`env`、`createBucket`、`createDatabase`；`createDatabase=true` 时还要 `vpcId`、`subnetIds`、`clientSecurityGroupId`。
  - **release**：`pullAccounts`，可以为空。

  缺必填键时抛出 `Error('-c <key> is required for stack <name>')`。另外导出 `buildApp(context: Record<string, string>): App` 供测试调用；`app.ts` 的入口只读取 `process` 的上下文，再调用它。

- [ ] **Step 1: 写 `app.test.ts`**

  - `app refuses a stack without its context`：对三个栈分别去掉一个必填键，比如 eks 去掉 `oidcIssuer`、storage 在 `createDatabase=true` 时去掉 `vpcId`、公共的 `account`，断言抛出的错误信息含该键名。
  - `app builds exactly the stack it is asked for`：`stack=eks` 时 `app.node.children` 中只有 `CoveCatalogUatEks` 一个栈。
  - `unknown stack`：`stack=foo` 时抛错。

- [ ] **Step 2: 运行，确认失败；实现；再运行，确认通过**

- [ ] **Step 3: 合成门禁加进 `aws-test`**

  在 `deploy/aws` 下用占位上下文执行 `npx cdk synth --quiet`，三个栈各一次：

  - 账号 `111122223333`，region `us-west-2`；
  - issuer `https://oidc.eks.us-west-2.amazonaws.com/id/0123456789ABCDEF0123456789ABCDEF`；
  - 桶名 `bucket`；
  - `vpc-0123456789abcdef0`、`subnet-0123456789abcdef0,subnet-0123456789abcdef1`、`sg-0123456789abcdef0`；
  - `sourceSha` 取 `git rev-parse HEAD`。

  cdk-nag 有任何未确认的发现都会让合成失败。CI 与 `ci-local.sh` 同步修改。

- [ ] **Step 4: 本地全套**

  Run: `scripts/ci-local.sh`

  Expected: `pre-commit`、`shellcheck`、`aws-test`、`verify` 四项全部 PASS，`exit 0`。

- [ ] **Step 5: 提交**（`feat(aws): the app synthesizes one validated stack at a time, cdk-nag gating it`）
