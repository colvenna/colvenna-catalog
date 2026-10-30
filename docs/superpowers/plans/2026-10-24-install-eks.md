# EKS 形态的部署物（二）：install.sh 与 uninstall.sh：实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户在自己的账号里，用一份配置文件和一条命令装好 EKS 形态的 Cove Catalog，用另一条命令卸载，默认保留数据。安装可以重复执行，中断后重跑可以恢复，不合法的配置被拒绝；这些都在 #7 的测试环境上实测并留证据。

**Architecture:** 根目录的 `install.sh` 与 `uninstall.sh` 只负责编排，每一步都由 `deploy/lib/` 下的函数完成，各步骤都"先查后做"：

- `config.sh`：读取并校验配置；
- `preflight.sh`：预检；
- `helm.sh`：从 cove-lake 原样移植，负责恢复被中断的 Helm 操作；
- `catalog.sh`：数据库、TLS、chart、NLB、引导管理员、冒烟检查。

AWS 资源由 #4 的 CDK 栈建（storage 按需，eks 必建），集群里的东西由 chart 与脚本建。纯逻辑（配置、参数、预检的判断、chart 参数的组装、Helm 的恢复决策）用 Python unittest 驱动 bash 测试；真实行为在测试环境上验证。

**Tech Stack:** bash、`.tools/` 中的 kubectl、helm、crane；aws CLI v2、docker buildx（`linux/amd64`）、Maven、JDK 21；psql 镜像 `postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`、curl 镜像 `curlimages/curl:8.16.0@sha256:463eaf6072688fe96ac64fa623fe73e1dbe25d8ad6c34404a669ad3ce1f104b6`（两者都与 cove-lake 一致）；Python 3 unittest。

**Spec:** [`docs/design/v1.0-design.md`](../../design/v1.0-design.md) 第 6.1 节、第 6.3 节、第 6.4 节，以及第 9.1 节对安装脚本的完成条件。Issue #8 的范围与接收条件，包括从 #3、#4 评审转来的事项。

## Global Constraints

- **配置契约**：一份本地 env 文件，默认放在 `deploy/config/<env>.env`，`.gitignore` 忽略 `deploy/config/*.env`，但保留 `example.env`。
  - 只接受下面列出的键，出现未知的 `COVE_*` 键就拒绝，防止拼错。
  - 值不做 shell 展开：文件按 `KEY=value` 逐行解析，不 `source`。
- **配置键与默认值**：
  - **必填**：`COVE_ENV`、`COVE_ACCOUNT`、`COVE_REGION`、`COVE_FORM`（1.0 只接受 `eks`）、`COVE_EKS_CLUSTER`。
  - **可选**（括号里是默认值）：`COVE_NAMESPACE`（`cove-catalog`）、`COVE_RELEASE_NAME`（`cove-catalog`）、`COVE_SERVICE_ACCOUNT`（`cove-catalog`）、`COVE_WAREHOUSE_NAME`（`cove`）、`COVE_BUCKET`（空表示新建）、`COVE_WAREHOUSE_PREFIX`（`warehouse`）、`COVE_DB`（`create`）、`COVE_DB_NAME`（`cove_catalog`）、`COVE_DB_USER`（`cove_catalog`）、`COVE_DATA_TOKENS`（`aws-sts`）、`COVE_DATA_TOKEN_DURATION`（`PT3H`，`PT1H30M`–`PT12H`）、`COVE_NODE_SELECTOR`（空，或 `k=v,...`）、`COVE_INTERNAL_NLB`（`off`）、`COVE_CLIENT_NAMESPACE_LABEL`（`cove-catalog/client=true`）、`COVE_METRICS_NAMESPACE_LABEL`（`cove-catalog/monitoring=true`）。
  - **条件必填**：`COVE_DB=existing` 时还要 `COVE_DB_ENDPOINT`、`COVE_DB_PORT`（`5432`）、`COVE_DB_ADMIN_SECRET_ARN`；`COVE_INTERNAL_NLB=on` 时还要 `COVE_NLB_ALLOWED_CIDRS`。
- **账号守卫**：调用者账号必须等于 `COVE_ACCOUNT`，否则在任何写操作之前退出，并写明两个账号。这一条替代了 app.ts 去掉的 `CDK_DEFAULT_ACCOUNT` 检查。
- **本地状态**：写在 `.state/<env>/`（kubeconfig、栈输出、证据、helm 锁），不碰用户的 `~/.kube/config`。
- **不打印秘密**：token、密码只经文件或 Secret 传递，不出现在命令行参数和日志里，做法沿用 cove-lake。
- **退出码**：`0` 成功；`1` 某一步失败，并写明是哪一步、为什么；`2` 参数或配置不合法；`3` 预检拒绝，包括账号不一致、NetworkPolicy 未执行、缺 LBC 等。
- **安全底线**：没有 Lambda Function URL；集群外不暴露任何端点；NLB 只放行 `allowedCidrs`；每个入口都要有未认证请求被拒的证据。
- **交付**：走 `/impl-and-mr`；真实验证在 #7 的测试环境上进行。PR 里的证据只用占位符。

