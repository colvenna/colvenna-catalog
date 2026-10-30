# 从 cove-lake 导入 cove-catalog：实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 cove-lake 里的 cove-catalog 服务端（代码、单元测试、上游契约测试、LLD）不带历史地导入本仓库根目录，并建立本仓库自己的 CI 与本地全套检查，使 `mvn -B clean verify` 在本仓库独立通过。

**Architecture:** 用 `git archive` 从 cove-lake 的一个固定提交（下称 `SRC`）导出 `cove-catalog/`，原样放到本仓库根目录，用 blob 哈希核对内容一致；随后只改与路径、链接、CI 名称相关的文字。CI 沿用 cove-lake 的 pre-commit 与 Maven 作业；Actions 跑不了时，以 `scripts/ci-local.sh` 在确切的 SHA 上执行全部作业并留记录。

**Tech Stack:** Java 21、Maven 3.8+（Error Prone、spotless）、Testcontainers（需要 Docker）、pre-commit 4.6.0、markdownlint-cli2 v0.23.3、actionlint v1.7.12、GitHub Actions。

**Spec:** [`docs/design/v1.0-design.md`](../../design/v1.0-design.md)（第 3、4 节与第 11 节第 1 步）。

## 本计划在 1.0 路线中的位置

设计稿第 11 节的每一步是一个独立的子项目，各自开 issue、写计划、按 `/impl-and-mr` 交付。本计划只覆盖第 1 步，其余轮到时再写计划：

1. **导入**（本计划）
2. EKS 形态的部署物：通用 chart、独立 CDK 应用（两种模式）、`install.sh`、发布清单与私有发布仓库
3. 本仓库专用的测试环境：两个 Region 的测试专用栈，测时建、测完拆
4. 兼容矩阵框架与 Flink、Spark、Doris、StarRocks 的格子（EKS 形态）
5. 无服务器形态的 spike（第 6.2 节五条硬约束），按设计稿规则定 1.0 的范围
6. 无服务器形态的实现与兼容矩阵扩展（取决于第 5 步）
7. 文档：用户手册、部署指南、管理 API 的 OpenAPI、费用估算与实测、英文 README、LLD 改为自足、NOTICE
8. UAT 用例与性能口径、`v1.0.0-rc.N`、两个 Region 的执行、报告与发布结论
9. v1.0.0：tag、仓库转 public、公共镜像与 chart
10. cove-lake 切换（在 cove-lake 仓库，cove-lake 发布之后）

## Global Constraints

- Java 21；服务端依赖保持 Paimon 2.0.0，导入时不升级任何依赖。
- 名字不改：`COVE_*` 环境变量、`com.covelake` 包名、`/cove/admin/v1` 管理路径。
- 不带 git 历史：导入是一个新提交，提交信息写明 `cove-lake@<SRC 完整 SHA>`。
- 导入的 Java 与 SQL 文件内容与 `SRC` 逐字节一致；本计划不改任何 Java 代码。
- 文档、提交信息、PR 里只写占位符（`<ACCOUNT_ID>`、`example.com`、`<ARN>`），不写账号 ID、ARN、真实域名与内网地址。测试代码里已有的 `111122223333` 是 AWS 文档的示例账号，可以保留。
- Maven 的结论只看日志里的 `[INFO] BUILD SUCCESS`，不用 `mvn -q` 加 grep 判断。
- 交付走 `/impl-and-mr`：先在本项目早期的私有仓库（当时名为 `cove-catalog`）开 issue（下称 `#N`），在专用 worktree `WT=<本仓库的克隆>/.worktrees/issue-<N>-import` 上工作，评审、CI（或本地全套）通过后按 SHA 合并。所有命令都用绝对路径或先 `cd "$WT"`，不要写进主 checkout。
- `SRC` 与 `WT` 是 shell 变量，跨任务不会保留：`SRC` 的值以 issue `#N` 描述里记下的为准，每个任务开头重新赋值。

## Review Focus

1. **导入内容和 SRC 不一致**（漏文件、多文件、换行或编码被改）：Task 1 的 Step 3 用 blob 哈希逐个比对，必须没有差异。
2. **混入敏感值**（真实账号 ID、ARN、内网地址）：Task 1 的 Step 4 与 Task 2 的 Step 4 各扫描一次，只允许出现文档示例账号 `111122223333`。
3. **只在本机能过**（依赖 `~/.m2` 里 cove-lake 其他模块的产物，或者缺 Docker 时静默跳过 Testcontainers）：Task 1 的 Step 6 用 `clean verify`，并核对测试总数；`ci-local.sh` 没有 Docker 时直接拒绝运行。
4. **测试跑完留下文件**（例如日志目录、`target/` 以外的产物）：Task 1 的 Step 7 与 `ci-local.sh` 都检查工作区在作业后仍然干净。
5. **LLD 的相对链接在新仓库里失效**：Task 2 的 Step 3 列出全部相对链接，逐个确认目标文件存在。

