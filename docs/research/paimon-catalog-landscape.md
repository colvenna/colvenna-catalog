---
title: "Paimon 生态的 catalog 竞品调研：在位者、开源空缺、统一 Iceberg 与部署成本"
date: 2026-10-23
status: draft
sources:
  - https://github.com/apache/paimon （release-2.0.0 = 604e6d5e131c74a8d127333a2a3ad6d0319732bf；release-2.1.0 = 3a5ffe7870697e2c2605e63413fb5ac5800aae7e；master = 0c24e922d73decaf2131b7246c51dada19e1250e）
  - https://api.github.com/repos/apache/paimon/releases ; https://cwiki.apache.org/confluence/spaces/PAIMON/pages/327979149/PIP-28+Introduce+REST+Catalog ; https://github.com/apache/paimon/issues/5966
  - https://github.com/apache/paimon-rust （v0.4.0：crates/paimon-rest-server）
  - https://github.com/apache/polaris （apache-polaris-1.8.0；PR #3723、#3820）；https://incubator.apache.org/projects/polaris.html
  - https://github.com/apache/gravitino （v1.3.1；issue #7663、#7853；PR #9751）
  - https://github.com/unitycatalog/unitycatalog （v0.7.0）；https://lfaidata.foundation/projects/unity-catalog/
  - https://github.com/lakekeeper/lakekeeper （v0.14.1；issue #1244）
  - https://github.com/projectnessie/nessie （nessie-0.109.0）
  - https://github.com/apache/amoro （v0.8.1-incubating、v0.9.0-rc13）
  - https://github.com/apache/hive （rel/release-4.2.1）
  - https://github.com/apache/incubator-xtable （0.4.0-incubating）
  - https://github.com/nryanov/kasanari （v0.3.0）；https://github.com/melin/lakehouse-rest-catalog （默认分支，2026-10-23）
  - https://github.com/StarRocks/starrocks （4.1.6、main）；https://github.com/apache/doris （4.1.4、4.1.5-release、master；PR #69343）；https://github.com/apache/paimon-trino （main）；https://github.com/trinodb/trino （PR #30913）
  - https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-7140-release.html
  - https://www.alibabacloud.com/help/en/dlf/dlf-2-0/product-overview/billing ；…/compute-resource-fees ；…/user-guide/access-dlf-paimon-tables-via-java-api ；…/user-guide/access-iceberg-tables-using-pyiceberg
  - https://aws.amazon.com/glue/pricing/ ；https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-east-1/index.json ；https://aws.amazon.com/s3/pricing/
  - https://docs.snowflake.com/user-guide/opencatalog/overview ；https://www.snowflake.com/legal-files/CreditConsumptionTable.pdf
  - https://www.databricks.com/product/unity-catalog ；https://cloud.google.com/products/lakehouse/pricing ；https://docs.cloud.google.com/bigquery/docs/about-blms ；https://developers.cloudflare.com/basin-catalog/platform/pricing/
  - https://docs.aws.amazon.com/lambda/latest/dg/ （lambda-runtimes、snapstart、gettingstarted-limits、configuration-vpc、web-functions）；https://aws.amazon.com/lambda/pricing/
  - https://docs.aws.amazon.com/apigateway/latest/developerguide/ （http-api-vs-rest、http-api-quotas、api-gateway-execution-service-limits-table）；https://aws.amazon.com/api-gateway/pricing/
  - https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html ；https://aws.amazon.com/fargate/pricing/ ；https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html ；https://aws.amazon.com/apprunner/pricing/
  - https://docs.aws.amazon.com/eks/latest/userguide/horizontal-pod-autoscaler.html ；https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/ ；https://keda.sh/docs/2.21/concepts/scaling-deployments/ ；https://github.com/kedacore/http-add-on
  - https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_terms-and-concepts.html ；https://aws.amazon.com/privatelink/pricing/
  - https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html ；https://docs.aws.amazon.com/aurora-dsql/latest/userguide/
  - colvenna-lake 仓库（main，2026-10-23）：docs/design/colvenna-catalog-lld.md、docs/manual/cost.md、docs/research/iceberg-open-layer.md、deploy/helm/colvenna-catalog/、colvenna-catalog/
---

# Paimon 生态的 catalog 竞品调研：在位者、开源空缺、统一 Iceberg 与部署成本

> 本文写于 colvenna-catalog 拆出 Colvenna Lake 之前，文中 `colvenna-catalog/`、`deploy/`、`docs/` 开头的路径都指 Colvenna Lake 仓库当时的 main（2026-10-23），按它改名后的名字写。指向 colvenna-catalog LLD 的链接现在指本仓库的 [LLD](../design/colvenna-catalog-lld.md)：它随服务端导入，文中引用它的请求体上限与固定的 Paimon 版本没有变（第 2.3 节指出的一处过时说法，LLD 已改正）。
>
> 说明：取数日期一律为 **2026-10-23**（star 数、最新发布、定价页都按这一天）。Paimon 的结论以发布标签的源码为准，下文 `P2.0/`、`P2.1/`、`PM/` 分别指 `https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/`（release-2.0.0）、`…/blob/3a5ffe7870697e2c2605e63413fb5ac5800aae7e/`（release-2.1.0）与 `…/blob/0c24e922d73decaf2131b7246c51dada19e1250e/`（master，2026-10-23）。AWS 价格取 `us-east-1`。本文**没有部署、运行或调用**任何组件或云资源；"推断："开头的是由一手材料推出的判断，"未核实"是没找到一手来源的结论。按需求方 2026-10-23 的澄清，**元数据库（RDS/Aurora PostgreSQL）由用户自带、不计入费用**，"空闲不花钱"只指计算资源；元数据库的无服务器选项只作简短备注（第 7.4 节）。

## 结论

1. **Paimon 生态的 REST 服务端几乎是空白，在位者只有阿里云 DLF。** Paimon 项目只发布客户端与 OpenAPI 契约，服务端只有测试 mock（`RESTCatalogServer` 注释 "Mock REST server for testing"）；PIP-28 原文 "Developers will need to create their own or opt for a hosted service"；官方文档唯一点名的服务端是 DLF（闭源、只在阿里云）。要求官方服务端的 apache/paimon#5966 开了 15 个月，0 条评论（第 2.5、2.6 节）。
2. **开源侧没有成熟的 Paimon REST 服务端。** Gravitino 的 #7663 仍开着，PMC 以"规范是否成熟、需求是否足够、维护成本"为由要求先讨论，Epic #7853 关闭时没有代码。能找到的实现只有三个：Kasanari（Apache-2.0，0 star，v0.3.0，跟 Paimon 1.4.2 契约，不下发表凭证）、melin/lakehouse-rest-catalog（2026-10-09 才建，无 LICENSE、无发布、认证默认关）、paimon-rust 自带的测试服务端（不发布）。Polaris、Lakekeeper、Unity Catalog OSS、Nessie、Amoro、HMS 都不说 Paimon REST 协议（第 3、4 节）。
3. **AWS 上没有任何托管的 Paimon catalog。** Paimon 没有 Glue catalog 类型（Glue 只出现在 Iceberg 兼容发布这条路上），S3 Tables 只存 Iceberg，EMR 7.14.0 与 Athena 的官方文档都没有 Paimon（第 2.2、5、6 节）。推断：在 AWS 上想用 Paimon REST，目前只能自建，这是 colvenna-catalog 最实在的空位。
4. **能直连 Paimon 2.0 REST 契约的已发布引擎只有 Paimon 自己的 Flink 与 Spark 连接器。** Doris 4.1.5 文档化支持 REST 并实现了服务端下发凭证，但捆绑 Paimon 1.4.2；StarRocks 4.1.6 捆绑 1.3.1，文档只写 `filesystem`/`hive`，REST 只能靠参数透传，且读路径不使用下发凭证；Trino 无正式发布。两家的 master/main 已升 2.x，但都未发布（第 5 节）。
5. **"统一 Paimon 与 Iceberg"有人在做，但都没做到"一个服务端同时说两种原生协议并下发凭证"的成熟版本。** DLF 同时提供 Paimon REST 与 Iceberg REST（后者要用它改过的 `pyiceberg-dlf`）；Polaris 的路线是 Iceberg 原生 + generic table 登记 Paimon（Spark 插件把 Paimon 表放在独立 warehouse，Polaris 只登记，2026-10-20 合进 main 未发布，generic table 不做提交协调、不下发凭证）；Gravitino 是统一元数据 API + Iceberg REST server + Paimon 连接器（客户端）；Kasanari 与 melin 在一个服务里同时暴露 Paimon 与 Iceberg REST，但都处于个人项目早期（第 4 节）。
6. **Paimon 自己的两条"兼容 Iceberg"路径都不是让 Paimon catalog 管 Iceberg 表。** REST 契约里的 `iceberg-table` 类型只登记元数据，`IcebergTable` 注释写明 "paimon does not support read and write operation on this table yet"；`metadata.iceberg.storage` 是把 Paimon 表发布成 Iceberg 元数据给 Iceberg 读端，colvenna-lake 已经用它加 Polaris EXTERNAL catalog（第 2.7 节）。XTable 0.4.0 只把 Paimon 当 source。
7. **差异化逐项看：** "Apache 2.0" 对开源竞品**不是差异**（全部是 Apache-2.0，Lakekeeper 另要 CLA、UC 要 DCO），只对 DLF（闭源）和 melin（无许可证）成立。"EKS 部署"**不是差异**：Polaris、Gravitino、Nessie、Lakekeeper 都有官方镜像与 Helm chart，差别在于它们都不提供 Paimon REST。"更便宜"**对 catalog 计费本身不成立**：按统一假设（1 万张表、每月 1000 万次请求），托管 catalog 每月 $0–$81（Glue 约 $9，DLF、Databricks UC、Snowflake Open Catalog 的 catalog 部分为 $0），而 colvenna-catalog 的计算层常驻就是几十美元；成立的是"AWS 上没有可比的托管 Paimon catalog"。"无服务器"是**真差异**：所有开源竞品都没有文档化的无服务器部署，但 colvenna-catalog 走 Lambda 有硬约束（第 8 条）（第 1、6、7 节）。
8. **colvenna-catalog 走 Lambda + 带鉴权 API Gateway 的硬约束**（Lambda Function URL 一律不用）：同步载荷 6 MB、API Gateway 10 MB，而服务端请求体上限 64 MiB；HTTP API 集成超时 30 s 不可调，REST API 默认 29 s，与主池 `lock_timeout` 30 s、S3 调用超时 30 s 相当或更短；**更关键的是凭证下发**：colvenna-catalog 现在用 Pod 服务账号 token 调 `AssumeRoleWithWebIdentity`，IAM 规定 role chaining 的会话最长 1 小时，而 `COLVENNA_DATA_TOKEN_DURATION` 只允许 `PT1H30M`–`PT12H`；推断：Lambda 与 Fargate 上用执行角色或任务角色再 AssumeRole 属于 role chaining，现有签发方式不能原样搬过去。SnapStart 支持 Java 21，也支持容器镜像，对 Java 不另收费，但恢复后要重建数据库连接（第 7 节）。
9. **计算层"缩到 0"的现状：** Lambda + API Gateway 空闲时不收费（VPC 内访问 STS 需 NAT 或接口端点，接口端点每 AZ 每小时 $0.01）。ECS Fargate 服务可把最小任务数设 0，但没有请求缓冲，ALB 在没有目标时回 503，ALB 本身照常计费。App Runner 已不对新客户开放。EKS 上 HPA 最少 1 个副本；HPA 缩到 0 在 Kubernetes 1.37 才是 beta 默认开，EKS 当前最高 1.36（alpha、默认关）。KEDA 能管 0↔1，它的 HTTP add-on 仍是 beta；而且 Pod 缩到 0 后节点和控制面照常计费。colvenna-catalog 的 chart 现在固定 2 副本、PDB `minAvailable: 1`，没有 HPA（第 7.2 节）。