## Review Focus

1. **配置里的拼写错误**，例如 `COVE_INTERAL_NLB=on`：必须拒绝，不能悄悄当成默认值。用例在 Task 1。
2. **账号不一致时什么都不改**：比如凭证是 A 账号，配置写的是 B 账号。用例在 Task 3，用桩命令记录调用；断言除了 `sts get-caller-identity` 之外，没有任何 `aws` 调用。
3. **重复执行改变已有的东西**：数据库用户的密码、私有 CA、管理员 token 在第二次运行时必须保持不变。CA 与 token 只在缺失时新建；密码由 Secrets Manager 的同一个 Secret 提供。Task 6 的实测场景 2 核对 CA 指纹与 token 的 Secret 没有变。
4. **卸载误删数据**：不带 `--delete-data` 时，存储栈、桶与数据库都必须保留，再以 `existing` 模式重装后数据还在。Task 6 的实测场景 5。
5. **NLB 两段安装的中间态**：NLB 刚建好、`healthCheckCidrs` 还没填时，目标不健康，这一点要在日志里写明。证书在重签前不含 NLB 的 DNS 名，所以在完成之前，脚本不能宣告 NLB 可用。Task 6 的实测场景 1。

---

### Task 1: 配置契约与校验

**Files:**

- Create: `deploy/config/example.env`（全部键，注释说明，只用占位符）
- Create: `deploy/lib/config.sh`
- Test: `tests/deploy/test_config.py`
- Modify: `.gitignore`（`deploy/config/*.env` 与 `!deploy/config/example.env`）

**Interfaces:**

- **Produces**：`load_config <file>`。逐行解析 `KEY=value`，忽略空行与 `#` 注释行，校验并补上默认值，然后把每个键导出为同名的 shell 变量。
  - 出错时打印 `config: <KEY>: <原因>` 并 `exit 2`，一次把所有错误都列出来。
  - 校验规则与 `deploy/aws/src/context.ts` 一致：环境名、12 位账号、标准 Region、DNS-1123 名字、PostgreSQL 标识符、桶名、warehouse 前缀、ISO-8601 时长与范围、CIDR（前缀长度大于 0）。
  - EKS 集群名：`^[0-9A-Za-z][A-Za-z0-9_-]{0,99}$`。
- **测试框架**：`tests/deploy/helpers.py` 的 `run_bash(script, env=None, path_prepend=None) -> CompletedProcess`，用 `bash -c` 先 source `deploy/lib/*.sh` 再运行一段脚本。

- [ ] **Step 1: 写 `test_config.py`**

  - **合法配置**：example 补上必填值后能加载，默认值都补上了。
  - **缺必填键**：每个必填键各缺一次，退出码 2，stderr 含键名。
  - **未知键**：`COVE_INTERAL_NLB=on` 退出码 2，stderr 含 `unknown key`。
  - **不做展开**：值 `$(touch /tmp/x)` 不被执行，结果是校验失败。
  - **条件必填**：`COVE_DB=existing` 而缺 endpoint，以及 `COVE_INTERNAL_NLB=on` 而缺 CIDR，都退出码 2。
  - **非法值**：`cn-north-1`、`warehouse/`、`PT1H`（短于 `PT1H30M`）、`PT13H`、`0.0.0.0/0`、`COVE_FORM=serverless`（1.0 的 EKS 安装只接受 `eks`）都被拒绝。
  - **一次列全**：两个错误同时出现时，两个都报出来。

- [ ] **Step 2: 运行，确认失败；实现；再运行，确认通过**

  Run: `python3 -m unittest discover -s tests/deploy -v`

- [ ] **Step 3: CI 作业 `deploy-test`**

  在 `ci.yml` 与 `ci-local.sh` 中各加一份，运行 `python3 -m unittest discover -s tests/deploy`。

- [ ] **Step 4: 提交**

### Task 2: 移植 `helm_upgrade` 及其测试

**Files:**

- Create: `deploy/lib/helm.sh`：从 cove-lake `deploy/scripts/lib/helm.sh`（`SRC`）原样移植，只把注释里的路径与 `STATE_DIR` 的来源改为本仓库的说法
- Test: `tests/deploy/test_helm_upgrade.py`：从 cove-lake `tests/acceptance/test_helm_upgrade.py` 移植，路径改为本仓库

- [ ] **Step 1: 先放测试，确认失败**（找不到 `deploy/lib/helm.sh`）；**再放实现，确认通过**
- [ ] **Step 2: 提交**（说明来源 SHA）