---

### Task 1: 导入服务端工程

**Files:**

- Create: `pom.xml`、`Dockerfile`、`src/**`（来自 cove-lake 的 `SRC:cove-catalog/`）
- Create: `.gitignore`
- Modify: `Dockerfile` 第 2 行的构建说明

**Interfaces:**

- Produces: `SRC`（cove-lake 的完整提交 SHA，记在 issue `#N` 描述里），Task 2 与提交信息都要用；仓库根目录的 Maven 工程，`mvn -B clean verify` 是后续所有子项目的基本检查。

- [ ] **Step 1: 固定来源提交**

```bash
git -C <Colvenna Lake 的克隆> fetch -q origin main
SRC=$(git -C <Colvenna Lake 的克隆> rev-parse origin/main); echo "$SRC"
git -C <Colvenna Lake 的克隆> log -1 --format='%H %ad %s' --date=short "$SRC"
```

Expected: 打印 40 位 SHA 与它的提交说明。把 SHA 记进 issue `#N` 的描述。

- [ ] **Step 2: 导出到 worktree 根目录**

```bash
git -C <Colvenna Lake 的克隆> archive "$SRC" cove-catalog | tar -x --strip-components=1 -C "$WT"
cd "$WT" && git add pom.xml Dockerfile src
```

Expected: `git status --short` 只列出 `A  pom.xml`、`A  Dockerfile` 与 `A  src/...`，共 62 个文件（以 `SRC` 的 `git ls-tree -r` 计数为准）。

- [ ] **Step 3: 用 blob 哈希核对内容一致**

```bash
diff <(git -C <Colvenna Lake 的克隆> ls-tree -r "$SRC" cove-catalog \
        | awk '{print $3, substr($4, length("cove-catalog/") + 1)}' | sort) \
     <(git -C "$WT" ls-files -s pom.xml Dockerfile src | awk '{print $2, $4}' | sort) \
  && echo IDENTICAL
```

Expected: `IDENTICAL`。

- [ ] **Step 4: 扫描敏感值**

```bash
cd "$WT" && grep -rnoE '\b[0-9]{12}\b' pom.xml Dockerfile src | grep -v ':111122223333$' ; \
grep -rnE 'arn:aws[a-z-]*:iam::[0-9]{12}' src | grep -v '111122223333'
```

Expected: 两条命令都没有输出。

- [ ] **Step 5: 加 `.gitignore`，改 Dockerfile 的构建说明**

`.gitignore` 包含 `/.worktrees/`、`target/`、`/.ci-local/`、`.DS_Store`、`.idea/`、`*.swp`。

`Dockerfile` 第 2 行改为：`# emulation. Build after \`mvn package\`.`。其余行不动。

- [ ] **Step 6: 构建并核对测试数**

```bash
cd "$WT" && mvn -B clean verify > /tmp/issue-<N>-verify.log 2>&1; echo "exit=$?"
grep -E '^\[(INFO|WARNING)\] Tests run: [0-9]+, Failures: [0-9]+, Errors: [0-9]+, Skipped: [0-9]+$|CoveCatalogContractTest|BUILD (SUCCESS|FAILURE)' /tmp/issue-<N>-verify.log | tail -4
```

Expected: `exit=0`，`[INFO] BUILD SUCCESS`，总数行 `Failures: 0, Errors: 0`。以 cove-lake `6ded0b7b` 为例，实测是 `Tests run: 209, Failures: 0, Errors: 0, Skipped: 11`，其中 `CoveCatalogContractTest` 是 `Tests run: 107, … Skipped: 11`。把本次 `SRC` 的实测数（契约测试的运行数、跳过数，以及由此算出的通过数）记下来，Task 2 要用。

- [ ] **Step 7: 工作区干净**

Run: `cd "$WT" && git status --short --untracked-files=all | grep -v '^A '`

Expected: 只有 `AM Dockerfile` 与 `?? .gitignore` 两行，没有其他文件，`target/` 不出现。

- [ ] **Step 8: 提交**

```bash
cd "$WT" && git add .gitignore Dockerfile && git commit -F - <<EOF
import: cove-catalog server from cove-lake@$SRC

The cove-catalog module of cove-lake at $SRC, moved to the repository
root without its history: pom.xml, Dockerfile, the server, its unit
tests and the upstream Paimon 2.0.0 contract tests. Contents are
byte-identical to the source; only the Dockerfile's build note changes,
since the module is now the root.

Refs #<N>

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```

### Task 2: 导入 LLD，更新契约测试的实测数

**Files:**