## 1. 对比表

### 1.1 许可证与治理

| 项目 | 许可证（LICENSE 文件） | 治理 | 最新发布（日期） | 语言 / 运行时 | 活跃度（2026-10-23） |
| --- | --- | --- | --- | --- | --- |
| colvenna-catalog | Apache-2.0（仓库 `LICENSE`） | 本项目 | 未独立发布，镜像随 Colvenna Lake 的 `v1.0.0-rc.1` 发布（Colvenna Lake 的成本估算 `docs/manual/cost.md` 第 5.2 节） | Java 21，JDK `com.sun.net.httpserver` | — |
| Apache Paimon（客户端与契约） | Apache-2.0 | ASF 顶级项目 | 2.1.0（2026-10-12）；2.0.0（2026-08-07） | Java | 3,428 star、1,422 fork |
| 阿里云 DLF | 商业闭源 | 阿里云 | 托管服务 | — | — |
| Apache Polaris | Apache-2.0，附第三方声明 | ASF 顶级项目，2026-02-15 毕业 | 1.8.0（2026-09-28）；1.7.0（08-02）；1.6.0（07-09） | Java 21 + Quarkus 3.39.3 | 2,096 star、554 fork |
| Apache Gravitino | Apache-2.0，附第三方声明 | ASF 顶级项目，2025-05 毕业 | v1.3.1（2026-09-30）；v1.3.0（06-29）；v1.2.1（05-12） | Java 17，Jetty + Jersey | 3,315 star、967 fork |
| Unity Catalog OSS | Apache-2.0；贡献需 DCO | LF AI & Data，sandbox 阶段 | v0.7.0（2026-10-13）；v0.6.0（08-20）；v0.5.1（07-18） | Java 17，Armeria + Hibernate | 3,557 star、690 fork |
| Lakekeeper | Apache-2.0（版权 Vakamo Inc.）；贡献需 CLA | 公司主导，无基金会 | v0.14.1（2026-10-16）；v0.14.0（10-07） | Rust（axum、sqlx） | 1,485 star、168 fork |
| Project Nessie | Apache-2.0 | 无基金会，NOTICE 版权 Dremio | 0.109.0（2026-10-08） | Java 21 + Quarkus | 1,523 star；2026-07-25 起 404 个提交，其中 361 个来自 renovate 机器人 |
| Apache Amoro | Apache-2.0 | ASF 孵化中（2024-03-11 起） | 0.8.1-incubating（2025-09-11）；0.9.0 RC13 于 2026-10-14 开始投票 | Java | 1,196 star、398 fork |
| Apache Hive Metastore | Apache-2.0 | ASF 顶级项目 | 4.2.1（2026-08-24） | Java 21 | 6,033 star（apache/hive） |
| Apache XTable | Apache-2.0 | ASF 孵化中 | 0.4.0-incubating（2026-08-24） | Java | 1,309 star |
| Kasanari | Apache-2.0 | 个人（2 个贡献者） | v0.3.0（2026-09-01） | Java + Quarkus | 0 star |
| melin/lakehouse-rest-catalog | **无 LICENSE 文件** | 个人（1 个作者，91 个提交） | 无发布，2026-10-09 建库 | Java + Spring Boot + JPA | 3 star |

来源：各仓库 `gh api repos/<owner>/<repo>` 与 releases 接口（2026-10-23）；Polaris 毕业日期见 <https://incubator.apache.org/projects/polaris.html> ；Gravitino 见 <https://incubator.apache.org/projects/> ；UC 见 <https://lfaidata.foundation/projects/unity-catalog/> ；Nessie NOTICE 见 <https://github.com/projectnessie/nessie/blob/nessie-0.109.0/NOTICE> ；Lakekeeper 见 <https://github.com/lakekeeper/lakekeeper/blob/v0.14.1/LICENSE> ；Amoro 0.9.0 投票见 <https://lists.apache.org/thread/0j80o73649qwg1p590h58tyykxzk8no9> ；melin 的 `license` 接口返回 404。

### 1.2 Paimon 与 Iceberg 支持层次

| 项目 | Paimon | Iceberg | 元数据持久化 | 认证 / 授权 | 凭证下发 |
| --- | --- | --- | --- | --- | --- |
| colvenna-catalog | **原生 Paimon REST**（2.0.0 契约，上游 `RESTCatalogTest`/`CatalogTestBase` 106 个用例 95 通过、11 跳过） | 无；开放层经 Polaris EXTERNAL catalog 发布 | PostgreSQL | 服务端签发不透明 bearer（存哈希、可过期撤销）；CATALOG/DATABASE/TABLE 等范围的 READ/WRITE/MANAGE，ALLOW/DENY，列级读 | AWS STS 按表会话策略；提交只支持 S3 兼容存储与本地文件 |
| 阿里云 DLF | **原生 Paimon REST**（Paimon 文档唯一点名） | Iceberg REST，SigV4 签名名 `DlfNext`，客户端要装 `pyiceberg-dlf` | 托管 | `token.provider=dlf`（AK/SK、STS、ECS 角色） | Doris 文档称 DLF REST 用 vended credential 访问 OSS |
| Polaris 1.8.0 | **登记级**：Generic Table API（beta，`format` 任意字符串）；Paimon 只经 Polaris 自己的 Spark 插件，见 4.1 | 完整 Iceberg REST：视图、多表提交、通知、联邦；无 `/plan`，无 remote signing | PostgreSQL（就绪）、MongoDB（实验）、内存（测试） | 内置 OAuth2 token broker / 外部 OIDC / mixed；RBAC，可接 OPA、Ranger | S3 STS、ADLS SAS、GCS；**generic table 不下发** |
| Gravitino v1.3.1 | **连接器级**：`lakehouse-paimon` 代理 filesystem/jdbc/hive/rest 后端（Paimon 1.2.0），rest 后端是 Paimon REST **客户端**；无 Paimon REST 服务 | Iceberg REST server（Iceberg 1.11 REST API，有 `/plan`；文档写多表事务未实现），只管 Iceberg 表 | H2（仅开发）、MySQL、PostgreSQL | simple/Basic/Kerberos/OAuth（JWT、JWKS、OIDC）；RBAC + DENY，可下推 Ranger | S3 STS/IRSA、GCS、ADLS、OSS；无 remote signing |
| Unity Catalog OSS v0.7.0 | 无（`DataSourceFormat` 无 Paimon） | 部分 Iceberg REST（无视图、事务、register、`/plan`）+ UniForm | H2 默认，MySQL/PostgreSQL | 外部 OIDC 换 UC token；权限默认关 | S3 STS、ADLS、GCS |
| Lakekeeper v0.14.1 | 无；generic table 只是不透明 `format`，不做提交协调 | Iceberg REST：多表提交、视图、S3 remote signing | PostgreSQL ≥15 | 外部 OIDC / K8s SA，自己不签 API key；OpenFGA | S3 STS + remote signing、R2、OSS、ADLS、GCS |
| Nessie 0.109.0 | 无 | Iceberg REST（文档自称 "experimental"）：视图、原子多表提交、git 式分支 | RocksDB、BigTable、MongoDB、PostgreSQL 等 | 校验 OIDC bearer（默认关），CEL 授权规则 | S3 STS + remote signing、GCS（实验）、ADLS |
| Amoro 0.8.1 | **连接器级、只展示**：filesystem/hive 类型的 Paimon catalog，不做自优化 | 自优化（压缩、过期）；有未文档化的 Iceberg REST 端点，**在登录白名单内、免认证** | derby/MySQL/PostgreSQL | dashboard token/basic | 无 |
| HMS 4.2.1 | 客户端侧 `metastore=hive` | Iceberg REST（4.1.0 起；多表提交非原子） | 关系库（derby/MySQL/PostgreSQL/Oracle/MSSQL） | Thrift：NOSASL/Kerberos/LDAP/JWT；REST：none/simple/jwt/oauth2 | 无 |
| XTable 0.4.0 | 只作 source | source 与 target（作客户端） | 无状态 | 无 | 无 |
| Kasanari v0.3.0 | Paimon REST（跟 1.4.2 OpenAPI，前缀 `/paimon/v1/{catalogId}`） | Iceberg REST（Iceberg 1.11.0）；另有 Lance | PostgreSQL / YugabyteDB | none、LDAP、OIDC；Casbin RBAC | **Paimon 侧无**：`getTableToken` 抛 `UnsupportedOperationException` |
| melin/lakehouse-rest-catalog | Paimon REST + Management API（依赖 Paimon 2.0.0） | Iceberg REST（README 自述 35 个端点实现 30 个） | MySQL 8.0（H2、PostgreSQL profile） | bearer + RBAC，**默认关闭** | 下发配置的静态密钥（未核实，见第 3 节） |

来源见第 3、4 节各项目条目；colvenna-catalog 见 [colvenna-catalog LLD](../design/colvenna-catalog-lld.md)。

### 1.3 部署形态

| 项目 | 官方镜像 | Helm chart | Operator | 文档化的无服务器部署 |
| --- | --- | --- | --- | --- |
| colvenna-catalog | 自建镜像（`eclipse-temurin:21-jre` 基础，`colvenna-catalog/Dockerfile`） | 仓库内 `deploy/helm/colvenna-catalog`：固定 `replicas: 2`、PDB `minAvailable: 1`、无 HPA | 无 | 无 |
| Polaris | `apache/polaris:1.8.0` | <https://downloads.apache.org/polaris/helm-chart> （含 1.8.0） | 未找到 | 未找到（云部署文档是 VM + 托管数据库 + Docker） |
| Gravitino | `apache/gravitino`、`apache/gravitino-iceberg-rest`、`apache/gravitino-lance-rest` | Docker Hub 上的 OCI chart | 未找到 | 未找到 |
| Unity Catalog OSS | `unitycatalog/unitycatalog:v0.7.0` | 只在仓库 `helm/`（0.0.1-pre.1），没找到发布渠道 | 未找到 | 未找到 |
| Lakekeeper | `quay.io/lakekeeper/catalog:v0.14.1` | <https://lakekeeper.github.io/lakekeeper-charts/> （最新 0.12.0，对应 0.13.3，落后） | lakekeeper-operator，README 标 "in development"，未发布 | 未找到 |
| Nessie | `ghcr.io/projectnessie/nessie`、quay.io | <https://charts.projectnessie.org/> | 未找到 | 认证文档残留 "When a Nessie server runs as an AWS Lambda"，0.109.0 源码无 Lambda 模块（未核实） |
| Amoro | `apache/amoro` | 仓库内 `charts/amoro`，未找到托管 chart 仓库 | 未找到 | 未找到 |
| HMS | `apache/hive:standalone-metastore-4.2.1`（镜像配置把 REST 认证设为 `none`） | 未找到 | 未找到 | 未找到 |
| Kasanari | `nryanov/kasanari`（Docker Hub） | 未找到 | 未找到 | 未找到 |
| DLF、Glue、S3 Tables、Snowflake Open Catalog、Databricks UC、Google Lakehouse runtime catalog | 托管 | — | — | 对用户而言即托管服务 |