### Task 3: 预检

**Files:**

- Create: `deploy/lib/preflight.sh`
- Test: `tests/deploy/test_preflight.py`：在 PATH 前面放桩命令 `aws`、`kubectl`，按参数返回预先写好的 JSON，并把每次调用记进文件

**Interfaces:**

- **Produces**（每个函数通过时 `return 0`，拒绝时打印原因并 `exit 3`）：
  - `assert_account`：`sts get-caller-identity` 的账号等于 `COVE_ACCOUNT`。
  - `cluster_facts`：读取集群，导出 `CLUSTER_ISSUER`、`CLUSTER_SECURITY_GROUP`、`CLUSTER_VPC`、`CLUSTER_SUBNETS` 与 `CLUSTER_SUBNET_ZONES`。后两者取自 `describe-subnets`，按子网顺序一一对应。集群状态不是 `ACTIVE` 时拒绝。
  - `assert_oidc_provider`：IAM 里有这个 issuer 的 OIDC 提供方，没有就拒绝，并给出建立它的命令。
  - `network_policy_enforced`：满足任一即视为执行。
    - aws-node 中镜像为 `aws-network-policy-agent` 的容器带 `--enable-network-policy=true`；
    - 集群里有 `calico-node` 或 `cilium` 的 DaemonSet。

    不执行时，除非给了 `--allow-unenforced-network-policy`，否则拒绝。
  - `assert_lbc`：`COVE_INTERNAL_NLB=on` 时，`kube-system` 里必须有 `aws-load-balancer-controller` 这个 Deployment。
  - `eligible_nodes`：数出 `COVE_NODE_SELECTOR` 匹配的 Ready 节点，至少 1 个，并打印数目。
  - `assert_bucket`：给出的桶存在，且在 `COVE_REGION`。
  - `assert_db_admin_secret`：`COVE_DB=existing` 时，管理员 Secret 可读，且含 `username` 与 `password`；只做 describe，不读值。

- [ ] **Step 1: 写测试**

  - `account mismatch refuses before anything else`：调用记录里只有 `sts get-caller-identity`。
  - **NetworkPolicy 检测**：agent 关闭、agent 缺失、Calico、Cilium，以及给了允许参数，五种情况各判一次。
  - **集群状态**：`CREATING` 时拒绝。
  - **子网与可用区**：一一对应。
  - **缺 OIDC 提供方**：提示里含 `associate-iam-oidc-provider`。

- [ ] **Step 2: 运行，确认失败；实现；再运行，确认通过**
- [ ] **Step 3: 提交**

### Task 4: 安装 catalog 的各个步骤

**Files:**

- Create: `deploy/lib/catalog.sh`
- Test: `tests/deploy/test_catalog.py`：只测纯函数

**Interfaces:**

- **Produces**：
  - `chart_args`：从配置与栈输出组装 `helm upgrade` 的 `--set` 参数，逐行打印。纯函数，可以测试。
    - 需要的值：`image.repository/digest`、`region`、`environment`、`warehouse.name/root`、`database.url`（`sslmode=verify-full&sslrootcert=/etc/cove-catalog/db-ca/ca.pem`）、`databaseCaConfigMap`、`serviceAccount.name/roleArn`、`dataTokens.*`、`eligibleNodes`、`nodeSelector`、两个命名空间标签；
    - 开 NLB 时还要 `internalLoadBalancer.enabled`、`allowedCidrs`，以及 `healthCheckCidrs`（第二段才有）。
  - `ensure_database`：用 psql Pod 建角色与库（幂等），写 Secret `cove-catalog-db`。做法照 cove-lake 的 `install-catalog.sh`。
  - `ensure_tls <extra-dns-names...>`：CA 只在缺失时建，服务证书每次重签，SAN 包括 Service 的各个 DNS 名与传入的额外名字。
  - `nlb_addresses <service>`：读出 Service 的主机名，找到对应的负载均衡器与它的网卡，按 `<ip>/32` 逐行打印。
  - `ensure_admin`：Secret `cove-catalog-admin-token` 缺失时才签发；给管理员一条 MANAGE 授权；建 `default` 库。
  - `smoke <evidence-dir>`：在带客户端标签的临时命名空间里，未认证请求得 401，管理员请求得 200；在不带标签的命名空间里连不上（NetworkPolicy 执行时）。
  - 管理 API 的辅助函数 `admin_open`、`admin_call`、`ensure_grant`、`admin_close`，从 cove-lake 的 `catalog-admin.sh` 移植。

- [ ] **Step 1: 测 `chart_args`**

  - 默认配置；
  - `COVE_DATA_TOKENS=disabled` 时，不出现任何 `dataTokens.roleArn`；
  - 开 NLB 的第一段（只有 `allowedCidrs`）与第二段（加上 `healthCheckCidrs`）；
  - 节点选择器多个键值对；
  - `database.url` 的格式。

  先失败、后通过。