- Create: `docs/design/cove-catalog-lld.md`（来自 `SRC:docs/design/cove-catalog-lld.md`）
- Modify: `README.md`（文档列表加 LLD）
- Modify: `docs/product/press-release.md`、`docs/design/v1.0-design.md`（契约测试数）

**Interfaces:**

- Consumes: Task 1 的 `SRC`，以及 Step 6 记下的契约测试数。
- Produces: `docs/design/cove-catalog-lld.md`，后续子项目以它为服务端行为的依据。

- [ ] **Step 1: 导出 LLD**

```bash
git -C <Colvenna Lake 的克隆> show "$SRC:docs/design/cove-catalog-lld.md" > "$WT/docs/design/cove-catalog-lld.md"
```

- [ ] **Step 2: 只改以下几处**

1. **frontmatter**：`issue: 18` 换成 `imported_from: "cove-lake@<SRC>"`；`status`、`date` 不变。
2. **一级标题下加一段引用**：
   > 本文导入自 cove-lake@`<SRC>`。指向 cove-lake 的链接都固定在这个提交上；v1.0 公开之前，由文档子项目改成本仓库自足的说明。
3. **`](../../cove-catalog)` 改为 `](../../src)`**，所在句子的 `` `cove-catalog/` `` 改为 `` `src/` ``。
4. **`hld-realtime-lakehouse.md#…`、`../history/m2/catalog-on-eks.md`、`../product/authorization.md` 三个相对链接**，改成 `https://github.com/colvenna/colvenna-lake/blob/<SRC>/docs/...` 的绝对链接；锚点保持不变。
5. **"该套件在 CI 的 `catalog-verify` 作业中运行"** 改为"该套件在 CI 的 `verify` 作业中运行（`.github/workflows/ci.yml`）"。
6. **契约测试数**：如果 Task 1 Step 6 的实测数与 LLD 写的"106 个用例中 95 个通过、11 个跳过"不同，在原句后补一句"导入时（cove-lake@`<SRC>`）实测为 `<运行数>` 个用例中 `<通过数>` 个通过、`<跳过数>` 个跳过、0 个失败"。原有的历史数字保留。

- [ ] **Step 3: 相对链接都能解析**

```bash
cd "$WT/docs/design" && grep -oE '\]\((\.\.?/)[^)#]+' cove-catalog-lld.md | sed 's/^](//' | sort -u \
  | while read -r p; do [ -e "$p" ] && echo "ok $p" || echo "MISSING $p"; done
```

Expected: 只有 `ok ../../src`，没有 `MISSING`。

- [ ] **Step 4: 扫描敏感值**

Run: `grep -nE '\b[0-9]{12}\b|arn:aws' "$WT/docs/design/cove-catalog-lld.md"`

Expected: 没有输出。

- [ ] **Step 5: 新闻稿与设计稿改用实测数**

`docs/product/press-release.md` 的"解决方案"与"目标"表，以及 `docs/design/v1.0-design.md` 第 4 节，把"106 个用例中 95 个通过、11 个跳过、0 个失败"、"95 通过、11 跳过、0 失败"改成 Task 1 的实测数，并注明"导入时实测"。链接改指本仓库的 `docs/design/cove-catalog-lld.md#契约与版本`。

- [ ] **Step 6: README 的文档列表加一行**

在 `Press release & FAQ` 那一行之前加：`- [cove-catalog LLD](docs/design/cove-catalog-lld.md): how the server implements the Paimon REST contract, commits, auth and per-table credentials (in Chinese).`

- [ ] **Step 7: markdownlint**

Run: `cd "$WT" && npx --yes markdownlint-cli2@0.23.3 README.md "docs/**/*.md"`

Expected: `Summary: 0 issues`。

- [ ] **Step 8: 提交**