来源：Polaris <https://github.com/apache/polaris/tree/apache-polaris-1.8.0> ；Gravitino <https://github.com/apache/gravitino/blob/v1.3.1/docs/docker-image-details.md> ；UC <https://github.com/unitycatalog/unitycatalog/tree/v0.7.0/helm> ；Lakekeeper <https://github.com/lakekeeper/lakekeeper/blob/v0.14.1/README.md> ；Nessie <https://projectnessie.org/nessie-latest/authentication/> ；Amoro <https://amoro.apache.org/docs/latest/deployment-on-kubernetes/> ；HMS <https://github.com/apache/hive/blob/rel/release-4.2.1/standalone-metastore/packaging/src/docker/conf/metastore-site.xml> ；Kasanari <https://github.com/nryanov/kasanari/blob/v0.3.0/README.md> ；colvenna-catalog `deploy/helm/colvenna-catalog/values.yaml`、`templates/pdb.yaml`。"未找到"指官方仓库与文档里没有，搜索方法见第 8 节。

### 1.4 托管产品收费（统一假设下的月费估算）

假设：1 万张表，每张表算 1 个 catalog 对象，没有分区；每月 1000 万次 catalog API 请求，其中 90% 为读、10% 为写或列举；只算 catalog 本身；表数据另列 1 TB。访问日期 2026-10-23，AWS 为 `us-east-1`。**全部是估算**，推导见第 6 节。

| 产品 | 计费维度 | catalog 月费（估算） | 备注 |
| --- | --- | --- | --- |
| AWS Glue Data Catalog | 对象数（前 100 万免费，之后每 10 万个 $1）、请求数（前 100 万免费，之后每 100 万次 $1） | **约 $9.00** | Paimon 不能把 Glue 当 metastore |
| Amazon S3 Tables | 存储、请求、按对象的监控费、压缩 | **$13.60**（20 万个对象）至 **$258.60**（1000 万个对象） | 只存 Iceberg；1 TB 存储另计 $27.14 |
| 阿里云 DLF 2.0 | 按 catalog 计：湖表存储 + 计算 CU；元数据 API 不在计费项里 | **$0**（catalog 部分） | 维护任务按 CU 另计，弗吉尼亚 $0.07/CU-小时 |
| Snowflake Open Catalog（托管 Polaris） | REST API 请求，每 100 万次 0.5 credit | **当前 $0**；开始收费后 Standard 约 $10 | **新客户已不能开通** |
| Databricks Unity Catalog | 不单独收费，含在 Premium/Enterprise | **$0**（增量） | 平台费另计，无法估 |
| Google Lakehouse runtime catalog（原 BigLake metastore） | Class A 每 100 万次 $6（前 5,001 次免费）、Class B 每 100 万次 $0.90（前 50,001 次免费） | **约 $14.02** | 区域默认 Iowa，页面注明价格随区域变 |
| Cloudflare Basin Catalog（原 R2 Data Catalog 页面路径） | catalog 操作（前 100 万免费，之后每 100 万次 $9） | **约 $81.00** | 快照过期免费 |
| colvenna-catalog（参照，见第 7 节） | 计算层（数据库由用户自带） | EKS 上按 CPU requests 折算约 $30；Fargate 两个任务约 $29–36 加 ALB $16；Lambda + HTTP API 约 $38（按量假设） | 推算 |

## 2. Paimon 用户现有的 catalog 选项

### 2.1 版本