- [ ] **Step 2: 实现其余函数**（它们只在 Task 6 实测），shellcheck 通过
- [ ] **Step 3: 提交**

### Task 5: `install.sh` 与 `uninstall.sh`

**Files:**

- Create: `install.sh`、`uninstall.sh`（根目录，可执行）
- Test: `tests/deploy/test_entrypoints.py`

**Interfaces:**

- **`install.sh --config <file> --from-source [--yes] [--allow-unenforced-network-policy]`**，`--help` 打印用法。
  - `--release <候选>` 留给 #9：现在给出时，退出码 2，提示"available with #9"。
  - **执行顺序**：
    1. 读配置；
    2. 安装工具；
    3. 预检；
    4. 打印目标（账号、Region、集群、命名空间、版本 = 提交 SHA），确认；
    5. 存储栈（按需）与 EKS 栈；
    6. 构建并推送镜像：要求工作区干净，tag 为提交 SHA；tag 已存在时核对 digest 一致；
    7. 命名空间（PSS restricted）；
    8. `ensure_database`；
    9. `ensure_tls`；
    10. `helm_upgrade`；
    11. 开 NLB 时：`nlb_addresses`，写进 `healthCheckCidrs` 再升级一次，重签含 NLB DNS 名的证书并滚动重启，等目标组变健康；
    12. `ensure_admin`；
    13. `smoke`。
  - 每一步开始前打印步骤名，出错时报出步骤名。
- **`uninstall.sh --config <file> [--delete-data] [--yes]`**：
  - **默认执行**：先删 NLB 所在的 Service 并等负载均衡器回收，再 `helm uninstall`，删命名空间里本脚本建的 Secret 与 ConfigMap，然后 destroy EKS 栈；
  - **默认保留**：存储栈不动，CA 的 Secret 也留着，命名空间不删，因为里面可能还有用户的客户端；
  - **`--delete-data`**：再要求输入一次环境名确认；然后关掉 RDS 的删除保护，直接删实例（不留最终快照），清空并删除两个桶，最后 destroy 存储栈。
- **测试**：
  - `--help` 退出 0；
  - 缺 `--config` 或同时给 `--from-source` 与 `--release` 时退出 2；
  - `--release` 现在退出 2 并提示 #9；
  - 不带 `--yes` 且标准输入不是终端时，不确认就退出 2；
  - `--delete-data` 时输错环境名，就什么都不删。

- [ ] **Step 1: 测试先失败；实现；通过；shellcheck 通过**
- [ ] **Step 2: 提交**

### Task 6: 在测试环境上实测

用 `tests/environment/up.sh ct1 us-west-2` 起环境，配置文件放在 `.state/` 下，不入库。证据写到 `.state/<env>/evidence/`，PR 里只贴脱敏的摘要。

- [ ] **场景 1，从零安装**：`COVE_DB=create`、桶留空、`COVE_INTERNAL_NLB=on`，`allowedCidrs` 只填一个可用区的节点子网。核对：
  - 冒烟检查通过；
  - 从这个可用区节点上的 hostNetwork Pod 经 NLB 连上（200），从另一个可用区节点上的被拒；
  - Pod 日志里看到的源地址是客户端地址；
  - 目标组在 `healthCheckCidrs` 填入之后变健康；
  - `openssl s_client` 看到证书的 SAN 含 NLB 的 DNS 名；
  - 用 Flink 或 Spark 的最小流程（从 cove-lake 的流程中取一段）建表、写入、读出；
  - 拿到按表凭证；
  - 确认 ListBucket 的裸前缀是否必要：去掉它跑一遍，按结果决定保留与否，并记一条 Ruling。
- [ ] **场景 2，重复执行**：再运行一次，没有错误；CA 的指纹、管理员 token 的 Secret、数据库用户都不变。
- [ ] **场景 3，中断恢复**：在 `helm_upgrade` 期间杀掉进程，然后重跑，能恢复并完成。
- [ ] **场景 4，配置被拒**：
  - 账号不一致：退出码 3，没有任何写操作；
  - Region 为 `cn-north-1`：退出码 2；
  - 集群不存在：退出码 3；
  - 拼错的键：退出码 2。
- [ ] **场景 5，卸载保留数据**：
  - 先建一个库和一张表；
  - `uninstall.sh` 之后，存储栈、桶与数据库都在；
  - 以 `COVE_DB=existing`（指向保留下来的数据库）加 `COVE_BUCKET` 重装，库和表都还在。
- [ ] **场景 6，`--delete-data`**：桶、数据库与两个栈都删掉了，没有残留。
- [ ] **收尾**：`tests/environment/down.sh ct1 us-west-2`，然后 `scripts/ci-local.sh` 全部 PASS，再提交。
