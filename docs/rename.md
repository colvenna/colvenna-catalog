# 改名记录：Cove Catalog → Colvenna Catalog

本文是 #31 的改名映射与核对。产品名与副标题沿用系列索引的 **Colvenna Catalog**；服务端从 Colvenna Lake 的 `colvenna-catalog/` 移植（`Ported-from: colvenna-lake@33eb4b54c23aefb17e6af3577cc1adeaf8042b24`），所以 HTTP 契约、配置名、指标名与数据库迁移链（`V1`、`V2`）都与 Colvenna Lake 现在的完全一致：Colvenna Lake 之后改用本仓库的发布物（#23），它的客户端不需要改。

## 映射

| 类别 | 旧 | 新 | 说明 |
| --- | --- | --- | --- |
| 产品、文档 | Cove Catalog；Cove Lake | Colvenna Catalog；Colvenna Lake | README、LLD（`docs/design/colvenna-catalog-lld.md`）、设计、新闻稿、调研 |
| 控制台 | — | — | 不适用：本产品没有控制台，管理面是 REST API（设计第 2 节） |
| 包、模块、类 | `com.covelake.catalog`；groupId `com.covelake`；`CoveCatalog*` | `com.colvenna.catalog`；groupId `com.colvenna.catalog`；`ColvennaCatalog*` | Apache Paimon 的上游包名（`org.apache.paimon.*`）不变 |
| CLI | — | — | 不适用：1.0 不做命令行工具（设计第 2 节，非目标） |
| 环境变量与配置 | `COVE_*` | `COLVENNA_*` | 服务端设置、`deploy/config/example.env`、安装脚本与测试 |
| HTTP 路径与 token | `/cove/admin/v1`；`/v1/cove`；token 前缀 `cove_` | `/colvenna/admin/v1`；`/v1/colvenna`；`colvenna_` | 与 Colvenna Lake 的客户端一致 |
| OpenAPI | — | `docs/api/admin-v1.yaml`：`/colvenna/admin/v1`、`colvenna_` token、`x-colvenna-action` | 管理 API 的 OpenAPI 描述（#15）写成时就用新名字，没有旧名的版本；Paimon REST 的 OpenAPI 是上游的，不改 |
| schema 与系统列 | 版本表 `cove_schema_version` | `colvenna_schema_version` | 迁移文件 `V1`、`V2` 不改名、不合并；表里没有带旧名的系统列 |
| 元数据、SQL、策略 | 默认 warehouse `cove`；元数据库主账号 `cove_admin`；数据库与它的账号 `cove_catalog` | `colvenna`；`colvenna_admin`；`colvenna_catalog` | IAM 策略与会话名的前缀见下行 |
| 指标、日志、标签 | `cove_catalog_*` 指标；日志名 `cove.access`；STS 会话名前缀 `cove-`；标签 `Project=cove-catalog` | `colvenna_catalog_*`；`colvenna.access`；`colvenna-`（超长时截短并加哈希，不超过 64 个字符）；`Project=colvenna-catalog` | 指标名与 Colvenna Lake 的看板、告警一致 |
| 文件与目录 | `deploy/helm/cove-catalog/`；`docs/design/cove-catalog-lld.md` | `deploy/helm/colvenna-catalog/`；`docs/design/colvenna-catalog-lld.md` | 已执行的计划 `docs/superpowers/plans/` 保持原样（见“例外”） |
| 镜像、chart、IaC、资源前缀 | 镜像与 chart `cove-catalog`；栈 `CoveCatalog*`、`CoveCatalogTest*`；资源前缀 `cove-catalog-*`；CDK 包 `@cove-catalog/aws` | `colvenna-catalog`；`ColvennaCatalog*`、`ColvennaCatalogTest*`；`colvenna-catalog-*`；`@colvenna-catalog/aws` | 发布仓库栈 `ColvennaCatalogRelease` |
| 安装、卸载、备份恢复 | `install.sh`、`uninstall.sh` 与 `deploy/lib/` 里的旧名 | 新名 | 本仓库还没有备份恢复脚本（用户手册 #14 写元数据库的快照恢复），写时用新名字 |
| 测试、fixtures、示例 | 契约测试 `CoveCatalogContractTest` 等 | `ColvennaCatalogContractTest` 等 | 测试数不变：209 个，契约测试 107 个运行、11 个跳过 |
| workflow 与发布脚本 | `.github/workflows/ci.yml`、`scripts/ci-local.sh` 里的旧名 | 新名 | 发布脚本还没有（#9），写时用新名字 |
| 图、SVG、HTML、PNG、视频 | 标志 `assets/brand/` 的字标 Cove Catalog | 系列主标识加 Colvenna Catalog 字标，由 `assets/brand/generate.py` 生成（移植自 Colvenna Lake 的生成器） | PNG 逐个查看过；没有 HTML 图与视频 |
| 仓库元数据 | GitHub `amliuyong/cove-catalog` | `amliuyong/colvenna-catalog` | 待办：合并之后改名（见“未完成”） |
| 当前任务标题 | issue、里程碑标题里的 Cove Catalog | Colvenna Catalog | 待办：随仓库改名一并改 |

## 例外

旧名只允许出现在 [`scripts/rename/exceptions.tsv`](../scripts/rename/exceptions.tsv) 列出的位置，每条写明文件、允许的那一行文字与理由：

- 命名沿革：README、品牌说明与设计里各一句；
- 导入来源：LLD 与设计里指向 `cove-lake@6ded0b7b` 的几行（导入提交 `e37870b` 就是这样写的）；
- 改名之前执行过的四份计划（`docs/superpowers/plans/2026-10-24-*.md`）：记录当时的名字，不回改。只有维护者主机上的克隆路径换成了占位符 `<本仓库的克隆>` 与 `<Colvenna Lake 的克隆>`，当时开 issue 的仓库写作“本项目早期的私有仓库（当时名为 `cove-catalog`）”（#99）；
  - 其中导入计划（`2026-10-24-import-from-cove-lake.md`，Task 2 第 2 步第 4 项）有一个指向 Colvenna Lake 仓库的 GitHub URL 模板：它带占位符 `<SRC>`，不是可用的链接。它原先按 Colvenna Lake 当时的仓库名书写（#50），#99 把它改成 Colvenna Lake 迁到 colvenna 组织之后的地址；按它改出的链接已在 #17 从 LLD 中去掉。本文不重复这个模板：本仓库的文件不点名系列里的其他仓库；
- 检查自己的三个文件。

`scripts/rename/old_name.py` 在 pre-commit 与 CI 里检查整棵树：旧名作为独立词、词首、驼峰里以大写开头的一段，以及拼出旧名的十六进制字面量，都算命中；`cover`、`coverage`、`recover`、`discover` 等普通词不算。它不读二进制文件的内容，所以 PNG 由人查看。

## 已有环境与升级

改名之前没有保留的安装：两个 Region 里都没有 `CoveCatalog*` 栈，也没有 `cove-catalog` 的 ECR 仓库（2026-10-28 核对）。1.0 之前不保证升级兼容（设计第 3 节），所以不做旧名到新名的迁移：旧名字的数据库会在 `V1` 失败，旧 token 会被拒绝。

## 未完成

- **GitHub 仓库改名**：合并之后执行，之后 `amliuyong/colvenna-catalog` 的链接才有效，不靠重定向。
- **发布制品的扫描**：第一个发布候选（#9、#21）的镜像按 digest 解包扫描，结果写进它的发布检查。
- **从干净环境用新名字安装与验收**：在 #9 的试验候选与 #21 的 UAT 上完成。