- **最新正式版 2.1.0**：GitHub Release 2026-10-12；annotated tag 指向提交 `3a5ffe78`；Maven Central `latest` 为 2.1.0；2.1.1 自 2026-10-19 在 dev 列表讨论，未发布；master 为 `2.2-SNAPSHOT` [来源: https://github.com/apache/paimon/releases/tag/release-2.1.0 ; https://repo1.maven.org/maven2/org/apache/paimon/paimon-core/maven-metadata.xml ; https://lists.apache.org/thread/xzrnjdjpbsgj39tb4d9mz584pgdtfbxb]。
- **2.0.0**：GitHub Release 2026-08-07，tag 指向 `604e6d5e`，colvenna-catalog 固定依赖这一版 [来源: https://github.com/apache/paimon/releases/tag/release-2.0.0 ; [colvenna-catalog LLD](../design/colvenna-catalog-lld.md)]。

### 2.2 metastore 类型

- 2.0.0 文档原文 "Paimon catalogs currently support four types of metastores"：`filesystem`（默认）、`hive`、`jdbc`、`rest`；2.1.0 改成表格，仍是这四种 [来源: `P2.0/docs/docs/concepts/catalog.md` 第 33–38 行 ; https://paimon.apache.org/docs/2.1/concepts/catalog/]。
- 源码中的 `CatalogFactory`：`filesystem`、`jdbc`、`rest` 在 paimon-core，`hive` 在 paimon-hive-catalog；三个 ref 一致 [来源: `P2.0/paimon-core/src/main/java/org/apache/paimon/rest/RESTCatalogFactory.java` ; `P2.0/paimon-core/src/main/java/org/apache/paimon/jdbc/JdbcCatalogFactory.java` ; `P2.0/paimon-hive/paimon-hive-catalog/src/main/java/org/apache/paimon/hive/HiveCatalogFactory.java`]。
- **没有 Glue catalog 类型。** Glue 只出现在 Iceberg 兼容发布里：`metadata.iceberg.hive-client-class=com.amazonaws.glue.catalog.metastore.AWSCatalogMetastoreClient`，2.1.0 另有 `metadata.iceberg.glue.skip-archive` [来源: `P2.0/docs/docs/iceberg/hive-catalog.md` ; `P2.1/paimon-core/src/main/java/org/apache/paimon/iceberg/IcebergOptions.java`]。
- **DLF 不是独立的 metastore 类型**，而是 `rest` 加 `token.provider=dlf`。Doris 的 `paimon.catalog.type=dlf` 是 Doris 自己的类型 [来源: `P2.0/docs/docs/ecosystem/doris.md`]。

### 2.3 REST 契约与演进

- `docs/static/rest-catalog-open-api.yaml` 在 2.0.0、2.1.0、master 上都是 `openapi: 3.1.1`、`info.version: "1.0"`，路径前缀 `/v1/` [来源: `P2.0/docs/static/rest-catalog-open-api.yaml` 第 19–26 行 ; 同文件 `P2.1/`、`PM/`]。
- **首个可用版本是 1.1.0**：1.0.0 已注册 `rest` factory，但 `getTable`、`createTable` 等直接抛 `UnsupportedOperationException`；1.1 发布说明原文 "REST Catalog is ready in this release" [来源: https://github.com/apache/paimon/blob/release-1.0.0/paimon-core/src/main/java/org/apache/paimon/rest/RESTCatalog.java ; https://github.com/apache/paimon-website/blob/master/community/docs/releases/release-1.1.md]。
- **路径只增不减，直到 master 删了一条**（按 yaml 解析）：

| 区间 | 新增 | 删除 |
| --- | --- | --- |
| 2.0.0（40 条）→ 2.1.0（42 条） | `.../semantic-views`、`.../semantic-views/{semanticView}` | 无 |
| 2.1.0 → master（46 条） | `.../tables/{table}/commit-lock`、`.../commit-lock/renew`、`.../tables/{table}/replace`、`.../tables/{table}/schemas`、`.../schemas/{version}` | `.../branches/{branch}/rename` |

  删除分支改名对应 dev 讨论 "[DISCUSS] Drop renameBranch from REST Catalog due to non-atomic backend rename"（2026-05-29）[来源: https://lists.apache.org/thread/pbybqpd7jzcvy5pxo0cd8rmffb8cxpcb]。推断：`info.version` 一直是 "1.0"，契约的增删只能按 Paimon 发布版对照，不能按版本号判断。colvenna-catalog 对 `branches/{branch}/rename` 已经回 501（[LLD](../design/colvenna-catalog-lld.md)"端点范围"）。

- **Management API 已在 2.1.0 发布**，title "Paimon REST Management API"、version "1.0"，文档原文 "experimental … may evolve incompatibly"，覆盖 labels、permissions、policies（行过滤、列脱敏）；master 又改了 labels 的路径 [来源: `P2.1/docs/static/rest-management-open-api.yaml` 第 17–20 行 ; `P2.1/docs/docs/concepts/rest/management-api.md` ; https://paimon.apache.org/docs/2.1/concepts/rest/management-api/]。colvenna-catalog LLD 当时写的"只在 master 上出现"已过时（LLD 已据此改正）。

### 2.4 客户端的 token provider

- Java 客户端只有 **`bear`** 与 **`dlf`**，2.0.0、2.1.0、master 一致 [来源: `P2.0/paimon-api/src/main/java/org/apache/paimon/rest/auth/AuthProviderEnum.java` 第 23–24 行]；PyPaimon 同样只认这两种 [来源: `P2.1/paimon-python/pypaimon/api/auth/factory.py`]。
- provider 按 SPI 发现（`FactoryUtil.discoverFactory(..., AuthProviderFactory.class, tokenProvider)`），注册文件只列 Bear 与 DLF 两个 [来源: `P2.0/paimon-api/src/main/java/org/apache/paimon/rest/auth/AuthProviderFactory.java` ; `P2.0/paimon-api/src/main/resources/META-INF/services/org.apache.paimon.factories.Factory`]。推断：第三方 jar 可以注册自定义 provider，但所有引擎都要把这个 jar 放进 classpath。
- **没有 OAuth2/OIDC provider**，OpenAPI 只定义了 `BearerAuth` [来源: `P2.0/docs/static/rest-catalog-open-api.yaml`]。
- `dlf` 支持内联 STS token、本地 token 文件、ECS 实例角色三种来源 [来源: `P2.1/docs/docs/concepts/rest/dlf.md` 第 28–44 行]。
- 服务端下发表凭证：客户端设 `data-token.enabled=true`，由 `RESTTokenFileIO` 取用与续期；客户端会合并 `GET /v1/config` 的 overrides，服务端因此也能强制打开 [来源: https://github.com/apache/paimon/blob/release-1.3.1/paimon-common/src/main/java/org/apache/paimon/rest/RESTTokenFileIO.java ; https://github.com/apache/paimon/blob/release-1.3.1/paimon-api/src/main/java/org/apache/paimon/rest/responses/ConfigResponse.java]。

### 2.5 Paimon 项目是否提供可部署的 REST 服务端

**没有。**

- Java：`paimon-core/src/test/java/org/apache/paimon/rest/RESTCatalogServer.java`，类注释 "Mock REST server for testing"，基于 okhttp `MockWebServer`；2.1.0 与 master 仍在 test 目录 [来源: `P2.0/paimon-core/src/test/java/org/apache/paimon/rest/RESTCatalogServer.java` 第 189 行]。
- Python：`pypaimon/tests/rest/rest_server.py`，同为 "Mock REST server for testing" [来源: `P2.0/paimon-python/pypaimon/tests/rest/rest_server.py`]。
- Rust：`apache/paimon-rust` 的 `crates/paimon-rest-server` 以真实 `FileSystemCatalog` 为后端，README 写明 "for **local end-to-end testing** … a dev/testing tool and is **not published** to crates.io"，`Cargo.toml` 设 `publish = false` [来源: https://github.com/apache/paimon-rust/blob/v0.4.0/crates/paimon-rest-server/README.md]。
- 历史上的 `paimon-open-api` 构件（1.0.0–1.1.1）只是生成规范用的 Spring Boot 存根，返回写死的数据 [来源: https://github.com/apache/paimon/blob/release-1.0.0/paimon-open-api/src/main/java/org/apache/paimon/open/api/RESTCatalogController.java]。
- PIP-28 "Introduce REST Catalog" 原文 "Developers will need to create their own or opt for a hosted service" [来源: https://cwiki.apache.org/confluence/spaces/PAIMON/pages/327979149/PIP-28+Introduce+REST+Catalog]。
- apache/paimon#5966 "Paimon Rest Catalog Server"：2025-07-28 提出，open，0 条评论 [来源: https://github.com/apache/paimon/issues/5966]。

### 2.6 官方文档点名的 REST 实现

- **只有阿里云 DLF。** 2.1.0 REST 总览的 provider 表只列 "Alibaba Cloud DLF (`token.provider = dlf`)"；dlf.md 原文 "Use `token.provider = dlf` to authenticate a Paimon REST Catalog client with Alibaba Cloud DLF." [来源: `P2.1/docs/docs/concepts/rest/index.md` 第 62 行 ; `P2.1/docs/docs/concepts/rest/dlf.md` 第 28 行]。
- 没有出现 Gravitino、Polaris、Unity Catalog、Lakekeeper [来源: `P2.1/docs/docs/concepts/rest/`]。

### 2.7 REST 中的 `iceberg-table` 与 Iceberg 兼容元数据

- **`TableType` 有六种**：`table`、`format-table`、`materialized-table`、`object-table`、`lance-table`、`iceberg-table`（"An iceberg table, see '<https://iceberg.apache.org/'"）[来源>: `P2.0/paimon-api/src/main/java/org/apache/paimon/TableType.java` 第 28–38 行]。
- **`iceberg-table` 只有 REST catalog 能建，且只有元数据**：`IcebergTable` 注释 "paimon does not support read and write operation on this table yet"；`IcebergTableImpl` 实现 `ReadonlyTable`，`newScan()`、`newRead()` 都抛 `UnsupportedOperationException`；2.1.0 与 master 未变 [来源: `P2.0/paimon-core/src/main/java/org/apache/paimon/table/iceberg/IcebergTable.java` 第 29 行 ; `P2.0/paimon-core/src/main/java/org/apache/paimon/table/iceberg/IcebergTableImpl.java` 第 39、133–142 行]。PyPaimon 原文 "Metadata-only Iceberg table … does not support read / write" [来源: `P2.0/paimon-python/pypaimon/table/iceberg/iceberg_table.py`]。三个 ref 的 docs 都没有提到 `iceberg-table` [来源: `P2.0/docs/docs/concepts/rest/tables.mdx`]。colvenna-catalog 对这几种类型只在 PostgreSQL 登记元数据（[LLD](../design/colvenna-catalog-lld.md)"端点范围"）。
- **`metadata.iceberg.storage`** 取值 `disabled`（默认）/`table-location`/`hadoop-catalog`/`hive-catalog`/`rest-catalog`，三个 ref 一致；`metadata.iceberg.format-version` 默认 2，"only version 3 supports deletion vector" [来源: `P2.0/paimon-core/src/main/java/org/apache/paimon/iceberg/IcebergOptions.java` ; `P2.1/docs/docs/iceberg/catalogs.md`]。colvenna-lake 的取舍见它的开放层调研（Colvenna Lake 的 `docs/research/iceberg-open-layer.md`）。
- 推断：Paimon 侧"统一 Iceberg"目前只有"Paimon 表对外发布 Iceberg 元数据"这一条可用路径；要让一个 catalog 托管可读写的原生 Iceberg 表，只能在同一服务里另实现 Iceberg REST 协议。

## 3. 开源 Paimon REST 服务端

**结论：截至 2026-10-23，没有找到成熟、有用户、有正式发布的开源 Paimon REST 服务端。** 搜索方法：GitHub 仓库搜索（`paimon rest`、`paimon catalog`、`paimon rest catalog server` 等），各 catalog 项目的 issue/PR 搜索 "paimon"，`gh api` 读取候选仓库。GitHub 代码搜索的索引不完整，没出现在结果里不等于不存在。

| 实现 | 是什么 | 契约 | 许可证 | 发布 | 最近提交 | star/fork | 元数据 | 认证 | 表凭证 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Kasanari | 多格式 REST catalog（Iceberg、Paimon、Lance）；INTERNAL 自管元数据，PROXY 转发到 filesystem/jdbc/hive/rest | Paimon 1.4.2 OpenAPI，`/paimon/v1/{catalogId}` | Apache-2.0 | v0.3.0（2026-09-01），共 4 个 | 2026-09-01 | 0 / 0 | PostgreSQL、YugabyteDB | none、LDAP、OIDC；Casbin RBAC | 无（`getTableToken` 抛 `UnsupportedOperationException`） |
| melin/lakehouse-rest-catalog | "同一份元数据同时服务 Paimon 与 Iceberg"；另实现 Management API 与 Web 控制台 | 依赖 Paimon 2.0.0；README 称 Paimon 60 个端点、Iceberg 实现 30/35 | **无 LICENSE** | 无 | 2026-10-23（2026-10-09 建库） | 3 / 1 | MySQL 8.0（H2、PostgreSQL profile） | bearer + RBAC，`lakehouse.rest.auth.enabled` 默认 false，README 承认此时 `/v1/config` 与管理 API 匿名可调 | 有 `/token`，据称下发配置的静态密钥，未见 STS（未核实） |
| paimon-rust `paimon-rest-server` | 测试用服务端 | 子集：config、库表 CRUD、commit、snapshot、分区 | Apache-2.0 | 不发布（`publish = false`），随 v0.4.0（2026-10-12）源码 | 2026-10-22 | 208 / 104（整个仓库） | 本地 `FileSystemCatalog` | 无 | 无 |

来源：<https://github.com/nryanov/kasanari/blob/v0.3.0/docs/catalogs/paimon.md> ；<https://github.com/nryanov/kasanari/blob/v0.3.0/modules/catalog/catalog-paimon/src/main/java/kasanari/catalog/paimon/DefaultPaimonCatalogAdapter.java> 第 314–315 行 ；<https://github.com/melin/lakehouse-rest-catalog> （README）；<https://github.com/apache/paimon-rust/tree/v0.4.0/crates/paimon-rest-server> 。另有 `TKilome/paimon-rest-catalog`（0 star，只有设计文档）与 `zhang-jinlin-cm/my-paimon-rest-catalog`（空仓库）。

各大项目的情况：

- **Gravitino**：v1.3.1 的模块里服务端只有 `iceberg:iceberg-rest-server` 与 `lance:lance-rest-server`，Paimon 只有 `catalogs:catalog-lakehouse-paimon` [来源: https://github.com/apache/gravitino/blob/v1.3.1/settings.gradle.kts]。#7663 "[FEATURE] Support paimon rest server"（2025-07-11）仍 open；2025-07-31 PMC 成员 jerryshao 写道 "For such a big feature, it needs a vast community discussion and vote before making the decision"，并提出三点顾虑："Is the Paimon REST spec mature enough?"、"Are there many users who are keen on the Paimon rest catalog?"、Gravitino 已有统一 API 与 Iceberg REST；最后一条评论（2025-12-10）询问进展，无人回复 [来源: https://github.com/apache/gravitino/issues/7663]。Epic #7853 于 2025-09-23 由作者关闭，没有关联的代码 [来源: https://github.com/apache/gravitino/issues/7853]。Gravitino 是 Paimon REST 的**客户端**：`lakehouse-paimon` 的 rest 后端由 PR #9751（2026-01-29 合并）加入，文档示例是 DLF，`token-provider` 写 "`bearer` or `dlf`"（Paimon 的标识是 `bear`，Gravitino 是否做了映射未核实）[来源: https://github.com/apache/gravitino/pull/9751 ; https://github.com/apache/gravitino/blob/v1.3.1/docs/lakehouse-paimon-catalog.md 第 18、34、50–61 行]。
- **Polaris**：没有 Paimon REST 协议，只有 generic table 与 Spark 插件，见 4.1；#3343 "Support Apache Paimon as a first class citizen in the catalog."（2025-12-31）open [来源: https://github.com/apache/polaris/issues/3343]。
- **Lakekeeper**：#1244 问是否支持 Paimon REST，维护者只回了 "It should work, but I haven't tested it yet" 就关闭 [来源: https://github.com/lakekeeper/lakekeeper/issues/1244]。推断：Lakekeeper 的 generic table 不是 Paimon 的线协议，Paimon 客户端无法直连。
- **Unity Catalog OSS、Nessie、Amoro、HMS**：见第 4 节，都没有 Paimon REST 服务端。

## 4. 多格式 / Iceberg catalog 项目

### 4.1 Apache Polaris（`apache-polaris-1.8.0`）

- 许可证 Apache-2.0；ASF 顶级项目（2024-08-09 进入孵化，2026-02-15 毕业）；1.8.0 发布于 2026-09-28，之后无新版本（1.9.0 milestone 仍开着）[来源: https://github.com/apache/polaris/blob/apache-polaris-1.8.0/LICENSE ; https://incubator.apache.org/projects/polaris.html ; https://github.com/apache/polaris/releases/tag/apache-polaris-1.8.0]。
- Java 21 + Quarkus 3.39.3，捆绑 Iceberg 1.11.0 [来源: https://github.com/apache/polaris/blob/apache-polaris-1.8.0/gradle/libs.versions.toml]。
- 持久化：In-Memory（测试）、PostgreSQL（relational-jdbc，"Ready to use"）、MongoDB（实验）；1.8.0 起 JDBC 后端不再自动建 schema [来源: https://github.com/apache/polaris/blob/apache-polaris-1.8.0/site/content/in-dev/unreleased/metastores/_index.md ; https://github.com/apache/polaris/blob/apache-polaris-1.8.0/CHANGELOG.md]。
- 认证：`internal`（内置 token broker，`/v1/oauth/tokens` 用 OAuth2 client credentials 换 token）、`external`（Quarkus OIDC）、`mixed`；授权为内置 RBAC（principal → principal role → catalog role → privilege），可接 OPA，Ranger 授权器是扩展模块 [来源: https://github.com/apache/polaris/tree/apache-polaris-1.8.0/site/content/in-dev/unreleased/managing-security ; https://github.com/apache/polaris/blob/apache-polaris-1.8.0/extensions/auth/ranger/README.md]。
- 凭证下发：S3 STS AssumeRole、ADLS User Delegation SAS、GCS 降权 token；MinIO 可配 STS 端点；CHANGELOG 写 remote signing "not implemented" [来源: https://github.com/apache/polaris/blob/apache-polaris-1.8.0/site/content/in-dev/unreleased/vended-credentials.md]。
- **Paimon：只到登记级。** Generic Table API 独立于 Iceberg REST，路径 `/polaris/v1/{prefix}/namespaces/{ns}/generic-tables`；文档原文 "Generic tables are in beta"，`format` 示例为 "delta"、"csv"；限制写明 "There is no commit coordination provided by Polaris" 与 "Generic tables APIs do not support credential vending" [来源: https://github.com/apache/polaris/blob/apache-polaris-1.8.0/site/content/in-dev/unreleased/generic-table.md 第 25–40、164–167 行]。Paimon 只经 Polaris 自己的 Spark 插件：PR #3723 "Add Apache Paimon support to Polaris Spark Catalog"（2026-02-13 合并，1.8.0 已含 `PaimonHelper`）；PR #3820 "Integrate Paimon with Polaris without delegation mode"（2026-10-20 合并，**未发布**）改为由 Paimon `SparkCatalog` 在单独的 `paimon-warehouse` 管物理表，Polaris "additionally stores a Generic Table registration for unified discovery and `SHOW TABLES`"；README 同时写 "Generic tables (non-Iceberg tables) do not currently support credential vending" [来源: https://github.com/apache/polaris/pull/3723 ; https://github.com/apache/polaris/pull/3820 ; https://github.com/apache/polaris/blob/main/plugins/spark/README.md 第 35–56、148–150 行]。对应的 Paimon 侧需求 apache/paimon#7629（`DelegatingCatalogExtension`）open [来源: https://github.com/apache/paimon/issues/7629]。推断：Polaris 的"统一"是发现层的统一，Paimon 表的提交、凭证仍在 Paimon 自己的 catalog 里，不能替代 Paimon REST 服务端。
- **Iceberg**：完整 Iceberg REST，含视图、`/v1/{prefix}/transactions/commit`、register、`/credentials`、Polaris 扩展的 `/notifications`（colvenna-lake 开放层所用）、联邦到 Iceberg REST/HMS/BigQuery Metastore；无 `/plan` 路由 [来源: https://github.com/apache/polaris/blob/apache-polaris-1.8.0/spec/generated/bundled-polaris-catalog-service.yaml]。
- 托管版：Snowflake Open Catalog 原文 "a managed service for Apache Polaris"，见第 6 节。

### 4.2 Apache Gravitino（`v1.3.1`）

- 许可证 Apache-2.0；ASF 顶级项目（2024-06-04 进入孵化，2025-05 毕业）；v1.3.1 发布于 2026-09-30 [来源: https://github.com/apache/gravitino/blob/v1.3.1/LICENSE ; https://incubator.apache.org/projects/ ; https://github.com/apache/gravitino/releases/tag/v1.3.1]。
- Java 17、Jetty 9.4 + Jersey；实体存储默认 H2（文档称只用于开发），生产可用 MySQL 5.7/8.0、PostgreSQL 12–16 [来源: https://github.com/apache/gravitino/blob/v1.3.1/docs/how-to-use-relational-backend-storage.md]。
- 认证 simple、Basic、Kerberos、OAuth（JWT/JWKS/OIDC）；RBAC 支持 ALLOW/DENY，可把授权下推到 Ranger（覆盖 Hive、Iceberg、Paimon catalog）[来源: https://github.com/apache/gravitino/blob/v1.3.1/docs/security/how-to-authenticate.md ; https://github.com/apache/gravitino/blob/v1.3.1/docs/security/access-control.md ; https://github.com/apache/gravitino/blob/v1.3.1/docs/security/authorization-pushdown.md]。
- 凭证下发：S3（STS、静态密钥、IRSA）、GCS、ADLS、OSS；不支持 remote signing [来源: https://github.com/apache/gravitino/blob/v1.3.1/docs/security/credential-vending.md]。
- **Paimon：连接器级。** "Builds with Apache Paimon `1.2`"（`paimon = '1.2.0'`），`catalog-backend` "Supports `filesystem`, `jdbc`, `hive` and `rest`" [来源: https://github.com/apache/gravitino/blob/v1.3.1/docs/lakehouse-paimon-catalog.md ; https://github.com/apache/gravitino/blob/v1.3.1/gradle/libs.versions.toml 第 74 行]。无 Paimon REST 服务，见第 3 节。
- **Iceberg**：Iceberg REST server 可独立部署或作 auxiliary 服务，基于 Iceberg 1.11 REST API，有服务端 scan planning（`POST .../plan`），文档原文 "Iceberg table only" [来源: https://github.com/apache/gravitino/blob/v1.3.1/docs/iceberg-rest-service.md ; https://github.com/apache/gravitino/blob/v1.3.1/iceberg/iceberg-rest-server/src/main/java/org/apache/gravitino/iceberg/service/rest/IcebergTableOperations.java]。
- 推断：Gravitino 的"统一"是自有的统一元数据 API 加各格式连接器；Paimon 客户端要经 Gravitino 访问 Paimon 表，只能用 Gravitino 自己的 Flink/Spark connector，不能用 `metastore=rest`。

### 4.3 Unity Catalog OSS（`v0.7.0`）

- 许可证 Apache-2.0，贡献需 DCO；LF AI & Data 的 sandbox 项目，原文 "Unity Catalog is a sandbox-stage project of LF AI & Data Foundation"；v0.7.0 发布于 2026-10-13 [来源: https://github.com/unitycatalog/unitycatalog/blob/v0.7.0/LICENSE ; https://lfaidata.foundation/projects/unity-catalog/]。
- Java（server `--release 17`）、Armeria、Hibernate；默认 H2，可配 MySQL/PostgreSQL [来源: https://github.com/unitycatalog/unitycatalog/blob/v0.7.0/build.sbt ; https://github.com/unitycatalog/unitycatalog/blob/v0.7.0/docs/server/deployment.md]。
- 认证：外部 OAuth/OIDC IdP 的 token 经 `POST /auth/tokens` 换成 UC 自签 token；权限默认 `server.authorization=disable` [来源: https://github.com/unitycatalog/unitycatalog/blob/v0.7.0/docs/server/auth.md]。凭证：S3 STS AssumeRole、ADLS、GCS [来源: https://github.com/unitycatalog/unitycatalog/blob/v0.7.0/docs/server/aws.md]。
- **Paimon：无**。`DataSourceFormat` 只有 DELTA、ICEBERG、CSV、JSON、AVRO、PARQUET、ORC、TEXT；仓库 issue/PR 搜 "paimon" 为 0 [来源: https://github.com/unitycatalog/unitycatalog/blob/v0.7.0/api/all.yaml 第 1686–1697 行]。
- **Iceberg**：`/api/2.1/unity-catalog/iceberg` 部分 Iceberg REST；0.7.0 起原生 Iceberg 表默认开；无视图（`loadView` 返回 404）、无 `transactions/commit`、无 register、无 `/plan` [来源: https://github.com/unitycatalog/unitycatalog/blob/v0.7.0/server/src/main/java/io/unitycatalog/server/service/IcebergRestCatalogService.java]。

### 4.4 Lakekeeper（`v0.14.1`）

- 许可证 Apache-2.0（"Copyright 2024-2026 Vakamo Inc."），提交者须签 CLA，Cedar 授权只在商业版 Lakekeeper Plus；公司主导 [来源: https://github.com/lakekeeper/lakekeeper/blob/v0.14.1/LICENSE ; https://github.com/lakekeeper/lakekeeper/blob/v0.14.1/CONTRIBUTING.md]。
- Rust；只支持 PostgreSQL ≥15；认证为外部 OIDC（本地按 JWKS 验 JWT）或 K8s service account，自己不签 API key；授权默认 OpenFGA（需另部署）[来源: https://github.com/lakekeeper/lakekeeper/blob/v0.14.1/docs/docs/authentication.md ; https://github.com/lakekeeper/lakekeeper/blob/v0.14.1/docs/docs/authorization.md]。
- 凭证：S3 STS（含 session tag）+ remote signing、S3 兼容（MinIO、Ceph）、R2、OSS、ADLS、GCS [来源: https://github.com/lakekeeper/lakekeeper/blob/v0.14.1/README.md]。
- **Paimon：无。** v0.13.0 起有 Generic Table API（"Generic Table API with Lance (#1673)"），`format` 为不透明字符串，支持凭证下发，不做提交协调 [来源: https://github.com/lakekeeper/lakekeeper/blob/v0.14.1/docs/docs/generic-tables.md]。
- **Iceberg**：Iceberg REST，含 `/transactions/commit`、视图、remote signing [来源: https://github.com/lakekeeper/lakekeeper/tree/v0.14.1/crates/lakekeeper/src/api/iceberg/v1]。

### 4.5 Project Nessie（`nessie-0.109.0`）

- 许可证 Apache-2.0；无基金会，NOTICE 为 "Copyright 2015-2025 Dremio Corporation"；0.109.0 发布于 2026-10-08；2026-07-25 以来 404 个提交中 361 个来自 renovate 机器人 [来源: https://github.com/projectnessie/nessie/blob/nessie-0.109.0/NOTICE ; https://github.com/projectnessie/nessie/releases]。
- Java 21 + Quarkus；持久化 RocksDB（单节点）、BigTable、MongoDB、PostgreSQL 等 [来源: https://projectnessie.org/nessie-latest/configuration/]。
- 认证默认关，可校验 OIDC bearer；自己不签 token（`/iceberg/v1/oauth/tokens` 回 501）；CEL 授权规则 [来源: https://projectnessie.org/nessie-latest/authentication/ ; https://projectnessie.org/nessie-latest/authorization/]。
- 凭证：S3 STS AssumeRole（默认关）+ remote signing（默认开）、GCS（实验）、ADLS SAS [来源: https://github.com/projectnessie/nessie/tree/nessie-0.109.0/catalog/files/api/src/main/java/org/projectnessie/catalog/files/config]。
- **Paimon：无**；唯一相关的是 PR #11973（接受 field ID 0，让 Paimon 生成的 Iceberg 元数据能加载）[来源: https://github.com/projectnessie/nessie/pull/11973]。
- **Iceberg**：自 0.90.2 起提供 Iceberg REST，文档原文 "Support for Iceberg REST is currently considered experimental in Nessie!"；有视图与原子多表提交 [来源: https://projectnessie.org/guides/iceberg-rest/]。

### 4.6 Apache Amoro（`v0.8.1-incubating`）

- 许可证 Apache-2.0；ASF 孵化中（2024-03-11 起）；最新正式版 0.8.1-incubating（2025-09-11），0.9.0 已打到 rc13，2026-10-14 开始投票 [来源: https://incubator.apache.org/projects/amoro.html ; https://amoro.apache.org/download/ ; https://lists.apache.org/thread/0j80o73649qwg1p590h58tyykxzk8no9]。
- 自身数据库 derby（默认）/MySQL/PostgreSQL；dashboard 认证 token 或 basic [来源: https://amoro.apache.org/docs/latest/managing-catalogs/]。
- **Iceberg REST 端点免认证**：`RestCatalogService.ICEBERG_REST_API_PREFIX + "/*"` 在 `DashboardServer.URL_WHITE_LIST` 中 [来源: https://github.com/apache/amoro/blob/v0.8.1-incubating/amoro-ams/src/main/java/org/apache/amoro/server/dashboard/DashboardServer.java 第 421–442 行]。无凭证下发。
- **Paimon：连接器级、只展示。** Paimon catalog 只能是 Hadoop（映射为 Paimon `filesystem`）或 Hive 类型；`RuntimeHandlerChain` 注释 "Currently, paimon is unsupported"，自优化只适用于 Iceberg 与 Mixed 格式 [来源: https://amoro.apache.org/docs/latest/paimon-format/ ; https://amoro.apache.org/docs/latest/configurations/]。

### 4.7 Apache Hive Metastore（`rel/release-4.2.1`）

- 许可证 Apache-2.0；ASF 顶级项目；4.2.1 发布于 2026-08-24；4.1.0（2025-07-31）首次提供独立 Metastore 发行包与 "a REST-based catalog server backed by the Hive Metastore" [来源: https://hive.apache.org/general/downloads/]。
- Iceberg REST 在 `standalone-metastore/metastore-rest-catalog`，默认关闭（`metastore.catalog.servlet.port=-1`），认证 none/simple/jwt/oauth2（默认 jwt）；`transactions/commit` 的源码注释写明 "does not guarantee true transactional atomicity"；无凭证下发 [来源: https://hive.apache.org/docs/latest/admin/iceberg-rest-catalog/ ; https://github.com/apache/hive/blob/rel/release-4.2.1/standalone-metastore/metastore-rest-catalog/src/main/java/org/apache/iceberg/rest/HMSCatalogAdapter.java]。
- Paimon：只有 Paimon 客户端侧的 `metastore=hive` [来源: https://paimon.apache.org/docs/2.1/concepts/catalog/]。

### 4.8 Apache XTable（`0.4.0-incubating`）与 Iceberg 参考服务端

- XTable：ASF 孵化中；0.4.0（2026-08-24）起支持 "(as a source) Apache Paimon"，没有 Paimon target；直接按存储路径读表（`FileStoreTableFactory`），不经 Paimon catalog；捆绑 Paimon 1.3.1；REST service 模块 README 写 "ships with no authentication and no authorization" [来源: https://xtable.apache.org/releases/release-0.4.0-incubating/ ; https://github.com/apache/incubator-xtable/tree/0.4.0-incubating/xtable-core/src/main/java/org/apache/xtable/paimon ; https://github.com/apache/incubator-xtable/blob/main/xtable-service/README.md]。推断：XTable 能把 Paimon 表翻译成 Iceberg 元数据，作用与 Paimon 自带的 `metadata.iceberg.storage` 重叠，不是 catalog。
- Iceberg 的 `apache/iceberg-rest-fixture`：README 标题 "Iceberg REST Catalog Adapter Test Fixture"，服务类在 `open-api/src/testFixtures`，无认证 [来源: https://github.com/apache/iceberg/blob/apache-iceberg-1.12.0/docker/iceberg-rest-fixture/README.md]。即 Iceberg 与 Paimon 一样，官方只给测试服务端；不同的是 Iceberg 生态已有多家生产级服务端（4.1–4.5）。

## 5. 引擎接入 Paimon REST 的现状

依据：引擎发布标签的构建文件与源码。"推断兼容"的依据是：Paimon 1.3.1 起客户端已有 `uri`、`token`、`token.provider`（`bear`/`dlf`）与 `data-token.enabled`，OpenAPI `info.version` 一直是 "1.0"，2.0.0 相对 1.4.2 只增加了 `partitions/drop`、`partitions/list-by-filter` 两条路径 [来源: https://github.com/apache/paimon/blob/release-1.3.1/paimon-api/src/main/java/org/apache/paimon/rest/auth/AuthProviderEnum.java ; `P2.0/docs/static/rest-catalog-open-api.yaml`]。只比了路径，没比请求与响应的 schema，也没有端到端测试。

| 引擎 | 最新发布 | 捆绑 Paimon | REST catalog（配置项） | bear | dlf | data-token（表凭证） | 对 Paimon 2.0 契约 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Flink（Paimon 连接器） | Paimon 2.0.0 / 2.1.0，支持 Flink 1.16–1.20、2.0–2.2 | 同版本 | `metastore=rest` | 有文档 | 有文档 | 原生 | 原生 |
| Spark（Paimon 连接器） | 同上，支持 Spark 3.2–3.5、4.0、4.1 | 同版本 | `metastore=rest` | 有文档 | 有文档 | 原生 | 原生 |
| Doris | 4.1.5（2026-10-15） | 1.4.2；master 2026-10-23 升到 2.1.0（未发布） | `paimon.catalog.type=rest`，`paimon.rest.*` 去前缀透传 | 未文档化，可透传（推断） | 有文档 | **已实现**：`RESTTokenFileIO` 的凭证合并进 BE 存储属性 | 推断（1.4.2 客户端） |
| StarRocks | 4.1.6（tag 2026-09-29） | 1.3.1；main 2026-09-23 升到 2.0.0（未发布） | 文档只写 `filesystem`/`hive`；代码把 `paimon.catalog.type` 原样作 `metastore`，`paimon.option.*` 透传 | 推断 | 推断（`dlf` 在 StarRocks 是另一种类型） | **无**：仓库搜不到 `RESTTokenFileIO`/`data-token`，BE 原生读用 StarRocks 自己的存储凭证 | 推断（1.3.1 客户端） |
| Trino（`apache/paimon-trino`） | 无 release、无 tag；main 为 1.3-SNAPSHOT、Trino 476 | 1.3.1 | 文档无；catalog 配置整体转为 Paimon `Options`（推断可用） | 推断 | 推断 | 未核实 | 推断 |
| Trino 本体 | 无 Paimon connector；PR #30913 "Add Paimon 2.0.0 connector" 未合并即关闭 | — | — | — | — | — | — |
| Amazon EMR | 7.14.0（2026-09-08 首发） | **不含 Paimon** | — | — | — | — | — |
| Amazon Athena | — | 官方文档未见 Paimon | — | — | — | — | — |

- **Flink / Spark**：Paimon 2.0.0 文档列 Flink 2.2、2.1、2.0、1.20、1.19、1.18、1.17、1.16；Spark 4.1、4.0（Java 17、Scala 2.13）与 3.5–3.2；2.1.0 兼容矩阵为 Flink 1.16–1.20 与 2.0–2.2、Spark 3.2–3.5 与 4.0、4.1 [来源: `P2.0/docs/docs/flink/quick-start.mdx` 第 36 行 ; `P2.0/docs/docs/spark/quick-start.mdx` 第 34–38 行 ; `P2.1/docs/docs/ecosystem/index.md` 第 45–60 行]。
- **Doris**：4.1.4 与 4.1.5 的 `fe/pom.xml` 都是 `paimon.version` 1.4.2，master 为 2.1.0（PR #69343 "`[improvement](fe) Upgrade Paimon to 2.1.0`"，2026-10-23 合并，提交 `949a8061`）[来源: https://github.com/apache/doris/blob/4.1.5-release/fe/pom.xml ; https://github.com/apache/doris/pull/69343]。`paimon.catalog.type` 可选 `dlf`、`filesystem`、`hms`、`rest`、`jdbc`；REST 属性由 `PaimonRestMetaStoreProperties` 处理，`paimon.rest.token.provider` 必填 [来源: https://github.com/apache/doris/blob/4.1.4/fe/fe-core/src/main/java/org/apache/doris/datasource/property/metastore/PaimonPropertiesFactory.java ; https://github.com/apache/doris/blob/4.1.4/fe/fe-core/src/main/java/org/apache/doris/datasource/property/metastore/PaimonRestMetaStoreProperties.java]。下发凭证由 `PaimonVendedCredentialsProvider` 读 `RESTTokenFileIO.validToken()` [来源: https://github.com/apache/doris/blob/4.1.4/fe/fe-core/src/main/java/org/apache/doris/datasource/paimon/PaimonVendedCredentialsProvider.java]。4.x 文档的类型列表里没有 `rest`，REST 只出现在 DLF 示例里 [来源: https://doris.apache.org/docs/4.x/lakehouse/catalogs/paimon-catalog]。**Doris 官方已发布的版本还没有原生捆绑 Paimon 2.0**；colvenna-lake 在 4.1.4 上自建 Paimon 2.0.0 读取器的做法仍然必要（Colvenna Lake 的 M0 能力矩阵 `docs/research/version-capability.md`）。
- **StarRocks**：4.1.6 与 4.0.16 的 `fe/pom.xml` 为 Paimon 1.3.1；GitHub Release 对象最新只到 4.1.3（2026-09-17），4.1.6 只有 tag [来源: https://github.com/StarRocks/starrocks/blob/4.1.6/fe/pom.xml 第 82 行 ; https://github.com/StarRocks/starrocks/releases]。文档 "`paimon.catalog.type` … Set this parameter to `filesystem` or `hive`" [来源: https://github.com/StarRocks/starrocks/blob/4.1.6/docs/en/data_source/catalog/paimon_catalog.md 第 131 行]。源码只特判 `hive` 与 `dlf`，其余原样交给 Paimon，`paimon.option.*` 去前缀透传 [来源: https://github.com/StarRocks/starrocks/blob/4.1.6/fe/fe-core/src/main/java/com/starrocks/connector/paimon/PaimonConnector.java 第 75–125 行]。main 由提交 `87d0a0c4e673`（PR #79390，2026-09-23）升到 Paimon 2.0.0 [来源: https://github.com/StarRocks/starrocks/blob/main/fe/pom.xml 第 84 行]。社区需求 #80530 "Support paimon catalog rest"（2026-10-13）open [来源: https://github.com/StarRocks/starrocks/issues/80530]。推断：StarRocks 4.1.6 即使透传 `rest` 能连上 catalog，没有下发凭证就要给 StarRocks 自己配宽的 S3 凭证，与 colvenna-catalog "引擎不持有对象存储凭证"的模型冲突。
- **Trino**：paimon-trino main 的 `pom.xml` 为 Paimon 1.3.1、Trino 476，最后提交 2026-03-17；Paimon 2.1 下载页只有 snapshot [来源: https://github.com/apache/paimon-trino/blob/main/pom.xml ; `P2.1/docs/docs/project/download.mdx`]。
- **EMR / Athena**：EMR 7.14.0 发布页中 "paimon" 出现 0 次；Athena 用户指南目录有 Iceberg、Hudi、Delta，没有 Paimon [来源: https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-7140-release.html ; https://docs.aws.amazon.com/athena/latest/ug/toc-contents.json]。

## 6. 托管产品的收费

访问日期 2026-10-23。估算假设同第 1.4 节。

- **阿里云 DLF 2.0**：计费概览原文 "DLF is billed per catalog. Costs include lake table storage fees and compute resource fees."；计费项为存储容量、数据请求、数据取回、临时存储与计算 CU；"Charges for GET and PUT data requests are currently waived"；元数据或 catalog API 调用**不在计费项里**（页面没有明说免费，只在欠费处写 "The system will block calls to the metadata API"）[来源: https://www.alibabacloud.com/help/en/dlf/dlf-2-0/product-overview/billing （页面更新于 2026-10-23）]。CU（1 CU = 1 vCPU + 4 GiB）按量 $0.06（杭州）/ $0.09（新加坡）/ $0.07（弗吉尼亚）每 CU-小时，包月 $31.26 / $45.94 / $35.96，消耗于压缩、快照与分区清理、孤儿文件清理、bucket rebalance [来源: https://www.alibabacloud.com/help/en/dlf/dlf-2-0/product-overview/compute-resource-fees]。DLF 同时提供 Paimon REST（`http://{region}-vpc.dlf.aliyuncs.com`，`token.provider=dlf`，示例 POM 用 Paimon 1.4.2）与 Iceberg REST（`…/iceberg`，SigV4 签名名 `DlfNext`，需 `pyiceberg-dlf`，"cannot coexist" 官方 pyiceberg）[来源: https://www.alibabacloud.com/help/en/dlf/dlf-2-0/user-guide/access-dlf-paimon-tables-via-java-api ; https://www.alibabacloud.com/help/en/dlf/dlf-2-0/user-guide/access-iceberg-tables-using-pyiceberg]。估算：catalog **$0**；维护任务若每月 100 CU-小时，弗吉尼亚 $7。
- **AWS Glue Data Catalog**：存储 "up to a million metadata objects for free"，之后 "$1.00 per 100,000 objects over a million, per month"；对象包括 "a table, table version, partition, partition indexes, statistics, database, or catalog"；请求前 100 万次免费，示例按超出 100 万次计 $1；压缩与统计 $0.44/DPU-小时 [来源: https://aws.amazon.com/glue/pricing/]。Iceberg REST 端点的计费方式页面未写。估算：1 万张表加表版本仍在 100 万个对象以内，$0；请求 (1000 万 − 100 万) × $1/100 万 = **$9.00**。
- **Amazon S3 Tables（us-east-1，公开价格表）**：存储 $0.0265/GB-月（前 50 TB）；PUT/COPY/POST/LIST $0.005/千次；GET 等 $0.0004/千次；监控 $0.025/千个对象-月；压缩 $0.002/千个对象 + $0.005/GB（binpack）或 $0.01/GB（sort/z-order）[来源: https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-east-1/index.json （usagetype `Tables-*`）; https://aws.amazon.com/s3/pricing/]。只支持 Iceberg 格式。估算：900 万次读 × $0.0004/千 = $3.60，100 万次写或列举 × $0.005/千 = $5.00；监控费 20 万个对象 $5.00，或 1000 万个对象（小文件多）$250.00；合计 **$13.60–$258.60**；1 TB 存储 $27.14 另计。Iceberg REST 调用如何映射为计费请求未核实。
- **Snowflake Open Catalog**：原文 "Open Catalog is currently free to use with general availability."、"Billing will begin in the first half of 2026."（取数日已过了这个时间，页面未更新）；收费后只按 REST API 请求计；"Customers who haven't previously created a Snowflake Open Catalog account can't sign up for their first Open Catalog account"，新客户被引导到 Horizon Catalog [来源: https://docs.snowflake.com/user-guide/opencatalog/overview]。服务消耗表（2026-10-15 生效）："Open Catalog … 0.5 Platform Credits per 1 million requests"，脚注 "available free of charge for a limited period of time"；AWS US East 按需 credit 价 Standard $2、Enterprise $3、Business Critical $4 [来源: https://www.snowflake.com/legal-files/CreditConsumptionTable.pdf]。估算：当前 **$0**；开始收费后 5 credit，即 $10–$20。
- **Databricks Unity Catalog**：原文 "No separate license is required. Unity Catalog is included with Databricks Premium and Enterprise tiers. You only pay for the compute and storage you use on the platform" [来源: https://www.databricks.com/product/unity-catalog]。估算：增量 **$0**，前提是已买 Databricks 平台。
- **Google Lakehouse runtime catalog**：原文 "As of April 20th, 2026, BigLake is now called Lakehouse."、"BigLake metastore is now called the Lakehouse runtime catalog."，支持 Iceberg REST，页面未提 Paimon [来源: https://docs.cloud.google.com/bigquery/docs/about-blms]。Class A（Create、Register、List、Update、setIamPolicy）前 5,001 次免费，之后 $6.00/100 万次；Class B（Read、Delete、getIamPolicy）前 50,001 次免费，之后 $0.90/100 万次；元数据文件超过 1 MB 时每多 1 MB 计 1 次；"The list prices depend on the region" [来源: https://cloud.google.com/products/lakehouse/pricing]。估算：假设 100 万次 Class A、900 万次 Class B，$5.97 + $8.05 = **$14.02**；注意 List 属于 Class A，全按 Class A 计是 $59.97。
- **Cloudflare（Basin Catalog 定价页）**：catalog 操作每月含 100 万次，之后 $9.00/100 万次；压缩 $0.005/GB 与 $2.00/100 万个对象（各有免费额度）；"Snapshot expiration is free of charge" [来源: https://developers.cloudflare.com/basin-catalog/platform/pricing/]。页面没有明说它原名 R2 Data Catalog（未核实）。估算：**$81.00**。

## 7. colvenna-catalog 的部署成本与无服务器可行性

### 7.1 仓库已有的估算

Colvenna Lake 的成本估算 `docs/manual/cost.md`（2026-10-22，基准区域 `us-west-2`，按需价）**没有单列 colvenna-catalog**，它算的是整套平台。与 colvenna-catalog 相关的项：

- colvenna-catalog 跑在共享的写入节点上（t3.xlarge，$0.1664/小时，每台每月 $121.47）；平台全部 Pod 的 CPU requests 约 5.1 个，两类节点 `allocatable` 约 15.7 个 CPU（cost.md 第 2、7 节）。chart 设 2 副本，每副本 requests 500m CPU、1 GiB，limits 2 CPU、2 GiB（`deploy/helm/colvenna-catalog/values.yaml` 第 6、72–77 行）。推算：按 CPU requests 折算，两副本约占一台写入节点的 1/4，约每月 $30；节点是共享的，去掉 catalog 也省不下这笔钱。
- 专属项：catalog 数据库账号的 Secret $0.40/月；ECR 仓库 `colvenna-catalog` 9 个镜像约 2.3 GiB，约 $0.23/月（cost.md 第 2 节）。
- 项目 RDS（db.t4g.small $23.36 + 存储 $2.30，cost.md 第 2 节）按本次口径不计入。EKS 控制面 $73、NAT $33、ALB $31 等是平台固定项，节点缩到 0 后平台仍剩约 $199/月（cost.md 第 1 节）。

### 7.2 计算层方案的事实（us-east-1，访问 2026-10-23）

#### Lambda + 带鉴权的 API Gateway（Lambda Function URL 不考虑）

- 运行时 `java21`（Amazon Linux 2023）与 `java25` 都在，java21 计划 2029-06-30 弃用 [来源: https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html]。
- SnapStart 原文 "SnapStart supports the following Lambda managed runtimes and the corresponding AWS Lambda base images across ZIP and container image deployment format: Java 11 and later …"；不支持预置并发、EFS、"ephemeral storage greater than 512 MB"；只能用于已发布版本与别名；"For Java managed runtimes, there's no additional cost for SnapStart"；恢复后 "The state of connections that your Event Function establishes during the initialization phase isn't guaranteed" [来源: https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html]。恢复与 `afterRestore` 钩子须在 10 秒内完成；不用 SnapStart 时 Init 阶段也限 10 秒；Java 快照 14 天未调用会被删除 [来源: https://docs.aws.amazon.com/lambda/latest/dg/snapstart-runtime-hooks-java.html ; https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html ; https://docs.aws.amazon.com/lambda/latest/dg/snapstart-activate.html]。
- 配额：超时 900 秒；内存 128–10,240 MB；zip 包含层解压后 250 MB，容器镜像 10 GB；"Invocation payload (request and response) 6 MB each for request and response (synchronous)"，流式响应 200 MB；默认账号并发 1,000；每个函数每 10 秒最多扩 1,000 个执行环境；一个执行环境同一时刻只处理一个请求 [来源: https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html ; https://docs.aws.amazon.com/lambda/latest/dg/scaling-behavior.html ; https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html]。
- 价格：x86 $0.0000166667/GB-秒，Arm $0.0000133334/GB-秒，请求 $0.20/100 万次；每月免费 100 万次请求与 40 万 GB-秒 [来源: https://aws.amazon.com/lambda/pricing/]。
- VPC：连到 VPC 后 "it can only access resources available within that VPC"，要上网或访问 AWS API 需 NAT 或 VPC 端点；Hyperplane ENI 由 Lambda 管理，函数 14 天空闲会回收 ENI 并进入 `Inactive`，下次调用失败并重建 [来源: https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html]。STS 有接口端点 `com.amazonaws.<region>.sts` [来源: https://docs.aws.amazon.com/vpc/latest/privatelink/aws-services-privatelink-support.html]；接口端点按 "$0.01 per hour for each endpoint ENI"（每 AZ）加 $0.01/GB [来源: https://aws.amazon.com/privatelink/pricing/]；S3 网关端点 "There is no additional charge for using gateway endpoints" [来源: https://docs.aws.amazon.com/vpc/latest/privatelink/gateway-endpoints.html]。
- API Gateway：REST API 支持 IAM、资源策略、Cognito、Lambda authorizer；HTTP API 支持 IAM、Cognito（JWT）、Lambda authorizer、JWT authorizer；WAF、私有端点、响应流只在 REST API [来源: https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html]。HTTP API "Maximum integration timeout 30 seconds"（不可调）、"Payload size 10 MB"；REST API Regional/私有 API 集成超时默认上限 29 秒，可申请调高（可能要降区域限流），edge-optimized 不可调，载荷 10 MB [来源: https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html ; https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-execution-service-limits-table.html]。价格 "you pay only for the API calls you receive"：HTTP API $1.00/100 万次（前 3 亿），REST API $3.50/100 万次（前 3.33 亿）[来源: https://aws.amazon.com/api-gateway/pricing/]。
- 现有 HTTP 服务上 Lambda：`aws/aws-lambda-web-adapter`（Apache-2.0，AWS 官方 GitHub 组织）原文 "Supports any web frameworks and languages"，支持 API Gateway REST/HTTP API 与 ALB 触发，支持 SnapStart 钩子 [来源: https://github.com/aws/aws-lambda-web-adapter]。它也支持 Function URL，此项不用。另有 Lambda Web Functions，原文 "currently support Node.js runtimes only"，自带公网 HTTPS 端点，对 Java 21 不适用，也不在考虑之列 [来源: https://docs.aws.amazon.com/lambda/latest/dg/web-functions.html]。
- ALB 直连 Lambda：请求体与响应各 1 MB 上限 [来源: https://docs.aws.amazon.com/elasticloadbalancing/latest/application/lambda-functions.html]。

#### ECS on Fargate

- 价格：x86 $0.04048/vCPU-小时 + $0.004445/GB-小时，Arm $0.03238 + $0.00356；按秒计费，最少 1 分钟 [来源: https://aws.amazon.com/fargate/pricing/]。最小任务 0.25 vCPU / 0.5 GB [来源: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html]。
- 缩到 0：原文 "If you want your task count to scale to zero when there's no work to be done, set a minimum capacity of 0. With target tracking scaling policies, when actual capacity is 0 and the metric indicates that there is workload demand, Service Auto Scaling waits for one data point to be sent before scaling out." [来源: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html]；目标跟踪 "does not perform scaling when the specified metric has insufficient data" [来源: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-autoscaling-targettracking.html]。ALB 在目标组 "have no registered targets" 时返回 503 [来源: https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-troubleshooting.html]；ALB $0.0225/小时 + $0.008/LCU-小时 [来源: https://aws.amazon.com/elasticloadbalancing/pricing/]。
- ECS Express Mode 一次调用建好 Fargate 服务、ALB 与自动扩缩，"There is no additional charge" [来源: https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html]。

#### App Runner

- 原文 "AWS App Runner is no longer open to new customers. Existing customers can continue to use the service as normal … we do not plan to introduce new features."，推荐迁到 ECS Express Mode [来源: https://docs.aws.amazon.com/apprunner/latest/dg/apprunner-availability-change.html]。
- 空闲仍计费："When your application is idle, you pay per GB of memory for provisioned container instances"（$0.007/GB-小时），默认最少 1 个预置实例；只有暂停服务才不计费 [来源: https://aws.amazon.com/apprunner/pricing/]。

#### EKS（现有部署方式）

- HPA：EKS 文档要求先装 Metrics Server，示例 `--min=1 --max=10` [来源: https://docs.aws.amazon.com/eks/latest/userguide/horizontal-pod-autoscaler.html]。Kubernetes 文档：HPA 缩到 0 "Beta since Kubernetes v1.37; enabled by default"，只对 object/external 指标生效，由 `HPAScaleToZero` 特性门控制 [来源: https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/]；1.36 的特性门表中 `HPAScaleToZero` 为 `false`、Alpha [来源: https://v1-36.docs.kubernetes.io/docs/reference/command-line-tools-reference/feature-gates/]；EKS 版本页最新为 Kubernetes 1.36 [来源: https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html]。
- KEDA：CNCF 毕业项目（2023-08-22）；0↔1 由 KEDA operator 决定，1↔N 由它生成的 HPA 负责 [来源: https://www.cncf.io/projects/keda/ ; https://keda.sh/docs/2.21/concepts/scaling-deployments/]。按 HTTP 请求扩缩要用 KEDA HTTP Add-on，README 原文 "scale their HTTP servers up and down (including to/from zero) based on incoming HTTP traffic"、"**Project status: beta**"，最新 v0.16.0（2026-09-16）[来源: https://github.com/kedacore/http-add-on]。
- colvenna-catalog 的 chart 现状：`replicas: 2`，PDB `minAvailable: 1`，无 HPA（`deploy/helm/colvenna-catalog/values.yaml`、`templates/pdb.yaml`）。

#### 月费示例（估算，us-east-1，数据库不计）

| 方案 | 常驻 / 空闲时 | 1000 万次请求/月时 |
| --- | --- | --- |
| EKS（现状） | 按 CPU requests 折算约 $30（推算，见 7.1）；节点与控制面照常计费 | 同左 |
| Fargate 常驻 2 个任务（0.5 vCPU、1 GB） | x86 2 × 730 × $0.024685 = $36.04；Arm $28.84；外加 ALB $16.43 + LCU | 同左（不扩容时） |
| Fargate 最小任务数 0 | 任务 $0；ALB $16.43 照计；冷启动期间请求得到 503 | 视任务运行时长 |
| Lambda + HTTP API | $0（若连 VPC：STS 接口端点 2 个 AZ 约 $14.60，或 NAT） | 假设平均 100 ms、2,048 MB：计算 (200 万 − 40 万) GB-秒 × $0.0000166667 = $26.67，请求 $1.80，HTTP API $10.00，合计约 **$38.47**（REST API 为 $63.47）；Lambda authorizer 调用另计 |
| App Runner | 新客户不可用 | — |

### 7.3 由事实推出的影响

1. **请求体上限对不上。** colvenna-catalog 接受最大 64 MiB 的请求体（[LLD](../design/colvenna-catalog-lld.md)"资源保护"），Lambda 同步调用 6 MB、API Gateway 10 MB、ALB→Lambda 1 MB，API Gateway 也不支持请求流。超过的提交（例如带大量分区统计的 commit）会在网关失败；实际 Paimon 请求的大小分布未测。
2. **超时对不上。** 主池 `lock_timeout` 30 秒，S3 客户端调用超时 30 秒（LLD"连接池""提交协议"），HTTP API 集成超时 30 秒不可调，REST API 默认 29 秒。推断：持锁等待的提交可能在网关先超时，客户端得到 504，函数仍在执行；Paimon 客户端随后重试，按 LLD 的提交重放逻辑应只生效一次，但没有验证。
3. **按表凭证的签发方式不能原样搬到 Lambda 或 Fargate。** 现在用 Pod 服务账号 token 调 `AssumeRoleWithWebIdentity`，LLD 写明不用角色链，"因为链式会话最长 1 小时，而 Paimon 客户端在凭证剩余不足 1 小时时就会刷新，那样每次文件操作都会请求 `/token`"。IAM 文档："Role chaining limits your … AWS API role session to a maximum of one hour"，`DurationSeconds` 大于一小时时 "the operation fails" [来源: https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_terms-and-concepts.html]。推断：Lambda 执行角色与 Fargate 任务角色的凭证本身就是角色会话，再 AssumeRole 数据角色属于 role chaining；`COLVENNA_DATA_TOKEN_DURATION` 的允许范围 `PT1H30M`–`PT12H` 在这两种运行环境里都会失败。
4. **SnapStart 与启动工作。** 服务启动时跑数据库迁移、建 4 个 HikariCP 连接池；快照在发布版本时生成，Lambda 打运行时补丁时会重跑初始化，恢复后连接状态不保证，恢复与钩子限 10 秒。不用 SnapStart 时 Init 也限 10 秒，JVM 加 Paimon 依赖的冷启动耗时未测。
5. **包大小。** 本地 Maven 仓库中 `paimon-s3-2.0.0.jar` 96.7 MB、`paimon-format-2.0.0.jar` 26.5 MB、`paimon-core-2.0.0.jar` 3.2 MB，三者已约 126 MB，加上 AWS SDK、JDBC 等。zip 方式上限 250 MB（全部依赖的总大小未核实）；容器镜像 10 GB 且同样支持 SnapStart。
6. **每个执行环境一个请求。** 数据库连接数大约是并发数乘以每个环境的连接池大小；colvenna-catalog 的 `COLVENNA_MAX_IN_FLIGHT_REQUESTS`（默认 512）与多池设计都是按常驻进程设计的。数据库虽由用户自带，连接上限仍会被它占用。
7. **网关鉴权方式。** Paimon 客户端只会发 `Authorization: Bearer <token>`（`bear`）或 DLF 签名（2.4 节），不会做 SigV4，所以 API Gateway 的 IAM 授权对 Paimon 客户端不可用（推断）；colvenna-catalog 的 token 是不透明字符串，不是 JWT，HTTP API 的 JWT authorizer 与 ALB `jwt-validation` 也不适用；可用的是 REST 或 HTTP API 的 Lambda authorizer（REQUEST 类型读 `Authorization` 头，可缓存）[来源: https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-use-lambda-authorizer.html]，colvenna-catalog 本身仍会再认证一次。
8. **"空闲不花钱"在各方案里的真实含义。** Lambda + API Gateway 是唯一空闲时计算与入口都不计费的组合，但 VPC 内访问 STS 要 NAT 或接口端点，二者都是常驻费用。Fargate 能设最小 0，但 ALB 常驻且没有请求缓冲。App Runner 不对新客户开放，且空闲照收内存费。EKS 上 Pod 缩到 1 是现成做法（HPA）；缩到 0 要 KEDA HTTP Add-on（beta），而且节点与控制面仍计费。

### 7.4 元数据库备注（不计费用，只记兼容性）

- **Aurora Serverless v2**：PostgreSQL 13.15/14.12/15.7/16.3 及以上可设最小 0 ACU（自动暂停，空闲 300–86,400 秒后暂停），恢复 "might be approximately 15 seconds"，暂停超过 24 小时后 "30 seconds or longer"；有用户连接打开或挂了 RDS Proxy 时不会暂停 [来源: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.how-it-works.html ; https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html]。推断：常驻的 colvenna-catalog 连接池会阻止暂停；恢复时间加冷启动可能超过网关超时。
- **Aurora DSQL**（PostgreSQL 16 兼容）：对照 colvenna-catalog 用到的特性（`colvenna-catalog/src/main/resources/db/migration/*.sql`、`CatalogStore.java`、`Db.java`、`Migrations.java`）：

| colvenna-catalog 用到的特性 | DSQL | 来源 |
| --- | --- | --- |
| `pg_advisory_xact_lock` / `_shared`（迁移串行化、路径占用） | 文档未提及；DSQL 自述 "lock-free"（未核实） | <https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-concurrency-control.html> |
| `SELECT … FOR UPDATE` | 支持，但是乐观的：提交时裁决冲突，不阻塞 | 同上 |
| `FOR NO KEY UPDATE`、`FOR SHARE` | 不支持："Aurora DSQL doesn't support the NO KEY UPDATE or FOR SHARE clauses." | 同上 |
| `FOR UPDATE SKIP LOCKED` | 支持的锁子句只列 `FOR { UPDATE \| KEY SHARE }`（未核实） | <https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-postgresql-compatibility-supported-sql-features.html> |
| 外键、`ON DELETE CASCADE` | 支持（`CASCADE` 等动作），级联修改计入事务行数上限 | <https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-foreign-key-constraints.html> |
| `bigserial` | 文档未出现；`CREATE SEQUENCE` 必须显式写 `CACHE`（1 或 ≥65536），只支持 `bigint`（推断：隐式建序列的 `bigserial` 不可用，未核实） | <https://docs.aws.amazon.com/aurora-dsql/latest/userguide/create-sequence-syntax-support.html> |
| `jsonb` 列 | 支持，压缩后 1 MiB，不能建索引 | <https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-postgresql-compatibility-supported-data-types.html> |
| JDBC 启动参数 `options=-c lock_timeout=…` | `lock_timeout` 等由 DSQL 自己执行，不支持设置 | <https://docs.aws.amazon.com/aurora-dsql/latest/userguide/accessing.html> |
| `READ COMMITTED` | 不支持："The transaction isolation level is fixed at PostgreSQL `Repeatable Read`." | <https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-postgresql-compatibility-unsupported-features.html> |
| 迁移在一个事务里执行多条 DDL 与 DML | 不支持："DDL and DML operations require separate transactions"、"A transaction can include only 1 DDL statement" | 同上 |
| `CREATE [UNIQUE] INDEX` | 要用 `CREATE INDEX ASYNC` | 同上 |
| 事务规模 | 最多修改 3,000 行；连接 1 小时超时 | 同上 |

  推断：DSQL 不能直接替代 PostgreSQL 承载现有 colvenna-catalog 模式与加锁协议。

## 8. 边界与未核实

### 边界

- 本文只比较 catalog 层，不比较查询引擎、表格式本身的能力，也不做实现设计。
- "未找到"指：官方仓库在所列标签的源码树、官方文档、GitHub issue/PR 搜索中都没有；GitHub 代码搜索的索引不完整（melin 的仓库在代码搜索里就没出现），不能证明不存在。
- 托管产品的估算只用了第 1.4 节的假设；真实账单取决于请求的读写比例、对象数（Glue 的表版本与统计信息也算对象）、小文件数（S3 Tables 监控费）与维护任务的计算量。
- 部分结论来自同一天的源码阅读，没有端到端运行：各引擎连 Paimon 2.0 服务端、Kasanari 与 melin 的契约覆盖度、Lambda 上的冷启动与 SnapStart 行为都没有实测。

### 未核实

- Paimon REST 契约在请求与响应 schema 层面的跨版本兼容（只比了路径），以及 Doris 1.4.2、StarRocks 1.3.1、paimon-trino 1.3.1 客户端连 2.0.0 服务端的实际表现。
- Gravitino 的 `token-provider=bearer` 是否映射为 Paimon 的 `bear`；Gravitino rest 后端连 DLF 以外服务端的情况。
- melin/lakehouse-rest-catalog 的 `/token` 是否只下发静态密钥、行过滤与列脱敏是否实现；Kasanari 的 Docker 镜像与端点实现的完整度。
- Lakekeeper 维护者在 #1244 中 "It should work" 的含义；Lakekeeper generic table 设 `format=paimon` 能否实际使用。
- Nessie 是否仍支持以 AWS Lambda 运行（文档残留一句，源码无模块）。
- DLF：元数据 API 是否明确免费（计费项里没有，但无原文声明）；DLF 是否在同一 catalog 中同时管理 Paimon 与 Iceberg 表；DLF 是否为 Paimon REST 下发 OSS 临时凭证（Paimon 与 DLF Java 示例页都没写，只有 Doris 文档提到）。
- Glue Iceberg REST 调用如何计费；S3 Tables Iceberg REST 调用如何映射到计费请求；Snowflake Open Catalog 免费期何时结束。
- Cloudflare Basin Catalog 与 R2 Data Catalog 的关系（定价页未写）。
- DSQL 对 advisory lock、`SKIP LOCKED`、`RETURNING`、`bigserial`、`::jsonb` 转换与 JDBC `READ_COMMITTED` 设置的实际行为。
- Lambda 与 Fargate 上调用 AssumeRole 属于 role chaining 是按 IAM 定义推出的，没有实测。
- colvenna-catalog 全部运行时依赖的总大小、JVM 冷启动时间、实际 Paimon 请求体大小分布。
