---
title: "Colvenna Catalog Press Release & FAQ"
type: working-backwards
status: draft
date: 2026-10-24
---

# Press Release

> 以下为 working-backwards 写法的预期发布稿，描述 1.0 发布当天的产品。文中数字均为产品目标，每处目标都以「目标」标注；已有实测的，在旁注明出处。竞品与引擎的现状以 [竞品调研](../research/paimon-catalog-landscape.md)（取数 2026-10-23）为准，Colvenna Catalog 自己的费用以 [费用说明](../manual/cost.md)（单价取数 2026-10-29）为准。

## Colvenna Catalog 1.0 发布：装在你自己 AWS 账号里的开源 Apache Paimon REST catalog

**Apache 2.0 许可，逐个引擎验证过兼容性，无服务器形态空闲时没有常驻的计算资源。在 AWS 上用 Paimon 的团队不必再自研 catalog，也不必把元数据交给另一朵云。**

**[发布日] —** 今天，Colvenna Catalog 项目发布 1.0 版本。Colvenna Catalog 是一个开源的 Apache Paimon REST catalog 服务端，以 Apache License 2.0 发布，元数据存在你自己的 PostgreSQL 里，表数据留在你自己的 S3 里。在 AWS 上使用 Paimon 的团队，用一条命令就能把它装进自己的账号：已有 Amazon EKS 的，装成两个副本的服务；没有 EKS、或想把 catalog 自身的固定成本压到最低的，以无服务器方式运行，没有请求时不占用任何计算资源。Flink 与 Spark 用 Paimon 官方连接器直接连上，就能建表、读写和提交；Doris 与 StarRocks 验证过的版本和接入方式，写在公开的兼容矩阵里。

### 问题

Paimon 定义了 REST catalog 的契约和客户端，却没有可部署的服务端：Paimon 仓库里的服务端只是测试用的 mock，REST catalog 的设计文档 PIP-28 写明开发者需要自己实现，或者选择托管服务。官方文档唯一点名的服务端是阿里云 DLF，闭源，只在阿里云上提供。在 AWS 上，Glue 不是 Paimon 支持的 metastore 类型，S3 Tables 只存 Iceberg 表，EMR 7.14 与 Athena 的文档里都没有 Paimon；开源的 Polaris、Gravitino、Unity Catalog、Lakekeeper、Nessie 都不提供 Paimon REST 服务。于是 AWS 上的 Paimon 团队只剩几条路，每条都有代价：

- **filesystem catalog** 最简单，但 S3 上的快照提交不是原子的，一张表只要有两个写入方（比如写入作业加上维护作业），就有静默覆盖快照的风险；它也没有认证、授权和审计。
- **Hive Metastore 或 JDBC catalog** 能靠共享锁保护多写者提交，但要多运维一套服务，仍然没有按表的权限和临时凭证，作业只能拿到整个仓库的 S3 权限。
- **另一朵云上的托管服务**，多一个云账号、一份跨云网络和一套身份体系，元数据每次往返都要出云。
- **自研 REST 服务端**，要实现整份 OpenAPI 契约、在多副本下保证提交原子性、做认证授权与凭证下发，再拿 Flink、Spark、Doris、StarRocks 的每个版本逐一联调。兼容性测试和长期跟进 Paimon 版本，是自研里最贵、也最容易被低估的部分。

### 解决方案