```bash
cd "$WT" && git add README.md docs && git commit -m "docs: import the cove-catalog LLD from cove-lake@$SRC

Links back to cove-lake are pinned to that commit until the docs are
made self-contained. The press release and the design quote the contract
test counts measured on import.

Refs #<N>

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

### Task 3: CI、本地全套检查与开发约定

**Files:**

- Create: `.github/workflows/ci.yml`
- Create: `.pre-commit-config.yaml`
- Create: `scripts/ci-local.sh`（可执行）
- Create: `docs/development.md`
- Modify: `README.md`（文档列表加开发约定）

**Interfaces:**

- Produces: `scripts/ci-local.sh`，不带参数，在当前 `HEAD` 上执行与 CI 相同的作业。退出码：0 表示全部 PASS，1 表示有作业失败，2 表示拒绝运行（工作区不干净或没有 Docker）。日志写在 `.ci-local/<sha>/<job>.log`。后续每个合并请求都在描述里附上它的输出。

- [ ] **Step 1: `.pre-commit-config.yaml`**

与 cove-lake 的同名文件一致：

- `pre-commit-hooks` v6.0.0，包含 `trailing-whitespace`、`end-of-file-fixer`、`mixed-line-ending --fix=lf`、`check-yaml --allow-multiple-documents`、`check-merge-conflict`、`check-added-large-files --maxkb=1024`；
- `markdownlint-cli2` v0.23.3；
- `actionlint` v1.7.12。

不需要 cove-lake 的 Helm 与视频排除项。

- [ ] **Step 2: `.github/workflows/ci.yml`**

- **触发与权限**：`pull_request` 与推送到 `main`；`permissions: contents: read`。
- **作业 `pre-commit`**：与 cove-lake `lint.yml` 的同名作业逐行一致，包括 action 的固定 SHA：checkout `3d3c42e5aac5ba805825da76410c181273ba90b1`（v7.0.1）、setup-python `5fda3b95a4ea91299a34e894583c3862153e4b97`（v7.0.0）、cache `55cc8345863c7cc4c66a329aec7e433d2d1c52a9`（v6.1.0），以及 `pre-commit==4.6.0`。
- **作业 `verify`**：
  - checkout 同上；
  - setup-java `cf277c60eb25467037889841efdb72551f06f6c3`（v4.9.1），`distribution: temurin`，`java-version: "21"`，`cache: maven`，`cache-dependency-path: pom.xml`；
  - 运行 `mvn -B clean verify`。

- [ ] **Step 3: 实现 `scripts/ci-local.sh`**

`#!/usr/bin/env bash`，`set -euo pipefail`，按以下顺序执行：

1. `git status --porcelain` 非空时打印 `working tree is not clean`，退出 2。
2. `docker info` 失败时打印 `Docker is required (Testcontainers)`，退出 2。
3. 作业 `pre-commit`：`pre-commit run --all-files --show-diff-on-failure`。
4. 作业 `verify`：`mvn -B clean verify`。只有退出码为 0 **且**日志里有一行恰好是 `[INFO] BUILD SUCCESS`，才算 PASS。
5. 作业结束后，`git status --porcelain` 必须仍为空，否则打印 `jobs left changes in the tree` 并判为失败。
6. 打印 `sha: <完整 SHA>`、`pre-commit: PASS|FAIL`、`verify: PASS|FAIL`，以及日志目录。

单个作业失败不影响其他作业继续执行。全部 PASS 时退出 0，否则退出 1。

- [ ] **Step 4: 拒绝路径**

Run: `cd "$WT" && shellcheck scripts/ci-local.sh && scripts/ci-local.sh; echo "exit=$?"`（此时脚本本身还没提交，工作区不干净）

Expected: shellcheck 没有输出；脚本打印 `working tree is not clean`，`exit=2`。完整运行放在 Step 9，在提交之后。

- [ ] **Step 5: 导入的文件过 pre-commit**

Run: `cd "$WT" && pre-commit run --all-files`

Expected: 全部 `Passed`。如果修复型 hook 改了导入的文件，把这些改动单独提交为 `style: apply pre-commit hooks`（在 Task 3 的提交之前），并在合并请求里列出被改的文件。

- [ ] **Step 6: `docs/development.md`**

英文，内容如下：

- **构建**：`mvn -B clean verify`。需要 JDK 21，`JAVA_HOME` 指向 21；需要 Docker。
- **代码格式**：改过 Java 代码后先运行 `mvn spotless:apply`。
- **CI**：GitHub Actions 能运行时以它为准；运行不了时，在合并请求描述里附上 `scripts/ci-local.sh` 在该请求确切 SHA 上的输出，发布说明里记为豁免。
- **交付流程**：issue → `.worktrees/issue-<n>-<slug>` → 合并请求 → 评审 → 按 SHA 合并 → 清理 worktree。
- **cove-lake 发布期间的同步规则**：新仓库是唯一的开发处。cove-lake 里只修阻断其发布的问题；每个修复移植到本仓库时，提交信息带 `Ported-from: cove-lake@<sha>` 行。
- **脱敏**：只写占位符。

- [ ] **Step 7: README 的文档列表加一行**

`- [Development](docs/development.md): build, CI and how changes are delivered.`

- [ ] **Step 8: lint 并提交**

```bash
cd "$WT" && pre-commit run --all-files && actionlint && git add -A && git commit -m "ci: pre-commit and Maven verify, a local runner for every CI job, development notes

Refs #<N>

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

Expected: pre-commit 全部 `Passed`，actionlint 没有输出。

- [ ] **Step 9: 在最终 SHA 上跑一遍本地全套**

Run: `cd "$WT" && scripts/ci-local.sh`

Expected: `pre-commit: PASS`、`verify: PASS`、`exit 0`。把完整输出贴进合并请求的描述，然后按 `/impl-and-mr` 评审、合并，并清理 worktree。