- **完整的 Paimon REST 契约。** Colvenna Catalog 实现 Paimon 2.0 REST catalog 的 OpenAPI 1.0 契约，用 Paimon 官方的 `RESTCatalog` 客户端即可连接。兼容性用上游自己的契约测试来证明：Paimon 发布的 `RESTCatalogTest` 与 `CatalogTestBase` 直接打真实的 Colvenna Catalog 和 PostgreSQL，而不是上游的 mock 服务端；每个跳过的用例都写明原因（导入时实测：107 个用例中 96 个通过、11 个跳过、0 个失败，见 [colvenna-catalog LLD](../design/colvenna-catalog-lld.md#契约与版本)）。
- **服务端原子提交。** 每次提交在 PostgreSQL 中加锁登记，快照文件以 S3 条件写入（`If-None-Match`）发布，多个副本、多个写入方同时提交也不会互相覆盖快照。
- **安全的默认值。** 所有 API 都要求 TLS 与 bearer token，没有匿名路由。服务身份按 catalog、库、表授权读、写、管理，可以显式拒绝，读权限可以限定到列。开启按表凭证后，服务端用 AWS STS 为每张表签发短期凭证，会话策略只覆盖这张表的目录，作业不再需要整个仓库的 S3 权限。所有写操作、凭证签发、认证与授权拒绝都写入审计，每条关联请求 id。
- **你不必再自己测的兼容矩阵。** 一张公开的"引擎 × 版本"兼容矩阵覆盖 Flink、Spark、Doris 与 StarRocks，每一格写明引擎版本、它自带的 Paimon 客户端版本和接入方式，并对应一条测试，你可以在自己的账号里一键重跑，确认它在你用的版本上成立。
- **两种部署形态，都在你的账号里。**
  - **Amazon EKS：** Helm chart 部署两个跨节点的副本；配套的 AWS CDK 应用既能接入已有的 VPC、EKS 集群与 S3 桶，也能新建最小的一套。
  - **无服务器：** 以带认证的 API 入口按请求运行，没有请求时不占用计算资源。
- **没有新的厂商依赖。** 元数据在你的 Amazon RDS 或 Aurora PostgreSQL 中，数据在你的 S3 中，不需要注册任何外部服务。代码以 Apache License 2.0 发布。

### 目标（1.0）

| 指标 | 目标 |
| --- | --- |
| 安装 | 在已有 EKS 集群与 PostgreSQL 的账号里，一条命令、15 分钟内装好并可读写（目标） |
| 契约兼容 | 上游 Paimon 契约测试 0 失败，每个跳过项都写明原因（目标；导入时实测 96 通过、11 跳过、0 失败，见上） |
| 引擎兼容 | 兼容矩阵中 Flink、Spark、Doris、StarRocks 的每一格都有测试并通过，用户可在自己账号里重跑（目标） |
| 空闲成本 | 无服务器形态：没有请求时计算费用为 0；EKS 形态：固定 2 个副本，每个申请 0.5 vCPU 与 1 GiB 内存。数据库与网络是用户的基础设施，不计入（目标；按[费用说明](../manual/cost.md)的估算，两种形态自身的固定资源空闲时每月 $0.43，安装脚本默认新建的数据库与网络另计） |
| 正确性 | 多副本、多写入方并发提交零快照丢失，由集成测试持续验证（目标） |
| 安全 | 没有匿名可达的 API；发布版的 ASH、Trivy、cdk-nag 扫描没有未处理的发现（目标） |

### 引言

"Paimon 用户问得最多的不是'catalog 该有哪些功能'，而是'我自己搭一个，要测多久才敢上生产'。Colvenna Catalog 把这些测试做成了公开、可重跑的兼容矩阵，团队第一天就能确认它在自己用的引擎版本上成立。" —— Colvenna Catalog 项目负责人

"我们评估过自研，光是让 Flink 写、Doris 和 StarRocks 读同一张表的兼容性测试就排了一个季度，也不想把元数据放到另一朵云上。现在 catalog 就跑在我们自己的账号里，没有流量的时候不花计算的钱。" —— 早期用户，某电商数据平台负责人（预期引言）

### 开始使用

访问 <https://github.com/colvenna/colvenna-catalog> 获取安装说明、兼容矩阵与费用说明。已有 EKS 的，用 Helm chart 部署；没有的，用 CDK 应用以无服务器方式部署。用 `bootstrap-admin` 签发第一个管理员 token，再把 Flink 或 Spark 的 Paimon catalog 指向 Colvenna Catalog 的地址（`metastore=rest`、`token.provider=bear`）。

---

<!-- markdownlint-disable-next-line MD025 -- PR/FAQ 由 Press Release 与 FAQ 两个一级部分组成 -->
# FAQ

## 面向用户

**Q：Colvenna Catalog 和 Paimon 自带的 catalog 有什么区别？**
Paimon 自带 filesystem、Hive、JDBC 三种 catalog，以及 REST catalog 的客户端与契约。前三种把元数据放在文件系统、Hive Metastore 或关系库里，由各个客户端自己提交；REST catalog 则把提交、权限和凭证交给服务端，但 Paimon 不提供这个服务端。Colvenna Catalog 就是这个服务端：你仍然直接使用 Paimon 表和 Paimon 官方客户端，只是把 catalog 的地址换成它。

**Q：它和 Apache Polaris、Apache Gravitino、Unity Catalog 是什么关系？**
它们以 Iceberg 为中心，Colvenna Catalog 以 Paimon 为中心。Colvenna Catalog 之于 Paimon，就像 Polaris 之于 Iceberg：一个实现该格式官方 REST 契约、可以独立部署的开源服务端。这几个项目对 Paimon 的支持停在别的层次：Polaris 只能把 Paimon 表作为通用表登记，不协调提交，也不下发凭证；Gravitino 的 Paimon 支持是连接 Paimon 自身 catalog 的客户端，社区提出的 Paimon REST 服务提案还停在讨论阶段；Unity Catalog、Lakekeeper、Nessie 不支持 Paimon。

**Q：要花多少钱？**
软件本身免费，以 Apache License 2.0 发布，没有服务费。运行费用是它在你账号里用到的 AWS 资源，按 `us-west-2` 的按需价，每月约为（单价、公式与测试安装的账单核对见[费用说明](../manual/cost.md)）：

- **两种形态共有：** 一个 Secrets Manager 密钥和一个镜像，$0.43，空闲时照常计费。
- **EKS 形态：** 两个常驻副本，每个申请 0.5 vCPU 与 1 GiB 内存。它们跑在你集群的节点上，不单独计费；按 `t3.large` 的价格折算，约值 $30，是否要为它们添节点取决于你的节点有多满。处理请求不另外计费。
- **无服务器形态：** 按请求计费，包括 API Gateway 的请求、Lambda 的调用与时长、日志和指标。没有请求时计算与请求费用为 0；每月 100 万次请求共约 $11–$24，1000 万次共约 $64–$200（都已含上面的 $0.43），范围取决于请求耗时，按测试中实测的中位数 47 ms 到均值 453 ms 折算。
- **可选项：** EKS 形态的内部 NLB $16.43，无服务器形态的 SnapStart $7.91，开启后空闲时也计费，默认都关闭。

元数据库与网络是你的基础设施，不计入上面的数字：可以用你已有的 PostgreSQL 与 VPC，EKS 形态总是用你的集群与 VPC。默认由安装脚本新建时，数据库每月 $26.06，无服务器形态的网络每月 $43.91，几乎都是三个接口端点的费用。

**Q：支持哪些引擎和版本？**
兼容矩阵写明每个引擎验证过的版本、它自带的 Paimon 客户端版本和接入方式：

- **Flink、Spark：** 用 Paimon 官方连接器，以 `metastore=rest` 原生连接，支持服务端下发的表凭证。Paimon 2.1.0 的连接器支持 Flink 1.16–1.20 与 2.0–2.2、Spark 3.2–3.5 与 4.0、4.1。
- **Doris：** 自带 REST catalog 类型（`paimon.catalog.type=rest`），读数据时使用服务端下发的表凭证；但已发布的 Doris 4.1.5 自带的是 Paimon 1.4.2 客户端，读 Paimon 2.0 的 deletion vector 表需要配套的 Paimon 2.0 读取器。
- **StarRocks：** 已发布的 4.1.6 自带 Paimon 1.3.1，文档只列 filesystem 与 Hive 两种 catalog 类型。1.0 以参数透传（`paimon.catalog.type=rest`）连接 Colvenna Catalog；StarRocks 读数据时不使用服务端下发的表凭证，所以要给它配一份只读的 S3 凭证，兼容矩阵在这一格写明。

矩阵之外的引擎，只要使用 Paimon 2.0 的 `RESTCatalog` 客户端，按契约应能连接，但不在 1.0 的验证范围内。

**Q：必须用 AWS 吗？**
1.0 面向 AWS：按表凭证由 AWS STS 签发，部署物是 EKS 的 Helm chart 与 AWS CDK 应用。服务端的提交路径支持 S3 兼容存储与本地文件系统，其他存储的提交会被拒绝。

**Q：元数据库有什么要求？**
PostgreSQL 16（Amazon RDS 或 Aurora PostgreSQL 均可）。数据库模式随服务启动自动迁移，多副本同时启动时靠 PostgreSQL 的 advisory lock 串行；遇到比自身更新的模式版本时拒绝启动，避免回退后的旧版本写坏新模式。

**Q：认证怎么做？**
Colvenna Catalog 自己签发不透明的 bearer token，只存哈希，可以设置过期和随时撤销；Paimon 客户端以 `token.provider=bear` 携带。第一个管理员 token 由 `bootstrap-admin` 命令签发并写入只有属主可读的文件，之后的服务身份、token 与授权都通过管理 API 管理。

**Q：支持 Iceberg 吗？**
1.0 不支持。下一阶段，Colvenna Catalog 会提供只读的 Iceberg REST 接口：Paimon 表以 Iceberg 兼容元数据发布后，Spark、Trino 等 Iceberg 读端可以直接从 Colvenna Catalog 读取，认证、授权和凭证下发与 Paimon 共用同一套。到那时，同时需要 Paimon 和 Iceberg 的团队装一个 Colvenna Catalog 就够了，不必再另装 Polaris。

**Q：能和 Colvenna Lake 一起用吗？**
可以。Colvenna Lake 是基于 Paimon、Flink 与 Doris 的开源实时湖仓平台，Colvenna Catalog 原本就是它的 catalog，独立后 Colvenna Lake 也以发布版的 Colvenna Catalog 作为自己的 catalog。

## 面向内部

**Q：差异化到底是什么？**
真正的空位是"AWS 上没有能用的 Paimon REST catalog"：没有托管的，开源里也没有成熟的（能找到的只有几个早期个人项目，有的没有许可证、有的不下发表凭证）。无服务器部署是第二个真差异，调研的开源 catalog 都没有文档化的无服务器部署。Apache 2.0 与 EKS 部署不是差异：开源的 Iceberg catalog 也都是 Apache 2.0，也都有镜像与 Helm chart，只是它们不提供 Paimon REST。

**Q：Colvenna Catalog 比托管的 catalog 便宜吗？**
只比服务费不成立。按调研的统一假设（1 万张表、每月 1000 万次请求），托管 catalog 本身每月 $0–$81，Glue 约 $9；而按[费用说明](../manual/cost.md)的估算（`us-west-2` 按需价），Colvenna Catalog 在 EKS 上常驻两个副本，按 CPU 申请量折算约每月 $30，另有 $0.43 的密钥与镜像；无服务器形态在这个请求量下共约每月 $64–$200，按测试中实测的请求耗时中位数 47 ms 到均值 453 ms 折算。这两个数都不含元数据库与网络。所以对外不说"比托管便宜"，说的是：没有服务费、无服务器形态空闲时没有计算费用，以及 AWS 上本来就没有可比的托管 Paimon catalog，替代方案是自研，省下的是自研和自测兼容性的人力。

**Q：为什么要从 Colvenna Lake 拆成独立仓库？**
因为目标用户不同。Colvenna Lake 面向想要整套实时湖仓的团队；Colvenna Catalog 面向任何在 AWS 上用 Paimon、只缺一个 catalog 的团队，他们不会为了一个 catalog 去装整个平台。独立仓库有自己的版本号、发布节奏、部署物和兼容矩阵，Colvenna Lake 按镜像 digest 引用它的发布版。

**Q：为什么新仓库不带 git 历史？**
Colvenna Lake 的历史里有部署环境的标识，后来才替换成占位符；提交信息里的 issue 编号指向 Colvenna Lake，搬到新仓库会链接到同号的错误 issue。设计上的理由已经写进 LLD，新仓库的第一个提交注明来源的 Colvenna Lake 提交，需要追溯时回 Colvenna Lake 查。

**Q：为什么 1.0 不做多 catalog、角色授权和多云？**
目标用户选择自研还是选成熟方案，卡在两件事上：自己测兼容性太贵，以及运行费用。1.0 先把这两件事做到可验证：公开可重跑的兼容矩阵，和空闲时没有常驻计算的部署形态。Polaris 式的多 catalog 与角色授权、AWS 以外的云，等这两点立住之后再做。

**Q：最大的风险是什么？**
三个。

1. **引擎侧跟不上 Paimon 2.0。** 只有 Paimon 自己的 Flink 与 Spark 连接器原生支持 2.0 契约。Doris 与 StarRocks 已发布的版本分别自带 Paimon 1.4.2 与 1.3.1，两家的主干虽已升级到 2.x，但都没有发布；StarRocks 读数据时不使用服务端下发的表凭证，与"引擎不持有对象存储凭证"的模型冲突，1.0 只能让它持有一份只读的 S3 凭证。兼容矩阵里这两格的范围取决于上游的发布。
2. **无服务器形态的硬约束。** Lambda 同步请求上限 6 MB、API Gateway 10 MB，而服务端接受 64 MiB 的请求体；API Gateway 的集成超时是 29–30 秒，与服务端 30 秒的锁等待相当；更关键的是按表凭证：Lambda 与 Fargate 的执行角色本身就是角色会话，再扮演数据角色属于角色链，会话最长 1 小时，而 Paimon 客户端在凭证剩余不足 1 小时时就会刷新，所以无服务器形态需要不经角色链的凭证签发方式。冷启动也必须在 Lambda 初始化的 10 秒限制内完成。
3. **契约漂移。** Paimon 的 REST 契约仍在演进（2.1.0 已于 2026-10-12 发布），Colvenna Catalog 要跟进每个 Paimon 发布版，并用上游契约测试守住兼容。

**Q：什么情况下这个项目不应该存在？**
出现下面任何一种情况，Colvenna Catalog 的差异化都会大幅缩小：Paimon 官方发布可部署的 REST 服务端（apache/paimon#5966）；Gravitino 的 Paimon REST 服务提案（apache/gravitino#7663）落地；Polaris 为 Paimon 表补上提交协调与凭证下发；AWS 推出支持 Paimon 的托管 catalog，或 DLF 在 AWS 上提供服务。项目应每半年对照这几项重新评估。
