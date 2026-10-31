---
title: "colvenna-catalog LLD"
date: 2026-10-19
status: implemented
imported_from: "cove-lake@6ded0b7bf6a9b4b62b7a33a2266adf9f4adb5d76"
aligned_with: "colvenna-lake@33eb4b54c23aefb17e6af3577cc1adeaf8042b24"
---

# colvenna-catalog LLD

> 本文随服务端从 Colvenna Lake 导入：导入的提交是 frontmatter 的 `imported_from`（Colvenna Lake 当时的名字），改名时与 `aligned_with` 对齐。导入的文本引用过 Colvenna Lake 的高层设计、里程碑验收记录、产品文档与 issue 编号；这些引用后来都改成了在本文中直接写出的设计理由与边界，读本文不需要 Colvenna Lake 的任何文档。

colvenna-catalog 是 Colvenna Catalog 的服务端：它实现 Apache Paimon 2.0.0 的 REST catalog 契约，元数据后端是 PostgreSQL，源码在本仓库的 [`src/`](../../src)。产品的范围、两种部署形态与 1.0 的接收条件见 [v1.0 设计](v1.0-design.md)；本文记录服务端的实现设计。

服务端存在的首要理由是提交的原子性。Paimon 2.0.0 不经 catalog 发布快照时，用 `RenamingSnapshotCommit` 先写临时文件、再改名成 `snapshot-N`，它的注释写明本地文件系统与 HDFS 上改名是原子的，对象存储上则需要额外的锁保护（[源码](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/catalog/RenamingSnapshotCommit.java)）；S3 的 FileIO 沿用 `FileIO.tryToWriteAtomic` 的默认实现，即"写临时文件再改名"（[源码](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-common/src/main/java/org/apache/paimon/fs/FileIO.java)）。所以在 S3 上，同一张表只要有两个写入方（例如写入作业加维护作业），没有锁的 filesystem catalog 就可能让后一个提交覆盖前一个快照。REST catalog 把快照提交交给服务端（`POST .../commit`），colvenna-catalog 在服务端完成原子发布，见下文“提交协议”。

## 契约与版本

| 项 | 取值 |
| --- | --- |
| REST 契约 | Paimon 2.0.0 发布版的 [REST catalog OpenAPI 1.0](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/docs/static/rest-catalog-open-api.yaml)，路径前缀 `/v1/{prefix}`，`prefix` 等于服务端的 warehouse 名 |
| 客户端 | Paimon 的 `RESTCatalog`（`metastore=rest`，`token.provider=bear`），引擎经它接入。哪些引擎与版本实测可用，以[兼容矩阵](../compat/matrix.md)为准（规则见 [v1.0 设计](v1.0-design.md)第 7 节） |
| 实现 | Java 21，固定依赖 Paimon 2.0.0 的 `paimon-core`、`paimon-format`、`paimon-s3` |
| 管理面 | colvenna-catalog 自有的 `/colvenna/admin/v1`，与 Paimon 契约分开版本化；不实现 Paimon 的 REST Management API（2.0.0 发布版没有它，2.1.0 起作为实验性接口发布，见[调研](../research/paimon-catalog-landscape.md)第 2.3 节） |

契约兼容性用上游测试验证：Paimon 2.0.0 发布的 `paimon-core` 测试 jar 中有 `RESTCatalogTest` 与 `CatalogTestBase`，`ColvennaCatalogContractTest` 让它们直接打到真实的 colvenna-catalog 和 PostgreSQL，而不是上游的 mock 服务端。2026-10-28 在本仓库 main 上的结果是 107 个用例中 96 个通过、11 个跳过、0 个失败。跳过的用例都在 `UpstreamExclusions` 中写明了原因：

- 3 个断言的是 mock 服务端写死的值：owner 字面量 `owner`、createdBy 字面量 `created`，以及 rollback-schema 后仍返回旧 schema id 的过期元数据。
- 1 个要求服务端在删表时删除客户端指定的外部数据路径；colvenna-catalog 刻意不用自己的凭证删除客户端自选的位置（见下文"客户端选择的位置"）。
- 7 个是行过滤与列脱敏用例。服务端不提供这两项：Paimon 2.0.0 发布版没有定义它们的管理接口，定义它们的 Management API 是 2.1.0 起的实验性接口，服务端不实现（见上表与下文“授权”的“执行方与边界”）。

该套件在 CI 的 `verify` 作业中运行（`.github/workflows/ci.yml`），契约兼容结果可以对任意提交重复验证。

## 端点范围

除下列例外，OpenAPI 1.0 的全部路径都已实现：

| 端点 | 行为 | 原因 |
| --- | --- | --- |
| `POST .../branches/{branch}/rename` | 501 | Paimon 2.0.0 的 `RESTCatalog.renameBranch` 在客户端直接抛 `UnsupportedOperationException`，不会发出请求 |
| `POST .../tables/{table}/auth` | 按授权判定所选列，允许时返回空的行过滤与列脱敏 | 只有表选项 `query-auth.enabled=true` 时客户端才调用；服务端不提供行过滤与列脱敏，见下文“授权” |
| `GET .../tables/{table}/token` | `COLVENNA_DATA_TOKEN_MODE=disabled`（默认）时 501 | 见下文“按表数据凭证”；客户端开启 `data-token.enabled` 而服务端未启用时直接失败，不会退回宽凭证 |
| 分区端点 | 只对 `metastore.partitioned-table=true` 的表生效，其他表返回 501 | 与上游一致：501 时客户端从文件系统列分区 |

表类型：`table` 与 `materialized-table` 是 Paimon 文件表，服务端写 schema 与 snapshot 文件；`format-table`、`object-table`、`iceberg-table`、`lance-table` 只在 PostgreSQL 中登记元数据。后四种类型属于 Paimon 2.0.0 发布契约，上游契约测试会用到；服务端只登记它们的元数据，不为它们写任何文件。

**名称**：库、表、视图、函数、分支、tag 与 consumer 的名称都会成为文件路径的一部分，因此不得是 `.` 或 `..`，也不得含 `/`、`\` 或控制字符；`?`、`#`、`%` 会被 URI 当成查询、片段或转义而截断或改写路径，`*` 与 `?` 在限定表凭证的 IAM 会话策略里是通配符，这四个字符同样禁止。服务端对每个 URL 解码后的路径段（`%2F` 会解码成 `/`）和请求体里的每个名称都做这项检查。库名还不得含 `.`，因为全局列表的分页 token 是 `库.名`（与 Paimon 客户端和上游测试一致），只有库名不含点时它才能无歧义地切分。

## 部署形态

服务无状态，可多副本运行，所有状态都在 PostgreSQL 与仓库目录中。

| 端口 | 用途 | 认证 |
| --- | --- | --- |
| 8181 | Paimon REST API 与管理面，HTTPS；无服务器形态中为只绑定 127.0.0.1 的明文（见“无服务器形态”） | 全部要求 bearer token，没有匿名路由；先认证再读请求体、再解析路由，未认证请求得到 401，不会暴露路由是否存在。认证之前只做与路由无关的检查：并发超限、模式版本不符（只在启动时不迁移的服务上检查，见“连接池”）回 503，路径段不是合法名称（见上文"名称"）回 400 |
| 8182 | `/healthz`、`/readyz` | 无认证，只返回状态码，不含任何目录数据；只监听 127.0.0.1，Pod 之外任何地方都连不上，Kubernetes 探针在容器内用 curl 访问 |
| 9100 | `/metrics`（Prometheus 抓取） | 无认证，不含目录数据；`COLVENNA_METRICS_PORT` 默认 `-1`（不开），chart 固定设为 9100，NetworkPolicy 只放行带 `colvenna-catalog/monitoring=true` 标签（chart 的 `metricsClientLabel`）的命名空间里的 Pod |

**停止**（#64）：服务收到 SIGTERM 后按这个顺序停止。一，`/readyz` 立即回 503，此后每个响应都带 `Connection: close`，客户端的下一个请求因此换一条新连接，由 Service 发往没有在停的副本。二，API 端口在 `COLVENNA_SHUTDOWN_DRAIN`（默认 5 秒）里照常接受新连接、照常应答，接住规则还没更新、仍被发到这里的客户端。三，端口不再接受连接，进行中的请求最多再等 `COLVENNA_SHUTDOWN_TIMEOUT`（默认 20 秒，向上取整到秒）完成，然后关闭剩余连接。两项默认合计 25 秒，在 Kubernetes 默认的 30 秒宽限期内。Lambda 执行环境中两项默认都是 0（设计决定）：那里没有 Service 要摘除，执行环境何时关停由 Lambda 安排，排空没有对象。chart 另外在容器上加 `preStop` 等待（默认 15 秒）：Kubernetes 删除 Pod 时，kubelet 先执行 `preStop`、再发 SIGTERM，与此同时控制面把这个 Pod 从 Service 的 EndpointSlice 里标为 terminating、`ready: false`（[Pod 终止流程](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination-flow)）；各节点的规则随后才更新，这段等待让服务在规则更新之前照常应答。Pod 的 `terminationGracePeriodSeconds` 是三项之和再加 10 秒。Paimon 2.0.0 的客户端不重试 `ConnectException`、`SSLException` 等连接异常，非幂等请求（提交）也不在 I/O 异常后重试，只对 429、503 重试（[`ExponentialHttpRequestRetryStrategy`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-api/src/main/java/org/apache/paimon/rest/ExponentialHttpRequestRetryStrategy.java)），等一个响应最多 3 分钟，写死在 [`HttpClientUtils`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-api/src/main/java/org/apache/paimon/rest/HttpClientUtils.java) 里、选项改不了；所以副本被替换时不能让客户端连到已关闭的端口或已消失的 Pod（#64 在旧版本上见过一次请求挂了约 2 分 20 秒才失败）。

配置全部来自 `COLVENNA_*` 环境变量，见 `ServerConfig` 与下文“配置项”：PostgreSQL 连接、warehouse 名与根路径、指标端口（`COLVENNA_METRICS_PORT`）、服务端 FileIO 选项（`COLVENNA_FILEIO_*`），以及强制下发给客户端的配置覆盖（`COLVENNA_CLIENT_OVERRIDES`）。

**S3A 区域**（[#9](https://github.com/colvenna/colvenna-catalog/issues/9)）：Paimon 的 S3 FileIO 把 `s3.region` 交给 S3A 时变成 `fs.s3a.region`，而 S3A 3.4 读的是 `fs.s3a.endpoint.region`；它没有区域也没有 endpoint 时发往 us-east-2，靠跨区域访问（`fs.s3a.cross.region.access.enabled`，默认开启）跟随重定向到桶所在的区域（Paimon 2.0.0 自带的 Hadoop 3.4.2，[`DefaultS3ClientFactory`](https://github.com/apache/hadoop/blob/84e8b89ee2ebe6923691205b9e171badde7a495c/hadoop-tools/hadoop-aws/src/main/java/org/apache/hadoop/fs/s3a/DefaultS3ClientFactory.java)）。有出口时这能工作，只是多一跳；没有出口的 VPC 中，S3 网关端点只在它所在的区域可用（[AWS 文档](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html)），请求就挂起。因此两种形态的部署都把 `COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION`（即 `fs.s3a.endpoint.region`）设为安装的区域：chart 的 ConfigMap 取 `region`（`install.sh` 设为 `COLVENNA_REGION`），无服务器栈取栈的区域；不设 endpoint，S3A 按区域连 `<bucket>.s3.<region>.amazonaws.com`。跨区域访问仍按默认开启，但桶必须在安装的区域（预检核对），请求不会被重定向。仓库在 S3 上而没有设 S3A 的区域或 endpoint 时，`serve` 在两种形态中都拒绝启动（EKS 形态自 [#9](https://github.com/colvenna/colvenna-catalog/issues/9) 起）。`S3aRegionTest` 让服务端的 S3A 经一个只记录隧道目标、随即拒绝的代理连接，证明设了区域时它只连本区域的端点，只设 `s3.region` 时连 us-east-2。发布 snapshot 的 S3 客户端（AWS SDK）读 `s3.region`，不受影响。实测（2026-10-30，us-west-2，EKS 形态，测试环境 ux2）：一条 NetworkPolicy 只让 catalog 的 Pod 连 VPC 内（数据库、DNS、为测试加的 `sts` 接口端点）与本区域 S3 网关端点前缀列表的地址，如同没有出口的 VPC；修复前的版本建表 200 秒没有回答，修复后建库、建表、读表、删表、删库都回答了，最慢 3.3 秒。

**连接**：API、健康两个端口（以及配置了的指标端口）都用 JDK 自带的 `com.sun.net.httpserver`，请求在虚拟线程上处理。它只在系统属性 `sun.net.httpserver.nodelay` 为 `true` 时给接受的连接设 `TCP_NODELAY`，默认 `false`（[`jdk.httpserver` 模块说明](https://docs.oracle.com/en/java/javase/21/docs/api/jdk.httpserver/module-summary.html)），并且只在第一个服务器建立时读一次（OpenJDK `jdk21u-dev` 的提交 `eac07229cf6c`，即本机 JDK 21 更新版本的 GA 标签，其中 `sun/net/httpserver/ServerConfig.java` 的静态初始化，`ServerImpl.java` 对每个接受的连接调用 `setTcpNoDelay`；HTTPS 共用这段代码）。`RestServer` 在建立服务器之前设置它（启动参数已设时不改）。不设时 Nagle 算法让响应体等客户端对响应头的 ACK，Linux 客户端会把这个 ACK 延迟：#37 测得 keep-alive 连接上的每个响应多约 40 ms。`NoDelayTest` 在独立的 JVM 里检查服务器接受的连接在每次应答时都设了 `TCP_NODELAY`：一个客户端在 keep-alive 连接上发的 45 次请求与另外 3 个客户端各自新连接上的请求，每次应答都是，对照组（不加载 `RestServer`）都不是。它不比较响应时间：响应时间取决于主机的延迟 ACK 与负载，这样的比较在主机繁忙时偶发失败（#66）。

**TLS**：API 端口由服务端自己终止 TLS，证书来自 `COLVENNA_TLS_KEYSTORE`（PKCS12）。未配置证书时服务拒绝启动，只有本地开发与测试显式设置 `COLVENNA_ALLOW_PLAINTEXT=true` 才走明文（监听所有接口）。这样 bearer token 只在 TLS 里传输。无服务器形态另有只在 Lambda 中生效的回环明文 `COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT`，见“无服务器形态”。Paimon 客户端用 JVM 默认信任库校验证书和主机名，所以引擎的信任库必须是 JDK 自带 `cacerts` 加上 colvenna-catalog 的 CA：只放 CA 会让同一 JVM 访问 S3、STS 时失去公网 CA。服务证书的 SAN 要覆盖 Service 的各个 DNS 名。EKS 形态的私有 CA 带 RFC 5280 对签发证书的 CA 要求的扩展：critical 的 `basicConstraints`、`keyUsage`（`keyCertSign`、`cRLSign`）和 `subjectKeyIdentifier`；服务证书按设计带 `keyUsage`（`digitalSignature`）、`extendedKeyUsage`（`serverAuth`）、SAN 和按密钥指向 CA 的 `authorityKeyIdentifier`。严格校验 X.509 的客户端（Python 3.13 起默认如此）缺了这些就拒绝连接（#58）；#58 之前的安装重跑 `install.sh` 时，CA 证书用原来的密钥和名称重新签发，持有旧证书的客户端照样信任新的服务证书。

**资源保护**：并发请求数受 `COLVENNA_MAX_IN_FLIGHT_REQUESTS`（默认 512）限制，超出时直接返回 503，此时请求还没有任何副作用，客户端可以安全重发。请求体上限 64 MiB。响应体默认不设上限；入口装不下大响应时（Lambda 最多 6 MB）用 `COLVENNA_MAX_RESPONSE_BYTES` 设上限，见“无服务器形态”。未认证的拒绝每秒最多写 20 条审计记录，超出部分只写访问日志，防止匿名请求刷满审计表。

**连接池**：四个连接池互不争用。请求在主池上执行；提交意图与表隔离标记要在请求仍持有主池连接和行锁时独立提交，走自治写入池；审计事件和 bearer token 查询各有一个池（后三个的大小都是 `COLVENNA_JDBC_AUX_POOL_SIZE`）。即使大量提交占满主池，它们等待的写入也不会被饿死，伪造 token 的洪泛也只会占用认证池。所有连接都以启动参数设置 `lock_timeout`（主池为 `COLVENNA_JDBC_LOCK_TIMEOUT`，默认 30 秒；其余 5 秒），连接第一个事务回滚也不会撤销它，因此 `COLVENNA_JDBC_URL` 不得自带 `options` 参数：持锁的提交在 Java 中等待自治写入，这种等待 PostgreSQL 看不见，超时让可能的环路变成报错而不是挂起。锁等待超时（SQLSTATE `55P03`）返回 503，说明本次请求的目录改动已回滚、可以重试，见“提交协议”中 503 的说明。连接池在第一次取连接时才建立，构造服务不连数据库；池大小至少为 1（Hikari 会把小于 1 的值悄悄换成 10）。被拒绝的 token 摘要会缓存 30 秒，重复的坏 token 不再查库；同时查库的未缓存 token 最多 64 个，超出时立即返回可重试的 503（等待会占住请求并发名额）。数据库模式随服务启动按版本迁移（`COLVENNA_MIGRATE_ON_START`，默认 `true`），多副本靠 PostgreSQL advisory lock 串行；遇到比自身更新的模式版本时拒绝启动，防止回退后的旧服务端写坏新模式。设为 `false` 时（Lambda 执行环境中的默认值）`serve` 启动时不碰数据库，由安装脚本先执行 `migrate`；服务在第一个请求时不取锁地读一次已记录的版本，与自身版本不同（更旧或更新）时，在认证之前就以 503 拒绝每个请求，直到版本一致。检查通过一次后不再重复，与启动时迁移的服务只在启动时检查相同。因此“旧服务端不会服务新模式”只对服务开始服务时看到的模式成立（Lambda 中是执行环境的第一个请求）：服务运行期间有人执行了更新版本的 `migrate`，它会继续服务新模式，直到被替换，与 EKS 滚动升级期间仍在运行的旧副本一样。升级时安装脚本因此先 `migrate`，再更新函数，让新版本的执行环境替换旧的。

## 配置项

"Lambda 形态"一列是无服务器形态要设的值（由部署栈设置）；空着的与默认值相同或按部署而定。所有 `COLVENNA_*` 的默认值都是 EKS 形态的取值。

| 变量 | 默认 | 含义 | Lambda 形态 |
| --- | --- | --- | --- |
| `COLVENNA_PORT` | `8181` | API 端口 | 适配器转发的端口（`AWS_LWA_PORT`） |
| `COLVENNA_HEALTH_PORT` | `8182` | `/healthz`、`/readyz`，只监听 127.0.0.1 | 适配器的就绪检查用 `/healthz`，它不连数据库 |
| `COLVENNA_METRICS_PORT` | `-1`（不开） | `/metrics` 端口 | 不开 |
| `COLVENNA_TLS_KEYSTORE`、`COLVENNA_TLS_KEYSTORE_PASSWORD` | 无 | API 端口的 PKCS12 证书库 | 不设（与回环明文互斥） |
| `COLVENNA_ALLOW_PLAINTEXT` | `false` | 在所有接口上走明文，只供本地开发与测试；只接受 `true` / `false` | 不设（与回环明文互斥） |
| `COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT` | `false` | API 端口以明文只绑定 127.0.0.1；只在 Lambda 执行环境中允许 | `true` |
| `COLVENNA_MIGRATE_ON_START` | `true`；Lambda 执行环境中为 `false` | `serve` 启动时是否迁移模式；`false` 时第一个请求检查版本 | `false`（默认），安装脚本执行 `migrate` |
| `COLVENNA_JDBC_URL`、`COLVENNA_JDBC_USER`、`COLVENNA_JDBC_PASSWORD` | 无 | PostgreSQL；URL 不得带 `options` | 没有 `COLVENNA_JDBC_PASSWORD`，URL 带 `sslmode=verify-full` |
| `COLVENNA_JDBC_AUTH` | `password` | `password`：以 `COLVENNA_JDBC_PASSWORD` 登录；`iam`：RDS IAM 数据库认证，每个新连接用服务端自己的 AWS 凭证在本地签一个 token 作密码（不调用 AWS），此时 `COLVENNA_JDBC_PASSWORD` 被拒绝，URL 须只有一个主机并带 `sslmode=verify-full` | `iam` |
| `COLVENNA_JDBC_IAM_REGION` | `AWS_REGION` | `iam` 时 token 签名所用的区域 | |
| `COLVENNA_JDBC_POOL_SIZE` | `16` | 主池大小，至少 1 | `2` |
| `COLVENNA_JDBC_AUX_POOL_SIZE` | `4` | 自治写入、审计、认证三个池各自的大小，至少 1 | `1` |
| `COLVENNA_JDBC_LOCK_TIMEOUT` | `PT30S`；Lambda 执行环境中为 `PT10S` | 主池的 `lock_timeout`，至少 1 毫秒；超时返回 503 | `PT3S`：与取连接的 10 秒、经 S3A 的一次 S3 请求（至多 15 秒）合计 28 秒，见“无服务器形态” |
| `COLVENNA_S3_CALL_TIMEOUT` | `PT30S`；Lambda 执行环境中为 `PT8S` | 发布 snapshot 的 S3 客户端一次调用（含重试）的超时，必须小于 5 分钟 | 默认；同上 |
| `COLVENNA_S3_CALL_ATTEMPT_TIMEOUT` | `PT10S`；Lambda 执行环境中为 `PT3S` | 该客户端每次尝试的超时，不大于上一项 | 默认；同上 |
| `COLVENNA_MAX_RESPONSE_BYTES` | `0`（不限） | 响应体上限，按 JSON 转义后的字节数计 | `4194304` |
| `COLVENNA_DROP_PURGE` | `inline`；Lambda 执行环境中为 `deferred` | 删表后谁删文件：`inline` 由删表请求自己删，`deferred` 请求不碰文件，由每个副本每 5 分钟的清扫删（Lambda 中由清理函数删，见“无服务器形态”与“元数据模型”的“表路径”）；Lambda 中不接受 `inline` | `deferred` |
| `COLVENNA_REQUEST_WORK_TIME` | `PT0S`（不限）；Lambda 执行环境中为 `PT8S` | 工作量随数据增长的请求一次最多做多久：一致性核对到时不再开始新的步骤，带 `nextPageToken` 回答（见“提交协议”的“一致性核对”）；删库到时回可重试的 503，已删的表不恢复，重发的请求接着删；Lambda 中须为正，且与取连接的 10 秒合计短于 29 秒 | `PT8S`：与取连接、最后一张表与删库本身的锁等待合计 27 秒 |
| `COLVENNA_SHUTDOWN_DRAIN` | `PT5S`；Lambda 执行环境中为 `PT0S` | 停止时就绪检查失败、响应关闭连接之后，端口继续接受新连接的时间（见“部署形态”的“停止”） | |
| `COLVENNA_SHUTDOWN_TIMEOUT` | `PT20S`；Lambda 执行环境中为 `PT0S` | 端口不再接受连接之后，进行中的请求最多再等多久，向上取整到秒 | |
| `COLVENNA_WAREHOUSE_NAME`、`COLVENNA_WAREHOUSE_ROOT` | 无 | warehouse 名（REST 路径前缀）与根路径 | |
| `COLVENNA_EXTERNAL_LOCATION_ROOTS` | 空 | 允许客户端选择位置的外部根，逗号分隔 | |
| `COLVENNA_FILEIO_*`、`COLVENNA_FILEIO_OPTIONS` | 无 | 服务端 FileIO 选项；仓库在 S3 上时必须有 S3A 的区域 `COLVENNA_FILEIO_FS_S3A_ENDPOINT_REGION`（或 endpoint），两种形态的部署都设为安装的区域，见“部署形态”的“S3A 区域” | S3A 超时见下文 |
| `COLVENNA_CLIENT_OVERRIDES` | 空 | 强制下发给客户端的配置，`k=v` 逗号分隔 | |
| `COLVENNA_DEFAULT_PAGE_SIZE`、`COLVENNA_MAX_PAGE_SIZE` | `100`、`1000` | 列举的默认与最大页长 | |
| `COLVENNA_MAX_IN_FLIGHT_REQUESTS` | `512` | 并发请求上限 | |
| `COLVENNA_AUTH_CACHE_TTL` | `PT2S` | 认证与授权在副本内的缓存时间；只接受 ISO-8601 时长 | |
| `COLVENNA_DATA_TOKEN_MODE` | `disabled` | 按表数据凭证：`disabled` 或 `aws-sts` | `aws-sts` |
| `COLVENNA_DATA_TOKEN_ROLE_ARN` | 无 | 数据角色 | |
| `COLVENNA_DATA_TOKEN_REGION` | `AWS_REGION` | STS 区域与返回给客户端的 S3 区域 | |
| `COLVENNA_DATA_TOKEN_DURATION` | `PT3H` | 会话时长，`PT1H30M`–`PT12H` | |
| `COLVENNA_DATA_TOKEN_WEB_IDENTITY` | `token-file` | `token-file`（IRSA）或 `cognito` | `cognito` |
| `COLVENNA_DATA_TOKEN_COGNITO_IDENTITY_POOL_ID` | 无 | 身份池 id，`<region>:<id>` | 部署栈建的身份池 |
| `COLVENNA_DATA_TOKEN_COGNITO_DEVELOPER_PROVIDER` | 无 | 身份池的开发者提供方名 | 同上 |
| `COLVENNA_ENVIRONMENT` | `dev` | 授权生效的环境 | |

开关类配置只接受 `true` 或 `false`（不分大小写），时长只接受 ISO-8601（如 `PT10S`）；其他值让服务拒绝启动，并说明是哪个配置。`COLVENNA_ALLOW_PLAINTEXT` 与 `COLVENNA_AUTH_CACHE_TTL` 也是如此：`COLVENNA_ALLOW_PLAINTEXT=yes` 之类的值如果被悄悄当成 `false`，写错的配置就不会被发现。

## 无服务器形态

[v1.0 设计](v1.0-design.md) 第 6.2 节的无服务器形态把同一个镜像跑在 Lambda 里：私有 API Gateway REST API（Lambda authorizer）在前，AWS Lambda Web Adapter 把调用转成执行环境内的本机 HTTP 请求。无服务器形态的 [spike](../research/serverless-spike.md) 实测了约束，服务端为此有以下行为，全部由上表的配置打开，默认值不改变 EKS 形态：

- **启动不碰数据库**：Lambda 初始化（或 SnapStart 恢复）限 10 秒，spike 中数据库连接耗尽时初始化会超时。`COLVENNA_MIGRATE_ON_START=false` 时不迁移，连接池不预先建立，`/healthz` 不连数据库；第一个请求才连接，先检查模式版本（见上文“连接池”）。SnapStart 快照里因此也没有连接。
- **等待短于网关超时**：REST API 的集成超时是 29 秒。在 Lambda 执行环境中，主池锁等待 `COLVENNA_JDBC_LOCK_TIMEOUT` 与发布 snapshot 的 S3 调用超时 `COLVENNA_S3_CALL_TIMEOUT` / `COLVENNA_S3_CALL_ATTEMPT_TIMEOUT` 默认为 `PT10S`、`PT8S`、`PT3S`。其中任何一项达到 29 秒，或者取连接的 10 秒（固定）加锁等待再加 S3 调用超时达到 29 秒（默认值合计 28 秒），服务就拒绝启动，并说明是哪几项配置。EKS 形态的默认值不变，也不做这项检查。服务端 FileIO（S3A）的超时用已有的 `COLVENNA_FILEIO_FS_S3A_*` 设置。S3A 3.4 会把低于 15 秒的连接、读取与请求超时提高到 15 秒（spike 的 `5s` 因此并未生效），并把请求超时（`fs.s3a.connection.request.timeout`）同时用作每次尝试与整个 SDK 调用（含 SDK 重试）的上限；S3A 自己的重试套在外面，分错误（`fs.s3a.retry.limit`）与限流（`fs.s3a.retry.throttle.limit`，默认 20 次）两种。部署栈因此把三个超时设为 15 秒、SDK 重试 1 次、S3A 的两种重试都设为 0：经 S3A 的一次 S3 请求至多 15 秒，主池锁等待设为 `PT3S`，与取连接的 10 秒合计 28 秒，低于 29 秒（测试检查这个和）。锁超时返回可重试的 503，重发的提交只生效一次（见“提交协议”）。函数超时应长于网关超时，让网关已回 504 的提交仍能在服务端完成，客户端重发时作为重放成功（spike 实测）。这项检查只管单次调用的上限；删表要删的文件数、一致性核对要读的文件数随表变大，没有单次调用的上限，所以这两个请求另有安排（下两条，#69）；回滚、删 tag 与删分支也是（再下一条，#93）。
- **删表不等文件**（#69）：`COLVENNA_DROP_PURGE=deferred`。删表在一个事务里把表从目录中删掉，并在 `table_purges` 里记下托管表的根目录，提交后就回答，不碰任何文件；客户端此后看到的就是表已不在，删除对客户端是原子的。文件由清理函数删（见下文“部署”）。以前删表请求自己删文件：UAT-FLT-02 里一张 1,200 次提交的表删了 29,028 ms，网关在 29 秒回 504，而服务端随后删完（一张约 600 次提交的表 27,587 ms）；删文件超过函数的 60 秒超时时还可能只删一部分。删表要锁住表的全部分支指针，这一条语句的等待合计不超过一个 `lock_timeout`（3 秒）：PostgreSQL 对每次取锁分别计时（[文档](https://www.postgresql.org/docs/16/runtime-config-client.html#GUC-LOCK-TIMEOUT)），所以服务端在这条语句上把 `statement_timeout` 设为当时的 `lock_timeout`，超时与其他锁超时一样回可重试的 503，什么都没删。删库按表逐个删，每张表一个事务；`COLVENNA_REQUEST_WORK_TIME`（8 秒）用完时回 503，已删的表留在已删状态，重发的请求接着删剩下的表，最后删库本身。DELETE 是幂等的，Paimon 客户端的 HTTP 层对 503 自动重发 5 次（见“提交协议”），重发是安全的。
- **一致性核对分页**（#69）：`COLVENNA_REQUEST_WORK_TIME=PT8S`。核对分步进行：各分支的指针和 head 是一步，之后每个抽样快照或 tag 的数据文件各是一步。一次回答做了 8 秒以后不再开始新的一步，带着到此为止的报告与 `nextPageToken` 回答；客户端带上它再问，核对从停下的地方接着做。最后一页没有 token，就是完整的报告，只有它的 `consistent` 可能为真（详见“提交协议”的“一致性核对”）。以前一张约 600 个快照的表在网关 29 秒时 504（UAT-FLT-02）。核对同时变快：每个数据目录在一次回答里只列一次，不再对每个文件发 HEAD；manifest 以 16 路并发读，并在一次回答里缓存（64 MiB）；目录也以 16 路并发列。
- **回滚、删 tag、删分支不等数据文件**（#93）：这三种请求在请求里只做让变更生效的那一步：先把它们去掉的快照、tag 与 long-lived changelog 文件（下称根）连同内容登记进 `file_purges` 与 `file_purge_roots`，再去掉它们，变更生效即回答；根引用的数据文件、manifest、manifest list、索引与统计文件由清理函数之后删除（`deferred`；EKS 形态 `inline` 时删 tag 由请求在回答前自己删，与删表相同，见下文“EKS 形态”）。以前它们在请求里删，都随表的历史变多：回滚对目标之后的每个快照 id 依次查存在与删除（[`RollbackHelper.cleanLargerThan`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/table/RollbackHelper.java)），删 tag 读 tag 与两侧邻居的全部 manifest、再删只属于它的文件（[`TagManager.deleteTag`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/utils/TagManager.java)）。实测（见下文“实测”的 #93 部分）以前的服务端回滚掉 450 个快照在网关 29 秒时 504，删一个独占 800 个数据文件的 tag 也 504。
  - **回滚**：与 `RollbackHelper.cleanLargerThan` 做同样的事：先把 `LATEST` 提示改成目标，删掉目标之后的快照、long-lived changelog（及其提示）与 tag 文件；回滚到快照已过期的 tag 时把 tag 写回成快照并改 `EARLIEST` 提示（同一文件里的 `createSnapshotFileIfNeeded`）。目录各列一次，不再对每个快照 id 查一次。快照文件必须在请求里删：下一次提交要以条件写占用这些槽位（见“提交协议”），流式读端也按文件发现快照。Paimon 自己的回滚不删数据文件与 manifest，它们成了没有根引用的孤儿，要等孤儿文件清理（默认只删一天以前的文件，[`OrphanFilesClean`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/operation/OrphanFilesClean.java)）；现在由清理删掉，但要等一小时，见下文“重新提交的宽限”。
  - **删 tag**：请求里只删 tag 文件，并通知表的 tag 回调，与 `TagManager.deleteTag` 相同。tag 的快照仍在（同一个快照）时什么都不登记：它引用的文件都还被这个快照引用，与 Paimon 相同。Paimon 自己删 tag 时只看这个 tag 所在分支上左右两个邻居（同一文件的 `doClean`），从这个 tag 建的分支（分支目录里有 tag 快照的副本）会因此丢文件；清理读所有分支的根，不会。
  - **删分支**：请求里删分支目录，与 Paimon 相同（[`FileSystemBranchManager.dropBranch`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/utils/FileSystemBranchManager.java)）；目录的存在就是分支的存在，同名分支随即可以再建。分支最新的 schema 一并登记，清理按它读分支的 manifest。Paimon 删分支时只删分支目录，分支写的数据文件与 manifest 在表根下，与 main 共用目录，它们留成孤儿；现在由清理删掉其中别的分支不再引用的，同样等一小时。
  - **S3 上的读与删**：要读、要删的文件多时，S3 上经服务端的 S3 客户端（即发布 snapshot 的条件写所用的那个，同一套 endpoint、区域、凭证与 `COLVENNA_S3_CALL_TIMEOUT` / `COLVENNA_S3_CALL_ATTEMPT_TIMEOUT` 调用超时，Lambda 中 8 秒 / 3 秒）：每个文件一次 `GetObject`，32 路并发（`TableFiles.PARALLELISM`），每 1,000 个文件一次 `DeleteObjects`（API 的上限）；路径都先确认在表根之下，与 `JailedFileIO` 相同。其他存储经服务端 FileIO，32 路并发，每个文件一次。最初经 S3A 的版本在 fx5 上回滚掉 450 个快照用了服务端的 9.0 秒，超过 8 秒的工作时间，于是改成这样（见“实测”）。
  - **先登记，后删除**：根在自治事务里先提交，请求随后才删文件；`file_purges.applied` 在变更自己的事务里置真，记录在变更生效时才开始计时。登记之后、提交之前失败的请求留下未生效的记录，它在一个租期（20 分钟）加上操作的宽限（见下文）之后到期：这样的请求可能已经删了一部分快照文件；所以记录只是“可能删哪些”的清单，删不删由清理当时核对（下两条）。这样的请求可能已经删了部分快照文件，而 catalog 的指针仍是原来的快照：清理也把指针算作根，见下条。
  - **只删没有根引用的**：根所达的文件（manifest list、其中的 manifest、manifest 里 `ADD` 的数据文件及其 extra 文件、索引 manifest 及其索引文件、统计文件）是候选。清理接着读这张表**当时**每个分支的每个快照、tag 与 long-lived changelog 文件，以及 PostgreSQL 里每个分支指针的快照（`table_branches.snapshot_json`，`loadSnapshot` 与批读按它回答，即使它的文件已不在），删掉候选中没有被引用的（`Reach.referenced`）。“引用”按 Paimon 读快照的规则：快照的 base 与 delta manifest 合并后仍在的数据文件（`ADD` 减去其后同一文件的 `DELETE`，与 [`FileEntry.mergeEntries`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/manifest/FileEntry.java) 相同；层级变化是同名的一对 `DELETE` 与 `ADD`，仍算在），delta 与 changelog manifest `ADD` 的文件（增量与 changelog 读取读它们），以及它的全部元数据。只被 `DELETE` 条目提到的文件没有读者，Paimon 自己删 tag 时也删它们（`doClean` 以邻居合并后的文件为界）。按文件名判断：Paimon 的数据文件名带每个路径工厂的随机 UUID 与计数（[`DataFilePathFactory`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/io/DataFilePathFactory.java)），是唯一的，别处有同名文件也算被引用。元数据缺失时视为什么都不引用，与 Paimon 的孤儿文件清理相同（`OrphanFilesClean.retryReadingFiles` 把文件不存在当作空）：缺了 manifest list 的快照本来就读不了；其他读取失败让这次清理失败，什么都不删。客户端选择的位置（表根之外）与 Paimon 管理的 blob 包（`.managed.blob`，孤儿文件清理也不删）从不删。
  - **删之前再核对，持锁删**：算好候选与引用之后，每批 500 个文件删除前先锁表行与全部分支指针（与删表相同的顺序，等待合计一个 `lock_timeout`），重新列出所有分支的根、重读指针，读新出现的、剔除它们引用的，再删这一批（S3 上一次 `DeleteObjects`），提交即放锁。经 catalog 的提交、建 tag、建分支与回滚因此在一批删除期间等着；直接写进文件的 tag（Flink、Spark 的 procedure）在下一批之前被看到。锁等待超时就稍后重试这一批，3 次后这次清理失败、留待下次。删的顺序是数据、索引与统计文件，然后 manifest，最后 manifest list：中途停下的清理从同样的根再做一遍，剩下的文件仍然可达。
  - **重新提交的宽限**：回滚与删分支留下的文件在变更生效一小时后才可清理（`FilePurges.RECOMMIT_GRACE`），删 tag 留下的不等。原因：从回滚以前的 checkpoint 恢复的写入方会把回滚掉的快照里它提交过的文件再提交一次：Paimon 的提交方对没有快照记着的 checkpoint 先核对文件还在、再提交（[`TableCommitImpl.filterAndCommitMultiple`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/table/sink/TableCommitImpl.java)）。宽限内重新提交，清理时这些文件已被新快照引用，留着；宽限之后恢复，核对失败、作业报错，而不是提交一个缺文件的快照。只剩一种窗口：恢复的提交方核对文件之后、提交之前，恰好清理删了这一批，这与 Paimon 的孤儿文件清理同样存在，宽限让它只可能发生在回滚一小时以后的恢复上。删 tag 留下的是更早快照的文件，没有写入方会再提交它们，Paimon 自己也当场删。回滚或删分支生效时还把这张表别的记录推迟到同一时刻（`due_at` 与 `notBefore` 取较晚的）：没生效的、别的操作的记录也可能达到这次留下的文件。`GET /colvenna/admin/v1/purges` 的 `dueAt` 显示何时到期；积压告警的“最旧”从 `due_at` 算起，宽限不算积压。
  - **请求的时间**：要登记的根随回滚去掉的、被删分支上的快照数变多。登记受 `COLVENNA_REQUEST_WORK_TIME`（Lambda 中 8 秒）约束：每次请求至少读一批（32 个），时间用完时已读的留在记录里，回答可重试的 503，什么都没删；重发的同一请求（同一张表、分支、操作与目标的、没生效、半个租期即 10 分钟内建的记录）接着读剩下的。Paimon 客户端对 503 自动重发 5 次（约 31 秒）。登记与删除期间请求持有表行锁与分支指针锁，与以前在请求里删文件时相同：同一分支的提交等待，至多一个 `lock_timeout` 后得到可重试的 503。
  - **重发**：删 tag 与删分支是 DELETE，生效之后重发得到 404，与以前相同。回滚以前在生效之后重发会再回滚一次，抹掉期间别的写入方刚提交、已被告知成功的快照（#80 列出的取舍）。现在每个分支记下最后一次回滚：谁、目标、`fromSnapshot`、回滚到的快照、时间（`table_branches.last_rollback`）。同一调用方在 5 分钟内（`CommitService.ROLLBACK_RETRY_WINDOW`，长于客户端约 31 秒的重发加每次至多 29 秒）发来同一个回滚（同一分支、目标与 `fromSnapshot`），算作它的重发。指针仍停在那次回滚留下的快照时：带 `fromSnapshot` 的回答 200，就是它生效时的回答（Paimon 写入方解决压缩冲突时就带着它发，[`CommitRollback`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/operation/commit/CommitRollback.java)：回滚到最新减一、条件是最新）；不带的照常执行，结果相同。此后已有提交的，两种都回答 409 并说明，不再回滚：别的写入方的一次提交就可能让指针回到条件里的快照 id，按条件回答会把它的提交再回滚掉。代价：同一调用方（例如共用一个 principal 的两个作业）在 5 分钟内有意再做一次同样的回滚会得到 409：不带 `fromSnapshot` 的带上最新快照作 `fromSnapshot`，否则等 5 分钟即可；写入方的压缩冲突回滚因此失败时，它的提交按冲突失败、重试。
  - **EKS 形态**：`inline`（默认）时删 tag 由请求提交后自己清理，清完再回答：结果与以前相同（回答时文件已删），而从这个 tag 建的分支不再丢文件；请求读这张表所有分支的根，比 Paimon 只读邻居慢，没有网关的时间限制。回滚与删分支只做 Paimon 也做的那一步就回答，留下的文件在宽限后由副本每 5 分钟的清扫删掉，这是 Paimon 不做的。清理失败时记录留着，清扫重试。
- **时间预算**：各请求依次可能等待的上限，都短于网关的 29 秒：

  | 请求 | 依次等待 | 合计 |
  | --- | --- | --- |
  | 提交 | 取连接 10 s + 锁等待 3 s + 一次 S3 调用（经 S3A 至多 15 s；发布快照的条件写至多 8 s） | 28 s |
  | 删表 | 取连接 10 s + 表行锁 3 s + 全部分支指针的锁合计 3 s + 删除与记录的几条 SQL；不碰文件 | 16 s 多一点 |
  | 删库 | 取连接 10 s；逐表删，8 s 用完就回 503（至少删一张表）；最后开始的那张表（表行锁 3 s + 分支指针的锁合计 3 s）；删库本身（库行锁 3 s） | 10 + 8 + 6 + 3 = 27 s，加几条 SQL（部署栈的测试核对这个和） |
  | 一致性核对的一页 | 取连接 10 s（第一步）；8 s 后不再开始新的一步；正在做的一步：读一个快照的 manifest（16 路并发）并列它的数据目录（16 路并发），每个 S3 请求至多 15 s；先为它读至多三个之前已核对的快照的 manifest（见“一致性核对”） | 18 s 加上一步 |
  | 回滚 | 取连接 10 s + 表行锁 3 s + 分支指针锁 3 s；列目录，读全部 tag；登记：8 s 后不再开始新的一批（32 个 `GetObject`，每个至多 8 s）就回 503；改提示，删去掉的快照、changelog 与 tag 文件（每 1,000 个一次 `DeleteObjects`，至多 8 s）；几条 SQL | 16 + 8 s 加一批读与删除 |
  | 删 tag | 取连接 10 s + 表行锁 3 s；读 tag 与它的快照，登记一个根，删一个文件 | 13 s 多一点 |
  | 删分支 | 取连接 10 s + 表行锁 3 s + 分支指针锁 3 s；列目录；登记同回滚；删分支目录（经 S3A：列出后成批删除） | 16 + 8 s 加删目录 |

  回滚与删分支的最后一项随去掉的快照数变长（每 1,000 个一次删除），没有固定上限；实测见下文“实测”的 #93 部分。

  核对的一步没有固定上限：它读一个快照的全部 manifest，时长随这个快照的 manifest 数而定。一个快照的 manifest 多到一步就超过约 19 秒的表，在无服务器形态里核对不了（网关 504），只能在 EKS 形态里核对。实测见下文“实测”的 #69 部分。

- **响应体上限**：Lambda 的响应最多 6 MB，超出时函数失败，客户端只收到网关的错误（#80 之后是 503，之前是 502）。`COLVENNA_MAX_RESPONSE_BYTES` 按响应体在 Lambda 响应里的样子计数，即 JSON 字符串转义后的 UTF-8 字节数（`"` 与 `\` 各多 1 字节）。按可见项组页的列举（库、表、视图、函数及其详情）在会超限的那一项之前结束本页，并给出下一页 token，Paimon 客户端按 token 读完全部；单独一项就超限时返回 500 并说明是哪一项，而不是一个会让客户端提前结束列举的空页。其他超限的响应返回 500 并说明上限，记为失败的审计。建议值 4 MiB：即使适配器把响应体按 base64 传，也仍在 6 MB 之内。
- **按表凭证**：`COLVENNA_DATA_TOKEN_WEB_IDENTITY=cognito`，见“按表数据凭证”。
- **连接预算**：执行环境一次只处理一个请求，主池 2、其余三个池各 1，即每个执行环境最多 5 个连接；清理函数（预留并发 1）另有至多 5 个，每 15 分钟一次、每次几秒。预留并发限制的是并发调用，不是持有连接的执行环境：冻结与退役的环境仍占着连接，所以数据库上要设 `idle_session_timeout` 或用 RDS Proxy，并按 `max_connections` 选预留并发。
- **并发之外的请求可重试**（#80）：服务端函数的预留并发 C（`COLVENNA_LAMBDA_CONCURRENCY`，默认 10）之外的调用被 Lambda 拒绝（`TooManyRequestsException`，429，[Invoke API](https://docs.aws.amazon.com/lambda/latest/api/API_Invoke.html)）。API Gateway 把被拒绝的调用当作内部错误回 500（[Lambda 文档](https://docs.aws.amazon.com/lambda/latest/dg/services-apigateway-errors.html)：“If the Lambda API rejects the invocation request, API Gateway returns a 500 error code”），Paimon 2.0.0 的客户端只对 429 与 503 重发（[`ExponentialHttpRequestRetryStrategy`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-api/src/main/java/org/apache/paimon/rest/ExponentialHttpRequestRetryStrategy.java)），所以以前并发一高客户端就失败（UAT-FLT-02）。
  - **实测是哪种网关响应**（2026-10-29，fx3，访问日志的 `$context.error.responseType`）：被限流的调用是 `INTEGRATION_FAILURE`，状态 500；执行日志写的是 `Execution failed due to configuration error: Rate Exceeded.`，但响应类型不是 `API_CONFIGURATION_ERROR`。一个运行了却失败的函数（把一个版本的超时设成 1 秒，`Task timed out after 1.00 seconds`）也是 `INTEGRATION_FAILURE`，状态 502。网关响应的状态码是按类型固定的（[网关响应类型](https://docs.aws.amazon.com/apigateway/latest/developerguide/supported-gateway-response-types.html)），两者分不开。
  - **`INTEGRATION_FAILURE` 回 503，一个取舍**：被限流的请求没有运行，重发安全，这是要解决的常见情形。运行了却失败的函数也因此回 503，Paimon 客户端会把它重发（任何方法，5 次）。多数写重发时只生效一次：提交是重放，建库建表、改表、改名、登记、tag 与分支的建删、分区、授权与停用身份等回 409 或 404，读与凭证签发照样回答（见“提交协议”的“元数据变更的重试”）。但有几个写在已经生效之后再发一次会再做一次：分支快进与 `replaceTable` 会抹掉期间别的写入方刚提交、已被告知成功的快照，schema 回滚同理；重置 consumer 会把已经前进的 consumer 拉回；签发 token 会多签一个（不返回给任何人，仍然有效直到过期，管理员可列出并撤销）。回滚以前也在其中（不带起点快照的回滚会再回滚一次），#93 之后同一调用方 5 分钟内的同一个回滚算作重发，不再回滚（见上文“回滚、删 tag、删分支不等数据文件”）。这种情形要求函数在写已经提交之后、回答之前失败：内存耗尽（实测最多用到 2,048 MB 中的约 350 MB）或 Lambda 的基础设施故障；超时不会，函数的 60 秒超时长于网关的 29 秒，网关先回 504。权衡：过载在 UAT-FLT-02 里每分钟上百次，以前每次都让客户端失败；函数在写提交之后失败极少，且只涉及上述几种写。网关响应的状态码按类型固定，网关与服务端都分不出这两种情形。`INTEGRATION_TIMEOUT`（504）不改：网关放弃时服务端可能还在做这个请求；`DEFAULT_5XX` 不改：改它会把所有没单独设定的 5XX 都变成同一个状态码。超过 6 MB 的响应也会让函数失败，服务端以 `COLVENNA_MAX_RESPONSE_BYTES` 先挡住了。
  - **stage 限流，一个上限**：API Gateway 用令牌桶限流，桶容量是突发上限，令牌按速率补充，超出时网关自己回 429（`THROTTLED`，[文档](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-request-throttling.html)；限流是“目标”而不是硬上限）。按 Little 定律，并发等于请求速率乘平均时长（[Lambda 文档](https://docs.aws.amazon.com/lambda/latest/dg/lambda-concurrency.html)），C 个执行环境在平均时长 D 下每秒服务 C / D 个请求，这是满负荷的吞吐。所以速率 R = ⌊C / D⌋、桶容量 B = C：网关每秒放进的不超过 C 个环境在 D 下能做的，同一刻开始的不超过 C 个（桶容量限制的是同时放进，不是同时在做的请求数）。D 取 0.125 s（`API_REQUEST_SECONDS`）：UAT-FLT-02 过载时实测平均 112 ms 向上取整。默认 C = 10 时 R = 80 个/秒，B = 10。设定在全部方法与路径上（`*` 与 `/*`）；限流按方法计，根路径的 `ANY` 与 `{proxy+}` 的 `ANY` 各一个桶，目录的请求几乎都走后者，所有客户端共用。
  - **哪个先拒绝**：在 UAT-FLT-02 的负载下（20 个写入方，每秒约 18 个请求，最高的一分钟约 25 个）网关的上限不会先到；拒绝都发生在全部执行环境被替换后的冷启动（每个环境的头一个请求约 2 秒，这时 10 个环境每秒只做得了 5 个左右），由 Lambda 拒绝、网关回 503。网关的 429 管的是持续超过 R 的请求，比如成批的读。两种拒绝都没有副作用，客户端都会重发。实测见下文 #80 部分。
  - **不带 `Retry-After`**：服务端自己的 503 也不带。Paimon 客户端按 1、2、4、8、16 秒退避重发 5 次（约 31 秒），每次另加最多 10% 的随机抖动；遇到 `Retry-After` 时用它代替退避，也不再抖动（同一处源码）。网关响应只能带固定的头，所有被拒的客户端会按同一个间隔一齐重发，持续过载时更容易一齐用完 5 次。
  - **调节**：只有 `COLVENNA_LAMBDA_CONCURRENCY`：R 与 B 随 C 等比（每加 1 个并发，每秒多放 8 个请求）。`COLVENNA_LAMBDA_MEMORY` 影响 D：CPU 随内存分配。授权函数也预留 C，它被限流时网关回 `AUTHORIZER_FAILURE` 或 `AUTHORIZER_CONFIGURATION_ERROR`，早已映射为 503。一个作业重发 5 次仍被拒，说明 C 不够，加大它。
  - **看得见**：访问日志多了 `errorType`（`$context.error.responseType`）与 `error`，网关自己回的 429、503 与服务端的 503 分得开；Lambda 的 `Throttles` 指标计被拒的调用。
- **回环明文**：TLS 在 API Gateway 终止，适配器经回环把请求交给服务端。`COLVENNA_LAMBDA_LOOPBACK_PLAINTEXT=true` 让 API 端口以明文只绑定 127.0.0.1，没有配置能让它绑定别的地址。它与 `COLVENNA_TLS_KEYSTORE`、`COLVENNA_ALLOW_PLAINTEXT` 互斥（同时设置时配置本身就被拒绝），并且只在 Lambda 执行环境中允许（`AWS_LAMBDA_RUNTIME_API` 已设），否则 `serve` 拒绝启动。回环上同样要求 bearer token。
- **S3A 区域**：与 EKS 形态相同，见“部署形态”的“S3A 区域”。spike 在没有出口的 VPC 中只设了 `COLVENNA_FILEIO_S3_REGION`，第一次建表挂到函数超时。

### 部署

`install.sh` 以 `COLVENNA_FORM=serverless` 安装这个形态（`deploy/lib/serverless.sh`），CDK 应用 `deploy/aws` 中有它的四个栈，都受 cdk-nag 与禁用资源守卫（`src/guard.ts`）把关：

| 栈 | 内容 | 卸载时 |
| --- | --- | --- |
| `ColvennaCatalog<Env>Network` | `COLVENNA_NETWORK=create`：新 VPC（`COLVENNA_VPC_CIDR`，两个可用区各一个私有子网；没有 internet gateway、NAT gateway、Elastic IP），S3 网关端点（只放行本账号的桶）与 `sts`、`cognito-identity`、`execute-api` 接口端点（私有 DNS，两个子网都有，安全组只放行 VPC 内的 HTTPS），流日志。`existing`：用户的 VPC 与子网，端点按原样使用，预检逐项核对。两种模式下都有函数的安全组：没有入站，出站只有 HTTPS 与 VPC 内的数据库端口 | 有数据库留在其中时保留，否则删除 |
| `ColvennaCatalog<Env>Storage` | 与 EKS 形态相同：要新建的桶与 RDS PostgreSQL 16，数据库放在上述子网，只放行函数的安全组 | 只有 `--delete-data` 才删 |
| `ColvennaCatalog<Env>Images` | 本环境的镜像仓库 `colvenna-catalog-<env>/colvenna-catalog`：Lambda 只从本账号、本区域的 ECR 取镜像。仓库策略只允许 Lambda 为本环境的函数拉取。单独成栈，因为函数创建之前镜像必须已在仓库里 | 删除（连同镜像） |
| `ColvennaCatalog<Env>Serverless` | 见下 | 删除 |

**服务端函数**运行仓库 `Dockerfile` 的镜像（按 digest；release 安装时就是发布的镜像，只是复制到本环境的仓库）。镜像里因此带有 AWS Lambda Web Adapter 1.1.0（`/opt/extensions`，只有 Lambda 会启动它）、Lambda 沙箱用户 UID 993 的 passwd 条目（Hadoop 登录需要）、按校验和固定的 RDS CA 证书包（Lambda 中没有 ConfigMap 可挂，JDBC 以 `sslmode=verify-full` 校验它）。函数为 x86_64（发布的镜像是 linux/amd64），超时 60 秒（长于网关的 29 秒，见上文“等待短于网关超时”），内存 `COLVENNA_LAMBDA_MEMORY`（默认 2048 MB），预留并发 `COLVENNA_LAMBDA_CONCURRENCY`（默认 10），设置即上文配置项表的“Lambda 形态”一列。API 只调用它的别名 `live`。

**数据库登录没有密码**：服务端与维护函数以 `COLVENNA_JDBC_AUTH=iam` 登录，authorizer 与 ops 函数用 `@aws-sdk/rds-signer`；每个新连接都用函数自己的执行角色在本地签一个 RDS IAM 认证 token（15 分钟内有效，只用于建立连接），经已校验证书与主机名的 TLS 发送。每个执行角色只有以自己那个数据库用户、在这个数据库上 `rds-db:connect` 的权限（数据库的资源 ID：存储栈的输出，或预检时读取已有数据库的；已有数据库须已开启 IAM 数据库认证，否则预检拒绝）。ops 函数建立的两个用户都没有密码（以前留下的也删除），并授予 `rds_iam`。任何函数的环境变量里都没有密码或其他秘密，`GetFunctionConfiguration` 读不到可登录数据库的东西；栈里唯一的秘密是管理员的 token，没有函数读取它；数据库管理员的密码只在数据库步骤的调用载荷里出现一次。存储栈新建的数据库因此开启了 IAM 数据库认证（EKS 形态的密码登录不受影响）。

**改指向恢复出来的数据库**（#59，用户手册 6.2 节）：元数据库从快照或时间点恢复成新实例后，用 `COLVENNA_DB=existing` 加新 endpoint 重跑 `install.sh`，其余配置不变，两种形态相同。`COLVENNA_NETWORK=create` 的安装也可以这样做：数据库须在本安装建的 VPC 里（`Network` 栈的 VPC 与子网），`Network` 栈还不存在时预检拒绝。对已有数据库，两种形态的预检都要求它在本安装的 VPC 里、安全组放行 catalog、不可公网访问、只接受 TLS 连接（参数组的 `rds.force_ssl=1`，与存储栈建的数据库相同；catalog 自己总是 `sslmode=verify-full`），无服务器形态另要求开启 IAM 数据库认证；数据库步骤再把 catalog 的用户（EKS：设回 `Eks` 栈数据库用户 secret 中的密码；无服务器：没有密码，授予 `rds_iam`）校正一遍。预检还拒绝两种会换掉安装底下数据库的组合：存储栈有数据库时 `COLVENNA_DB=existing` 而不给 `COLVENNA_BUCKET`（重新部署存储栈会删掉它，以及恢复的实例所用的子网组、安全组和参数组）；安装正运行在别的数据库上（无服务器栈的输出 `DatabaseEndpoint`，EKS 为 Helm release 的 `database.url`）时 `COLVENNA_DB=create`（会把安装移回存储栈的数据库）。恢复出来的实例不是安装建的，`uninstall.sh --delete-data` 不删它；它还占用存储栈的子网组时，`--delete-data` 在改动任何东西之前拒绝，并指出它（它用存储栈的子网组或安全组，或者在无服务器安装建的 VPC 里，都算）。实测（2026-10-29，us-west-2，无服务器形态，`COLVENNA_NETWORK=create`）：从手动快照恢复 11 分钟，改指向的 `install.sh` 7 分钟（含新镜像）；之后库、表、快照、principal 与授权都与恢复点相同，两张表的一致性核对都通过，恢复点之后建的库与 principal 不在了；三种拒绝都在改动之前以退出码 3 拒绝。EKS 形态的恢复只有离线测试。

**SnapStart** 默认关闭（`COLVENNA_SNAPSTART=off`），这是有意的取舍：按需初始化实测 2.1–2.4 秒，远在 Lambda 初始化的 10 秒上限之内（见下文“实测”），SnapStart 省下的约 1.5 秒不值得空闲时的计费。spike 实测容器镜像可以用 SnapStart，恢复 0.42–0.75 秒；但只有 Java 托管运行时的 SnapStart 不另收费，自建基础镜像的容器函数要为每个已发布版本的快照缓存按 GB·秒计费（至少 3 小时），空闲时也收，与本形态“没有请求时没有计算费用”相冲突（[Lambda SnapStart](https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html) 与 [Lambda 定价](https://aws.amazon.com/lambda/pricing/)，2026-10-28 查阅）。设为 `on` 时，栈在发布的版本上开启 SnapStart，旧版本随新版本替换而删除，不再计费。镜像带有 `com.amazonaws.lambda.feature.snapstart="Allow"` 标签，所以不需要运行时钩子；快照里没有数据库连接（连接池在第一个请求时才建立），API token 用的 `SecureRandom` 每次都读 `/dev/urandom`，恢复后仍然唯一。

**入口**：

- REST API 的端点类型是 `PRIVATE`，`EndpointConfiguration` 关联 `COLVENNA_API_ENDPOINT_IDS` 与新 VPC 的 `execute-api` 端点；资源策略只在 `aws:SourceVpce` 是这些端点时允许 `execute-api:Invoke`，其他一律拒绝。客户端的 catalog URI 是 `https://<api>.execute-api.<region>.amazonaws.com/catalog`（端点开了私有 DNS 时），或 `https://<api>-<vpce>.execute-api.<region>.amazonaws.com/catalog`。
- 所有方法（根路径与 `{proxy+}` 的 `ANY`）都经 REQUEST 类型的 Lambda authorizer（Node.js，`deploy/aws/functions/authorizer.ts`，由 esbuild 在本地打包，不需要 Docker）。它按 `TokenService.authenticate` 的规则校验：`Authorization` 必须以 `Bearer` 加一个空格开头（区分大小写），其余部分按 Java 的 `trim` 去空白后非空，其 SHA-256 须是 `api_tokens` 中未撤销、未过期、principal 未停用的 token；否则 401，服务端不被调用。决定按 `Authorization` 头缓存 60 秒，覆盖整个 stage；服务端对每个请求仍自己认证，撤销因此仍在“认证与审计”所说的时间内生效。authorizer 用只读用户 `<COLVENNA_DB_USER>_authz` 连库，它只能读这些列。连不上数据库时网关回 503（可重试），不是 500。
- 只有 API Gateway 能调用服务端与 authorizer，权限限定为这个 API；其他函数没有任何资源策略。

**按表凭证**：身份池只有开发者提供方 `colvenna-catalog-<env>.catalog`，不开未认证身份、不开经典流程、不挂角色；数据角色只信任 `cognito-identity.amazonaws.com`，`aud` 为该身份池、`amr` 为 `authenticated`，会话最长 12 小时，权限与 EKS 形态的数据角色相同（`src/warehouse.ts`）。整个栈只有服务端的执行角色有 `GetOpenIdTokenForDeveloperIdentity` 权限。账号内拥有通配权限的管理员同样能调用它，这与他们本来就能直接读写仓库桶相同；身份池没有资源策略可以进一步限制。

**安装脚本在 VPC 内做的事**：`install.sh` 不直接连数据库，也不直接访问私有 API，而是以 IAM 认证的 Invoke 调用两个函数（它们没有 API、没有 Function URL、没有任何触发器；冒烟检查另外调用一次清理函数，见上文）：

- 维护函数：同一个镜像，入口换成 `/opt/colvenna-catalog/lambda-maintenance.sh`，一个自己的 Runtime API 循环，执行 `migrate` 与 `bootstrap-admin`（新 token 只出现在调用的返回里，安装脚本直接写进 Secrets Manager 的 `colvenna-catalog-<env>/admin-token`）。镜像里的适配器在这里也会启动，但它只在就绪检查通过后才接调用，而这里没有进程监听它的端口。选 Lambda 而不选一次性任务：数据库在函数所在的子网里本来就可达，不需要再开 ECR 与日志的接口端点，也不新增任何可达的资源。
- ops 函数（`deploy/aws/functions/ops.ts`）：以数据库管理员建立或校正 catalog 用户、数据库与 authorizer 的只读用户（两者的空闲会话 5 分钟后由数据库结束，冻结与退役的执行环境因此不会一直占着连接），`migrate` 之后授予 authorizer 用户只读这些列，以及经 VPC 端点向私有 API 发请求（管理员授权、默认库与冒烟检查）。

能对维护函数调用 `lambda:InvokeFunction` 的身份能为管理员签发 token，能调用 ops 函数的身份能以它转发请求（仍须带 token），能调用清理函数的身份只能让它提前删除已记录的根目录（它不从载荷里取任何东西，除了命令本身）；这与 EKS 形态中能在 catalog Pod 里 exec 的身份相当，账号里应只有管理员有这些权限。密码与 token 都写在只有本用户可读的载荷文件里，不出现在命令行或日志中。顺序：网络、存储、镜像仓库、镜像、无服务器栈、数据库用户、`migrate`、授权、管理员、冒烟检查。已有安装的服务端运行着别的镜像时（升级，或回到上一个版本），`migrate` 之前不部署无服务器栈（#63）：本版本的 CDK 应用会改服务端函数的描述（带源码 SHA）与环境变量，CDK 据此发布服务端的新版本、移动别名 `live`，于是 `migrate` 拒绝之前服务端就已换了。安装脚本改为先合成本版本的栈，再取已部署的模板（`GetTemplate`），只把其中维护函数的属性（镜像、环境变量及其中的数据库，改指向恢复出的实例时也随之换成新的）、它的角色的策略文档（能以 IAM 登录的数据库）与输出 `MaintenanceImage` 换成合成结果里的（`deploy/lib/serverless_template.py`；新属性引用了已部署模板里没有的资源时，在改动之前失败），以栈原有的参数交给 CloudFormation 更新（`UpdateStack`，不给角色与标签时沿用栈原有的，见 [UpdateStack](https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_UpdateStack.html)，2026-10-31 查阅；失败即回滚）；服务端函数、它发布的版本与别名、authorizer、ops 与清理函数都原样不动。然后 `migrate`；`migrate` 成功之后才部署合成好的整个栈（服务端的新版本与别名 `live` 在其中，API 立即转到新版本），再由新的 ops 函数校正数据库用户、授权。数据库用户这一步放到最后：部署着的 ops 函数是正在服务的版本的，连的是它服务的数据库，而安装的数据库（改指向恢复出的实例时是另一个）里本来就有 catalog 的用户；因此一次运行里同时换版本、又换到一个还没有这些用户的数据库，`migrate` 会失败，要分两次做；同理，以后的版本若在数据库用户这一步加了 `migrate` 先要的东西（权限、角色属性、扩展），升级时它要在 `migrate` 之前做，不能只加在这一步里。新的 authorizer 随整个栈上线，到授权完成之间有一小段时间：新版本的 token 检查若读新迁移加的列，这段时间里它被拒绝（503，可重试），这次运行中途停下时，再跑一次即补上。`migrate` 拒绝（旧版本遇到更新的模式）时安装在这一步失败，服务端仍是原来的版本在服务，数据库里什么也没写；维护函数留在旧版本的镜像上，再跑新版本的 `install.sh` 时它会换回。模板在 51,200 字节以内时放在请求里发送（`TemplateBody`，见上面的 API 参考），否则与 CDK 自己一样先放进 CDK 引导的暂存桶、按 URL 发送（`TemplateURL`，与 CDK 一样用路径式的 URL），更新结束后删掉这个对象（桶开着版本控制，按它的生命周期规则留一份非当前版本；删不掉不影响这一步的结果）；本栈连同 CDK 写进去的元数据实测 51,365 字节，用的是后者。网络、存储与镜像仓库三个栈在这之前照常按本版本部署，它们不含 catalog 的代码。#63 之前的版本（`v1.0.0-rc.2` 及更早）回到上一个版本时仍先部署无服务器栈。冒烟检查在 VPC 内经私有 API 确认：没有 token 与伪造 token 都得到 401，管理员得到 200；五个函数都没有 Function URL；API 是私有的，资源策略拒绝其他来源。

**清理函数**（#69）：删表记下的根目录由它删除，回滚、删 tag 与删分支留下的文件（#93）也是。它与维护函数同一个镜像、同一个入口脚本，只执行 `purge`（`COLVENNA_MAINTENANCE_COMMANDS=purge`，`Main purge --until <期限>`）；与服务端同一个镜像：升级时它在 `migrate` 成功之后随整个栈换上新镜像（上文）；模式版本不是它的版本时什么都不做（退出码 0）。2048 MB，超时 15 分钟，预留并发 1；自己的执行角色：仓库前缀（与服务端相同）与以 catalog 用户的 `rds-db:connect`，没有别的权限。

- **触发**：EventBridge Scheduler 的计划 `colvenna-catalog-<env>-purge` 每 15 分钟以载荷 `"purge"` 调用它一次。Scheduler 异步调用 Lambda（[Lambda 文档](https://docs.aws.amazon.com/lambda/latest/dg/with-eventbridge-scheduler.html)，2026-10-29 查阅），用计划自己的角色：只有 `scheduler.amazonaws.com` 能代入，条件是本账号与本账号默认计划组的 `aws:SourceArn`（[Scheduler 文档的防混淆代理条件](https://docs.aws.amazon.com/scheduler/latest/UserGuide/cross-service-confused-deputy-prevention.html)），只能调用这一个函数。同一账号内角色的身份策略就足以调用（[IAM 策略评估](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_evaluation-logic_policy-eval-basics.html)），所以函数上没有资源策略，守卫拒绝非 API Gateway 调用者的规则不必放宽。上一次运行还没结束时，Lambda 把预留并发 1 挡下的异步调用留在队列里重试，最长 6 小时（[异步调用的错误处理](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)）。不由服务端在删表后立即调用：服务端所在的 VPC 没有出口，调用 Lambda 要再开一个 `lambda` 接口端点（每个端点每个可用区每小时 0.01 美元，两个可用区约每月 15 美元，[PrivateLink 定价](https://aws.amazon.com/privatelink/pricing/)），`COLVENNA_NETWORK=existing` 的用户也得补上它。删表与清理之间因此最多隔 15 分钟（加上前面排着的清理），这期间文件还在，但目录里已没有这张表，新表也不会用这个根目录。
- **每次运行**：先按记录的先后逐个领取到期的根目录，删除后再删记录；再同样领取 `file_purges` 里到期的记录（#93），删掉其中没有根引用的文件后再删记录（见上文“回滚、删 tag、删分支不等数据文件”）。领取把记录占住 20 分钟（`TablePurges.LEASE`），清理进行中每 5 分钟续一次，所以不论删多久，两处不会同时删同一个根目录；进程中途停下，续约随之停止，20 分钟后由下一次运行接着删。删除是幂等的，删的是根目录下还剩的东西。删完还有对象留下（例如被删表的写入作业还在写）算失败。运行到调用期限前 1 分钟为止（`--until`，取自 Runtime API 的 `Lambda-Runtime-Deadline-Ms` 响应头，[Lambda 文档](https://docs.aws.amazon.com/lambda/latest/dg/runtimes-api.html)），离它 2 分钟以内不再领取新的；正在删的那个在函数超时时被打断，记录留着，下次接着删。
- **失败可见、会重试**：删失败的根目录留着记录、次数与错误，下次运行再试；一次运行里同一个只试一次。有失败时这次调用以 `PurgeFailed` 报错，Lambda 对异步调用的函数错误再试两次（1 分钟与 2 分钟之后，[Lambda 文档](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-error-handling.html)）。两个告警，都不带动作，运维自己加通知：
  - `colvenna-catalog-<env>-purge-errors` 看这个函数的 `Errors`：一个 15 分钟周期里有一次就进入 ALARM，没有数据算正常；
  - `colvenna-catalog-<env>-purge-backlog` 看积压：每次运行的最后一行输出（`Main.summary`，也写进函数自己的日志）是 CloudWatch 嵌入式指标格式（EMF），Lambda 的日志据此生成 `ColvennaCatalog` 命名空间下按 `Environment` 的 `TablePurgeOldestSeconds`（两种记录中最旧的一条的秒数，#93 的记录从到期时间 `due_at` 算起；只发这一个指标，自定义指标按月计费，件数等留在摘要里，#93 的记录在摘要的 `filePurges` 里），不需要 `PutMetricData` 权限，也不需要网络（[CloudWatch 文档](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format.html)）。第一次报告最旧的记录超过 1 小时时进入 ALARM（4 个 15 分钟周期里有 1 个超过）。1 小时里没有任何一次运行报告（计划被关、函数在汇报前失败、模式版本落后于镜像）时进入 INSUFFICIENT_DATA，而不是 ALARM：否则新安装在第一次报告之前就会误报；要为它通知，再加一个数据不足的动作。安装脚本在冒烟检查里调用一次清理函数，安装完就有第一次报告。

  `GET /colvenna/admin/v1/purges` 列出还没删完的记录与最近一次的错误，#93 的记录在 `filePurges` 里（操作、目标、是否已生效、根的个数）。运维也可以随时以 IAM 认证的 Invoke 立即跑一次。
- **不越界**：记录只在删托管表时写，路径是表的根目录；所有文件访问都经过只认这个根目录的 `JailedFileIO`，唯一的例外是删完之后去掉空的库目录（见“元数据模型”的“库目录”）；删之前再确认三件事：根目录在仓库根之下，否则这次清理失败（记录、错误与告警都留着，文件不动；只有手工改过数据库才会如此）；没有已登记的表在这个位置，且根目录的 `schema/` 下没有比删表时间晚 5 秒以上的 schema 文件，否则只删记录、不删文件（计入 `skipped`）。删表时间取数据库在写记录那一刻的时间（`clock_timestamp()`，删表已持有锁）；5 秒容许数据库与存储的时钟稍有出入（`TablePurges.SKEW`），宁可留下文件也不删错。后两件只在目录不知道某张表时才会不成立：手工改过数据库，或者元数据库恢复到某次删表与它的清理之间的时间点，而清理之后有新表用了同一个根目录（恢复后的目录里没有这张新表，却有旧的记录）；这张新表的文件因此留着，按用户手册 6.3 节重新登记它。

EKS 形态不变：删表请求自己删文件（`inline`），删完回答，与以前一样；不同的只是删失败时（以前静默留下文件）现在留下记录，每个副本每 5 分钟扫一次重试（`TablePurges.SWEEP_INTERVAL`），并计入 `colvenna_catalog_table_purges_total{outcome="failed"}`；每次清扫后 `colvenna_catalog_table_purges_pending` 与 `colvenna_catalog_table_purge_oldest_seconds` 给出还剩多少、最旧的多久。不在 Lambda 中的服务端都做这个清扫，所以在 EKS 上设 `COLVENNA_DROP_PURGE=deferred` 时，文件由清扫在 5 分钟内删掉。`COLVENNA_DROP_PURGE` 同样决定删 tag 留下的文件由谁删（#93）；回滚与删分支留下的文件在一小时的宽限后由清扫删（见上文“回滚、删 tag、删分支不等数据文件”）；它们的计数是 `colvenna_catalog_file_purges_total{outcome}`、删掉的文件数 `colvenna_catalog_purged_files_total`，清扫后还剩的与最旧的是 `colvenna_catalog_file_purges_pending` 与 `colvenna_catalog_file_purge_oldest_seconds`。

**守卫**：合成阶段拒绝 `AWS::Lambda::Url`、HTTP 与 WebSocket API、非 `PRIVATE` 的 REST API、以 OpenAPI 文档（`Body`、`BodyS3Location`）定义的 REST API（其方法守卫看不到）、资源策略中有不以 `aws:SourceVpce`（或 `aws:SourceVpc`）为条件的 Allow，或没有一条覆盖所有方法与路径、拒绝其他来源的 Deny 的 REST API（守卫解析策略，不做文本匹配）、没有授权的方法、调用者不是 API Gateway 或来源不是本栈 REST API 的 Lambda 权限、开启未认证身份或经典流程或其他提供方的身份池、身份池的角色绑定、面向公网的负载均衡器，以及产品栈中的 internet gateway、NAT gateway 与 Elastic IP。CDK 的 `addProxy` 会在根路径上悄悄加一个不鉴权的 MOCK 方法，守卫在实现时就拦下了它；测试给每个函数加 Function URL、给 API 加不鉴权的方法，确认合成失败。

**实测**（2026-10-28，us-west-2，`COLVENNA_NETWORK=create`，从源码安装，`tests/serverless/run.sh`）：

- 安装约 20 分钟（多数时间在 RDS），重跑幂等；改了代码后的重跑按升级顺序进行（维护函数先换镜像、`migrate`、再换服务端）。安装的冒烟检查：没有 token 与伪造 token 401，管理员 200，没有 Function URL，API 为 `PRIVATE`。
- VPC 内的驱动函数（执行角色没有任何 S3 权限）用 Paimon 2.0.0 `RESTCatalog`（`data-token.enabled=true`）经私有 API 建库、建追加表与主键表，各写入 500 行、提交、读回一致；表凭证是数据角色经 Cognito 开发者身份的会话（`assumed-role/colvenna-catalog-<env>-data/colvenna-admin`，约 3 小时），在表根目录下可读写列举删除，在兄弟前缀、另一张表和库前缀下 AccessDenied。
- 拒绝：没有 `Authorization`（含未知路由与提交）、未知 token、`Basic`、空 bearer、小写 `bearer`、已撤销的 token、已停用的 principal，都在网关得到 401；没有授权的 token 读库与建表、非管理员访问管理面得到 403（服务端）。本机（VPC 外）解析不到 API 的私有 DNS 名；端点专用名解析到私有地址，连不上。未签名的 `GetId` 被身份池拒绝（不支持未认证访问），未签名的 `GetOpenIdTokenForDeveloperIdentity` 缺少认证。
- 冷启动（x86_64，2048 MB）：按需初始化 `Init Duration` 2,124–2,269 ms（n=5），第一个请求 1,857–2,494 ms（它才建立数据库连接）；SnapStart 恢复 510–656 ms（n=6），第一个请求 1,888–2,156 ms；authorizer 初始化 120–173 ms，第一个请求 513–627 ms。没有初始化超时。
- SnapStart 恢复后的执行环境里没有 `AWS_ACCESS_KEY_ID`，执行角色的凭证改由容器凭证端点提供。S3A 原先用 `EnvironmentVariableCredentialsProvider`（沿用自 spike，而 spike 的 SnapStart 测试只读了 config），于是第一次建表 500；改为 SDK 的 `DefaultCredentialsProvider` 后，SnapStart 与按需两种执行环境都通过了完整循环。从 SnapStart 切回关闭后，带快照的版本随之删除，只剩不带 SnapStart 的新版本；四个函数都没有预置并发。
- 改为 RDS IAM 认证（与上述 S3A 超时、3 秒锁等待）之后的复测：安装、完整循环、表凭证与全部拒绝都通过；四个函数的 `GetFunctionConfiguration` 里没有任何名含 PASSWORD 或 SECRET 的变量，也没有秘密引用或凭证；按需初始化 2,100–2,353 ms（n=4），第一个请求 1,669–1,812 ms。
- 卸载：保留数据时约 22 分钟（删除 VPC 中的函数要等 Lambda 释放网络接口），存储栈与网络栈留下；`--delete-data` 再约 10 分钟，删除数据库（不留快照）、两个桶、存储栈与网络栈。之后 `tests/serverless/verify-clean.sh` 确认没有留下任何资源；标签索引里还列着几个已不存在的端点与流日志。

**#69 实测**（2026-10-29，us-west-2，`COLVENNA_NETWORK=existing` 与 `COLVENNA_DB=existing`，接入测试环境 fx3 的 VPC、数据库与桶，从源码 `7f5f917` 安装；UAT-FLT-02，20 个写入方在 4 个 Pod 里共提交 1,200 次，结果 PASS）：

- 删表：这张 1,200 次提交的表 200，123 ms（以前 27,587–29,028 ms，或 504）。清理函数随后的一次运行删掉它，用了 86 s，根目录下没有留下对象，运行摘要里剩下的记录为 0。
- 一致性核对：两页，10.5 s（带 token）与 6.0 s，`consistent: true`（以前在网关 29 秒时 504）。
- 清理函数的空闲运行（计划每 15 分钟一次，没有要删的）：2.7–3.2 s，初始化 0.24–0.34 s，内存用到 185 MB。按 2,048 MB、x86 每 GB·秒 0.0000166667 美元，每月 2,880 次约 19,000 GB·秒、约 0.33 美元；Scheduler 每月 1,400 万次调用以内免费（[EventBridge 定价](https://aws.amazon.com/eventbridge/pricing/)）。
- 过载那 5 分钟里，服务端函数 6,041 次调用的时长：平均 112 ms，p50 34 ms，p90 132 ms，p99 3,150 ms，最长 10.4 s；并发最高到预留的 10。34 次限流都在全部执行环境被替换后的头两分钟（冷启动）。网关在执行日志里把它们记为 `Execution failed due to configuration error: Rate Exceeded.`，回给客户端的是 500（#80）。

这是评审修改前的版本：评审后核对的进度改存数据库（`consistency_checks`）、清理在进行中续约、加了积压告警与删库的工作时间；最终版本的实测见下文 #80 部分。

**#80 实测**（2026-10-29，同上的 fx3 安装，从源码 `35c721c` 安装，即 #69 合并后的 main 加上 #80；UAT-FLT-02，20 个写入方共提交 1,200 次，结果 PASS）：

- 过载那 5 分钟：Lambda 的 `Throttles` 计 397 次，访问日志里网关以 `INTEGRATION_FAILURE` 回了同样 397 次 503，没有一次 500，服务端函数的 `Errors` 为 0；旁路的探针（每秒一次读）得到 27 次 503、其余 200。写入方的每次提交恰好一次（`every-commit-once`），每个 Pod 有 20–28 次提交先失败后重试成功，没有一个写入方因 500 退出（#80 之前两个 Pod 在 500 上退出）。网关自己的 429 为 0：这个负载每秒约 18 个请求，最高的一分钟约 25 个，远低于 80 个的上限。
- 服务端函数 15,476 次调用的时长：平均 78 ms，p50 50 ms，p90 116 ms，p99 534 ms，最长 8.2 s，与 `API_REQUEST_SECONDS` 的 125 ms 相符。
- #69 的最终版本：这张 1,200 次提交的表的一致性核对两页（8.3 s 带 token、7.7 s），`consistent: true`；删表 200，117 ms；安装的冒烟检查与清扫都运行了清理函数，清理把表删完，根目录下没有对象。
- 函数错误的响应类型另做了一次：把一个版本的超时设成 1 秒、别名指向它，经 ops 函数发 3 个请求，都是 502、`INTEGRATION_FAILURE`；随后别名指回、超时恢复 60 秒、这个版本删除。

**#93 实测**（2026-10-29/30，us-west-2，测试环境 fx5：`COLVENNA_NETWORK=create`，从源码安装；`tests/serverless/run.sh` 的驱动函数在 VPC 内，动作 `purges`，经私有 API）。表按同一个历史建两次：按 `p` 分 4 个分区（每次提交 4 个数据文件），最多保留 550 个快照；main 提交 800 次，第 200 次后打 tag `old`，第 201 次覆盖全部分区，所以快照 1 到 250 过期后 `old` 独占 800 个数据文件；main 过 330、380、400 时打 tag `keep`、`forc`、`forb`，从后两个建分支 `c` 与 `b`，`b` 再提交 100 次。操作前：550 个快照、3,600 个数据文件、717 个 manifest、1,302 个 manifest list。三个请求依次是：回滚 main 到快照 350（去掉 450 个快照与 tag `forb`、`forc`），删 tag `old`，删分支 `b`；每个请求一次 HTTPS，遇 503 按客户端的间隔重发。

| 请求 | 以前的服务端（`a15985b`） | #93（`2c164ee`） |
| --- | --- | --- |
| 回滚（去掉 450 个快照） | 504，29,042 ms，服务端随后做完 | 200，5,125 ms：登记 452 个根 2,425 ms，删除 1,121 ms |
| 删 tag `old`（独占 800 个数据文件） | 第一次 503（回滚还在服务端持锁）；再发 504，29,014 ms，服务端随后删完 800 个文件 | 200，384 ms |
| 删分支 `b`（101 个快照） | 200，3,290 ms（第二次 503，删 tag 还持锁） | 200，1,787 ms：登记 103 个根 438 ms，删目录 978 ms |

- 清理函数：删 tag 的记录随即到期，下一次计划运行删了 828 个文件（数据文件与它的 manifest list 等），这次运行 25.6 s；回滚与删分支的记录在一小时的宽限后到期，到期后的第一次计划运行删了 3,040 个与 806 个文件，运行 79.6 s，内存用到 790 MB（2,048 MB 中）。
- 之后：剩下的 103 个根（main 的快照 251–350、tag `keep`，分支 `c` 的快照与 tag）逐个按自己的快照读完，行与历史相符；它们读到的 42,160 个数据文件引用对照一次列目录，一个不缺；Paimon 自己的孤儿文件清理（`LocalOrphanFilesClean`，dry run，截止时间设在一天以后）找到 0 个没有引用的文件。数据文件 3,600 → 720，manifest 717 → 125，manifest list 1,302 → 202，正与剩下的历史相符（快照 201–350 与分支 `c` 的 351–380 的数据文件）。
- 评审之前的版本（`5d3f2ae`，经 S3A 读写、没有宽限）在同样的表上：回滚 9,181 ms（服务端 9,011 ms），超过 8 秒，于是改用 S3 API（见上文）；删 tag 441 ms、删分支 2,153 ms；清理一次运行 92 s 删完 4,674 个文件，之后的核对同样一个不缺、孤儿 0 个。
- 第二次评审之后的修改（没生效的记录也等宽限、回滚与删分支推迟同表别的记录、到期时间 `due_at`、此后有提交时带条件的重发也回答 409）只改了记录何时到期与重发的回答，没有在 fx5 上再跑，只有离线测试（`FilePurgeTest`）。
- 以前的服务端在一张较小的表上（保留 150 个快照，回滚去掉 70 个）：回滚 6,215 ms，删独占 800 个文件的 tag 27,031 ms，删分支 533 ms；它的回滚与删分支留下 238 个孤儿文件。

**尚未实测**：这个形态的 `install.sh --release` 路径（发布的镜像按 digest 复制到本环境的仓库）与 `COLVENNA_NETWORK=existing`（接入用户已有的 VPC、端点与数据库）到目前只有离线测试（CDK 单元测试与 `tests/deploy` 的桩测试）；上文的实测都是 `COLVENNA_NETWORK=create`、从源码安装。发布前的 UAT 将实际运行这两条路径。

**已知限制**：请求体上限约 6 MiB（Lambda 事件，含 JSON 转义），一次提交约 41,000 个分区（单键分区），超出时网关回 413、什么都没生效（spike 第 2 节）；响应体上限 4 MiB（`COLVENNA_MAX_RESPONSE_BYTES`）。API Gateway 只在账号设置了 CloudWatch Logs 角色时写访问与错误日志，安装脚本不改账号设置，缺少时预检拒绝并给出设置方法。接口端点按小时计费：新 VPC 的三个端点在两个可用区，约每小时 0.06 美元；数据库与端点在空闲时仍计费。函数在没有请求时只有清理函数每 15 分钟运行一次（实测约每月 0.33 美元，见上文 #69 部分），其他函数不计费；它的两个告警与一个指标约每月 0.50 美元（用户手册的[成本](../manual/cost.md)一节）。

## 元数据模型

| 表 | 内容 |
| --- | --- |
| `catalog_databases` | 库、位置、选项、owner 与审计字段 |
| `catalog_tables` | 表 id、名称、路径、类型、外部表标记、当前 schema（不含 `path` 选项）、状态（`ACTIVE` / `FENCED`） |
| `table_branches` | 每个分支已发布的最新快照及统计；行锁串行化同一分支的提交；最后一次回滚（#93，认出回滚的重发） |
| `commit_intents` | 每次提交尝试：快照 id 与 UUID、提交者、请求 id、分区统计、状态（`PENDING` / `PUBLISHED` / `ABORTED`） |
| `table_partitions` | 由目录管理分区的表，按分支记录分区与统计 |
| `catalog_views`、`catalog_functions` | 视图与函数定义 |
| `principals`、`api_tokens` | 服务身份与 API key 的 SHA-256 |
| `catalog_grants`、`catalog_policy` | 服务身份的授权（见"授权"），以及每次增删推进的策略版本 |
| `audit_events` | 审计事件 |
| `table_purges` | 已删托管表还没删完的根目录（#69）：表 id 与删除时的名称、根目录、删除时间、删除者与请求 id、尝试次数、最近一次尝试与错误、下次可领取的时间；没有外键，表行已删 |
| `consistency_checks` | 分页回答的一致性核对（#69）：表 id、进度与到此为止的报告、版本号、更新时间；最后一页删除，否则一天后删除 |
| `file_purges`、`file_purge_roots` | 回滚、删 tag、删分支去掉的根与它们还没删完的文件（#93）：表 id 与当时的名称、根目录、分支、操作与目标、登记者与请求 id、是否已生效、尝试次数、最近一次尝试与错误、到期时间、下次可领取的时间；每个根的种类（快照、tag、changelog，删分支时还有分支最新的 schema）、文件名、修改时间与内容。没有外键，表删了以后由清理删记录 |

表目录保持 Paimon 自身的文件布局：schema、snapshot、tag、branch、consumer 文件都在原位置。流式读端和直接读文件的引擎（例如 Doris 的 native reader）看到的是同一张表。

**客户端选择的位置**：外部表的 `path`、`data-file.external-paths` 和 `global-index.external-path` 必须位于运维配置的外部根（`COLVENNA_EXTERNAL_LOCATION_ROOTS`）之下、仓库根之外，在建表、改表和替换表时校验。比较前先归一化：`s3a` 视同 `s3`，scheme 不区分大小写，没有 scheme 的路径按本地文件处理，`..` 先展开。外部根默认为空，即不允许客户端选择任何位置。仓库根是服务端凭证能触及的托管区域；允许客户端把位置指进去，就等于让服务端替它写入或删除别的表的文件。登记已有表（`register`）只接受仓库内、且与任何已登记表都不重叠的目录。服务端从不删除客户端选择的位置，由其所有者清理。

**表文件隔离**：客户端用自己的凭证写 manifest，manifest 里可以写任意绝对路径。服务端代表表执行删除或改写时（回滚、删 tag、截断、删表、分支操作），所有文件访问都经过只认该表根目录的 `JailedFileIO`；越界路径直接失败，客户端无法借服务端凭证触及别的表。

**表路径**：托管表默认使用 `<warehouse>/<db>.db/<table>`，与 Paimon 自身的 filesystem、JDBC 等 catalog 的默认布局相同（[`AbstractCatalog.newTableLocation`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/catalog/AbstractCatalog.java)）。改名只改元数据，路径不变。新建表时，如果该路径已被另一张表占用（例如改名后同名重建），或残留旧文件，就使用 `<table>-<8 位随机串>` 的兄弟路径，因此删除再重建永远不会读到旧文件。`DROP TABLE` 在一个事务里把表从目录中删掉，并在 `table_purges` 里记下托管表的根目录；提交之后只删除这个根目录，删完才删记录，删失败的留着记录与错误等下次再删（EKS 形态由删表请求自己删，删完回答；无服务器形态由清理函数删，见“无服务器形态”）。记录还在时，没有新表能用这个根目录或与它重叠的目录：建表时把它当作已占用，换用兄弟路径；登记与外部表返回 400。只在删完文件之后才删记录，所以根目录要么属于一张表，要么有记录，要么文件已删；清理永远不会删到新表的文件。**库目录**（[#13](https://github.com/colvenna/colvenna-catalog/issues/13)）：托管表的根目录都在库目录 `<warehouse>/<db>.db` 之下。S3A 删除一个路径之后，若父目录里已没有别的对象，会为父目录写一个零字节的目录标记 `<父目录>/`（Paimon 2.0.0 自带的 Hadoop 3.4.2，[`S3AFileSystem`](https://github.com/apache/hadoop/blob/84e8b89ee2ebe6923691205b9e171badde7a495c/hadoop-tools/hadoop-aws/src/main/java/org/apache/hadoop/fs/s3a/S3AFileSystem.java) 的 `maybeCreateFakeParentDirectory`，删除成功后无条件调用，没有关闭它的选项），所以删掉一个库的最后一张表的根目录后，库目录就以这个标记留下，库删了也还在。因此清理（`TablePurges.purge`）删完根目录后，若根目录形如 `<warehouse>/<db>.db/<table>`，就把库目录也去掉，但只在其中什么都没有时：S3 上只剩它自己的零字节标记时删掉这个标记，本地文件系统上删掉空目录。标记经 S3 API 删除（`TableFiles.removeIfEmpty`），因为 S3A 删它时又会为仓库根写一个标记。库是否还在都这样做：在无服务器形态中删库请求不碰文件，库的表在删库之后才由清理函数删，最后一张表删完时才轮到库目录；库还在时去掉空的库目录也无妨，下一张表建表时重新建目录。去掉失败算这次清理失败，记录留着，下次清理接着做（根目录已空，S3A 不再写标记）。以 `inline` 删库时，请求删完各表、删掉库行之后，也对库目录做同样的事，处理此前留下的标记；以 `deferred` 删库时请求不碰文件，与删表相同。库目录里只要还有别的东西就留着：例如改名到别的库的表（改名不改路径），或者不属于目录的文件。库的 `location` 选项是客户端给的，服务端从不写它，也就不删它。实测（2026-10-30，us-west-2，测试环境 ux2）：两种形态各建一个库与一张表再删掉（无服务器形态随后运行一次清理函数），安装的仓库前缀下没有留下任何对象；没有这项修复的版本在 EKS 形态中每删一个库都留下一个零字节的 `<db>.db/`。UAT-FLT-03 在两种形态中都通过，它的清理核对了库目录的标记已不在。schema 声明的 `data-file.external-paths`、全局索引外部路径和外部表（创建时带 `path`）的目录都是客户端选择的位置，一律不删。这比 Paimon 自身的 catalog 保守：后者会连同外部路径一起删除。删表和改名只接受表本身的名字，带 `$branch_…` 或系统表后缀的标识符返回 400。

**路径占用的并发**：建表和登记在检查路径重叠之前先占用路径，检查与插入在同一事务内完成。占用用事务级 advisory lock 分层实现：从桶根或文件系统根往下，对每一级祖先目录取共享锁，对表路径本身取排他锁。两个请求只有在一条路径等于或包含另一条时才冲突，互相嵌套的表无法并发登记；同一数据库中互不相关的建表并行执行，数据库行只加共享锁。表名另有一把锁，同名的建表、登记和改名目标排队后看到先完成的结果，返回 409。托管路径冲突时最多换 10 次随机后缀。外部位置还不能包含仓库根（包括整个存储桶），否则一张外部表就能把整个仓库纳入自己的表根。tag 的创建、删除与回滚都先锁表行再动文件，删除重建后的同名新表不会被旧请求写入。

**加锁顺序**：建表与登记按数据库行（`FOR SHARE`）→ 表名锁 → 路径锁；表级操作按表行 → 分支指针，删表在 `DELETE` 之前先锁住全部分支指针。表行锁用 `FOR NO KEY UPDATE`：持有分支指针的提交会插入引用该表的分区、分支和意图行，外键检查要取 `FOR KEY SHARE`，不能被等待指针的表级操作挡住。唯一的反向等待是自治池写入隔离标记，由 5 秒的 `lock_timeout` 兜底。

## 提交协议

S3 上的 Paimon 快照提交不是原子的（见本文开头）。关键约束是：**流式读端靠 `snapshot-N` 文件是否存在来发现新快照**（Paimon 2.0.0 的 `NextSnapshotFetcher`），并不询问 catalog。因此对读端来说，snapshot 文件写入 S3 的那一刻就是提交点，PostgreSQL 指针只服务于 `loadSnapshot` 和批读。

一次 `POST .../commit` 在该分支指针行的 `SELECT ... FOR UPDATE` 锁下依次执行：

1. 解决该分支上未完结的提交意图（见下文），使指针反映实际已发布的每一个 snapshot 文件。
2. 比较请求中的 base snapshot UUID 与指针，即 compare-and-set。不一致时：如果请求的快照 UUID 正是指针当前的快照，说明这是已成功请求的重放，返回成功；否则返回 `success=false`，客户端在新的基线上重试。
3. 在独立事务中写入 `PENDING` 提交意图，记录快照 id、UUID、提交者与分区统计。
4. 用原子的不存在才创建写入 `snapshot-N`。S3 用带 `If-None-Match: *` 的条件 `PutObject`，客户端的 endpoint、region、path-style 与静态凭证都取自服务端 FileIO 选项，与服务端读文件用的是同一个存储端点；本地文件系统用写完的临时文件做硬链接。每个桶第一次发布前，服务端会把同一个探测对象条件写两次，第二次必须返回 412。忽略 `If-None-Match` 的 S3 兼容存储因此会被识别，提交直接失败，不会悄悄退化为覆盖写。不支持原子创建的存储同样拒绝提交，不退回 rename。
5. 更新 `LATEST` 提示文件（尽力而为），在锁事务中推进指针、累加统计与分区，并把意图标记为 `PUBLISHED`。

| 故障 | 结果 |
| --- | --- |
| 第 4 步之前宕机或报错 | 没有 snapshot 文件，意图保持 `PENDING`；客户端得到 500 并重试。槽位被下一次提交占用后，该意图标为 `ABORTED`；超过 `ABANDON_AFTER`（5 分钟）仍没有文件的意图也标为 `ABORTED`。S3 客户端的调用超时 `COLVENNA_S3_CALL_TIMEOUT`（默认 30 秒）因此必须小于 5 分钟，配置不接受更大的值 |
| 第 4 步之后、PostgreSQL 提交之前宕机，或响应丢失 | 文件已存在，提交已经发生。下一次提交或 `loadSnapshot` 发现 `PENDING` 意图对应的文件存在，就把指针前滚到该快照，并记录审计事件 `ROLL_FORWARD_COMMIT` |
| 客户端重试 | Paimon 2.0.0 的 `FileStoreCommitImpl` 在提交异常后，按 `commitUser + commitIdentifier + commitKind` 检查新快照，确认自己的提交已存在就直接成功，因此重试不会产生重复提交 |
| 失去锁的副本在之后才写入 | 条件写保证它不能覆盖已发布的快照。若它先占了槽位，而它的意图仍是 `PENDING`，后来者会按 UUID 在意图表中找到该快照并接管为已发布。若它的意图已被关闭（被回滚，或超过 `ABANDON_AFTER`），迟到的写入按外部快照处理 |
| 槽位上出现不属于 `PENDING` 意图的 snapshot 文件 | 说明另一个 catalog 或写入方在提交这张表，例如没有切换的 JDBC 写入作业。表被标为 `FENCED`，并记录审计事件 `FENCE_TABLE`。发现外部快照的这次提交和之后的提交一律返回 409。Paimon 2.0.0 客户端把 409 转成 `AlreadyExistsException`（[`DefaultErrorHandler`](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-api/src/main/java/org/apache/paimon/rest/DefaultErrorHandler.java#L66-L68)），`RESTCatalog.commitSnapshot` 不处理它（[源码](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/rest/RESTCatalog.java#L466-L483)）；提交请求抛出的任何异常都被当作结果未知：`FileStoreCommitImpl.tryCommitOnce` 把它记为 `RetryCommitResult.forCommitFail`（[源码](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/operation/FileStoreCommitImpl.java#L1202-L1208)），`tryCommit` 先 `loadSnapshot`（返回 200，即 catalog 最后发布的快照）再提交，按 `commit.max-retries`（默认 10，[源码](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-api/src/main/java/org/apache/paimon/CoreOptions.java#L959-L963)）重试完才报错 `Commit failed after ... with N retries`（[源码](https://github.com/apache/paimon/blob/604e6d5e131c74a8d127333a2a3ad6d0319732bf/paimon-core/src/main/java/org/apache/paimon/operation/FileStoreCommitImpl.java#L756-L805)）。原因链的末端是最后一次的 409："... is fenced and accepts no commits: <原因>"。每次重试都得到 409，重试解决不了隔离；`CommitProtocolTest.snapshotWrittenOutsideTheCatalogFencesTheTable` 验证了这一点（`commit.max-retries=2` 时三次提交请求，各得 409）。`loadSnapshot` 和批读仍停在最后一个由 colvenna-catalog 发布的快照；流式读端按文件发现快照，仍会读到外部写入的快照，所以隔离是告警与止损手段，不能代替先切断旧写入方。切断外部写入方后，由管理员 reconcile 解除隔离，见下文“管理面修复” |
| 表已删除并重建 | 请求中的 `tableId` 与当前表不符，返回 404，旧写入方不会写进新表 |

服务端只在重发安全时返回 503，例如取不到数据库连接、超过并发上限、模式版本不符、STS 或 Cognito 限流时，这些请求都还没有任何副作用。原因是 Paimon 2.0.0 客户端的 HTTP 层（`ExponentialHttpRequestRetryStrategy`）遇到 429/503 会对任何方法自动重发，最多 5 次，间隔 1、2、4、8、16 秒（或按 `Retry-After`）。无服务器形态里另有网关自己回的 503：被限流的调用，以及运行了却失败的函数，后者的重发不一定安全，见“无服务器形态”的取舍（#80）。

锁等待超时（`lock_timeout`，SQLSTATE `55P03`）也返回 503。超时时事务已回滚，而除提交以外的写操作都先取齐所有锁再改动文件（不带 tag 创建分支、删除分支也先锁分支指针），所以超时的请求没有留下任何改动。提交是唯一的例外：snapshot 文件发布后，事务在标记意图时仍可能因锁超时而回滚。这时提交已经发生，重发的同一请求先把 `PENDING` 意图前滚，再因为请求的快照 UUID 已是指针而作为重放返回成功，仍只生效一次。`LockTimeoutTest` 验证了在分支锁上超时、什么都没写的提交，发布之后才超时的提交，Paimon 客户端自己重发超时的提交（结果只多一个快照），以及分支的创建与删除在锁超时后不留下文件。

数据库会话在请求中途断开时，按断在何处回答。会话断开指会话被服务端结束（SQLSTATE `57P01` admin_shutdown，如 `pg_terminate_backend`、数据库重启或故障切换；`57P02`、`57P03`、`57P05`、`25P03`，见 [PostgreSQL 16 的错误码表](https://www.postgresql.org/docs/16/errcodes-appendix.html)），或连接异常（`08` 类；pgjdbc 42.7.13 把连接上的 I/O 错误报为 `08006`、已关闭的连接报为 `08003`，见 [`PSQLState`](https://github.com/pgjdbc/pgjdbc/blob/REL42.7.13/pgjdbc/src/main/java/org/postgresql/util/PSQLState.java)）。HikariCP 7.1.0 遇到 `08` 类与 `57P01`–`57P03` 会把连接逐出连接池（[`ProxyConnection`](https://github.com/brettwooldridge/HikariCP/blob/HikariCP-7.1.0/src/main/java/com/zaxxer/hikari/pool/ProxyConnection.java) 的 `ERROR_STATES`），下一个请求拿到新连接。判定在 `Db.sessionLost`，`Db.inTransaction` 区分断在 COMMIT 之前还是之中；只读事务（`Db.read`，`BEGIN READ ONLY`，用于认证与加载授权）没有可提交的内容，断在 COMMIT 之中也按断在之前处理：

- **断在 COMMIT 之前**（执行语句时，或会话在事务空闲时被结束、之后还有语句）：PostgreSQL 回滚了整个事务，返回 503，与锁超时相同。没有请求会被记成成功。
  - 提交：断在发布 snapshot 文件之前，什么都没写；断在发布之后，意图仍是 `PENDING`，重发的同一请求先前滚、再作为重放成功。
  - 自己改动文件的操作（改表、schema 回滚、`rollback`、`replaceTable`、分支快进等）可能已经改了文件，文件因此领先于 PostgreSQL，与以前回 500 时相同。重发在锁下再做一次：结果与同一请求相同的（回滚到同一快照、设同一选项）就此补齐；加列这类不能重复的回 409，`replaceTable` 会再截断一次。仍不一致的，一致性检查会报出来，由 reconcile 修复。这一条是按代码推断的，未单独验证。
- **断在 COMMIT 之中**（`Db.CommitInDoubtException`；会话在最后一条语句之后被结束、服务端到 COMMIT 才发现的，也在此列）：COMMIT 是否生效，服务端无从得知。
  - 提交与读（GET）返回 503：重发的提交要么已是指针、作为重放成功，要么先前滚（`CommitService.commit` 对快照已是指针的请求回答重放）；读的写入只是解决意图，再做一次不会多改什么。
  - 其他写返回 500，说明结果未知，由调用方核实后再决定是否重发：它们重复一次就不是重放，例如再签发一个 token、`replaceTable` 再截断一次。`rollback` 在 5 分钟内由同一调用方重发时不会再回滚一次（#93，见上文“元数据变更的重试”）。

`SessionLossTest` 用 `pg_terminate_backend` 验证了：等锁时会话被结束（什么都没写，重发成功）；发布之后会话被结束（重发作为重放成功）；Paimon 客户端自己重发、只多一个快照；COMMIT 之中会话被结束（用延迟约束触发器在 COMMIT 时结束会话）时，提交回 503、重发作为重放成功，建库回 500。`DbTest` 验证了最后一条语句之后会话被结束时，写事务按结果未知处理，只读事务不是。

**元数据变更的重试**：建库、建表、改表、改名、删表在响应丢失后都只生效一次，`RetryConsistencyTest` 用丢弃响应的代理逐一验证。回滚也是（#93）：同一调用方 5 分钟内发来的同一个回滚算作重发，带起点快照的回答 200，不带的在分支此后有过提交时回答 409，都不再回滚（见“无服务器形态”的“回滚、删 tag、删分支不等数据文件”，`FilePurgeTest`）。POST 不会被 HTTP 层重发，应用重试同一调用时，得到的是与结果状态一致的回答：已存在返回 409，加列返回列已存在，改名后的旧名返回 404；带 `ignoreIfExists` 重试则成功。DELETE 是幂等方法，HTTP 层会自动重发，重发的请求发现表已不存在，所以 `dropTable(ignoreIfNotExists=false)` 会报表不存在，表本身只删了一次。GET 响应丢失时由 HTTP 层透明重试。

`rollback`、`replaceTable`（先截断提交，再追加新 schema）和分支快进会在服务端自己操作快照文件；回滚去掉的快照、tag 与 changelog 所引用的文件由清理之后删除（#93）。它们同样持有分支指针锁，结束时从文件重建指针，并关闭该分支的未完结意图。

**一致性核对**：`GET /colvenna/admin/v1/consistency/{db}/{table}`，仅限管理员，是恢复证据的来源。

- **核对前的状态**：每个分支先记下原始的指针与未完结意图，再像普通读者一样解决这些意图。所以这次 GET 本身可能前滚提交并写审计，与 `loadSnapshot` 相同。
- **核对项**：
  - 指针与最大 `snapshot-N` 文件；
  - PostgreSQL 保存的已发布快照是否就是快照文件；
  - head 快照的数据文件行数是否等于它记录的总数；
  - 最早与最新的快照、二者之间均匀抽取的快照（共 20 个）以及每个 tag，引用的数据文件是否都存在。频繁提交的表会保留上千个快照，逐个核对太慢，所以抽样；
  - main 分支 schema 的 id 与内容是否与最新 schema 文件一致。
- **结果**：读不到元数据（例如 manifest 丢失）记为问题，而不是 500；任何不一致都列在 `problems` 中。文件已不在的表（例如元数据库恢复点之后删掉的表）同样返回报告：`no schema file in <path>, where the catalog serves schema <id>`，以及每个有快照的分支的 `<branch>: pointer snapshot <n> has no file`；这种表应当注销。
- **适用条件**：报告针对静止的表。提交进行中时，快照文件会在发布与指针更新之间短暂领先于指针。
- **分步与分页**（#69）：各分支的指针和 head（行数核对在内）是一步，之后每个抽样快照、每个 tag 的数据文件各是一步；抽样的两端与各 tag 的快照在分支的第一步定下。一次回答里，每个数据目录只列一次（不再对每个文件发 HEAD），manifest 以 16 路并发读并缓存。`COLVENNA_REQUEST_WORK_TIME`（EKS 默认 `PT0S`，不限；Lambda 中 `PT8S`）用完后不再开始新的一步，把到此为止的报告与进度存进 `consistency_checks`，带着报告与 `nextPageToken`（这条记录的 id）回答；带 `pageToken` 再问时从停下的地方接着做，最后一页（`nextPageToken` 为 null）才是完整的报告，`consistent` 只在它上面可能为真；这时记录改存完整的报告，最后一页的回答丢了，拿同一个 token 再问得到同一份报告；记录都在一天后删除。token 属于这张表（表 id）：别的表、删掉重建过的表、不认识的或过期的 token 得到 400；两个请求拿同一个 token 同时往下做时，后存的那个得到 409，再拿同一个 token 问即可。报告存在服务端，调用方改不了它。分页时每个文件仍只计一次：一个数据文件被一个分支里连续的一段快照引用，所以一步在开始前，把之前回答里做过的、可能与它共用文件的步骤（抽样快照：前一个抽样；tag：两侧的抽样与前一个 tag）的文件记为已核对；被记下的都已核对过，不会漏查，每一步至多多读三个快照的 manifest。`RequestWorkTimeTest` 让每页只做一步，比对逐页得到的最终报告与一次回答的报告完全相同。

**管理面修复**：仅限管理员，各记一条审计：

- **重新对齐**：`POST /colvenna/admin/v1/tables/{db}/{table}/reconcile`。从文件重新读取每个分支的 head 与表的 schema，发布文件中超出指针的快照，再解除隔离；只在文件里存在的分支补上指针，分支目录已不存在的指针删除。适用于两种情况：
  - 管理员已切断外部写入方：读端已经能看到文件中的快照，所以由 catalog 发布它们；
  - 元数据库从早于文件的备份恢复：指针落后于文件，需要追上。

  返回每个分支前后的快照 id，以及之前的状态（如 `FENCED`）。没有 schema 文件的表无从对齐，返回 400，提示文件已不在时应注销。
- **注销**：`POST /colvenna/admin/v1/tables/{db}/{table}/unregister`。把表从 catalog 移除，不删任何文件，供表切回其他 catalog 使用。

## 分支、tag 与 consumer

- **分支**：分支通过 REST 端点创建、删除和快进，指针按分支分别维护。不带 tag 创建的分支从 main 当前快照开始，与上游 REST 服务端语义相同；服务端会把该快照文件复制到分支目录，让读分支的引擎能在文件系统上找到它。这种分支不持有 tag，main 的快照过期不会保护它与 main 共享的数据文件。需要长期保留的分支应从 tag 创建。
- **tag**：tag 可以经 REST 端点创建和删除，Flink、Spark 的 procedure 也会直接写 tag 文件。服务端始终从文件读取 tag。
- **consumer**：consumer 由流读作业直接写文件，REST 端点只列出和重置。

## 认证与审计

认证采用 API key 语义的 bearer token，由服务端签发，不接外部身份提供方（需求方的决定，见 [v1.0 设计](v1.0-design.md)第 2 节的非目标）：Paimon 2.0.0 客户端只有 `bear` 和 `dlf` 两种 `token.provider`，没有 OIDC。

- token 是 `colvenna_` 前缀加 32 字节随机数，只在签发时返回一次；PostgreSQL 只存 SHA-256、所属 principal、过期时间和撤销标记。
- 缺失、伪造、过期、撤销的 token，以及已停用的 principal，都返回 401；服务身份访问管理面返回 403。
- 认证结果在副本内缓存 `COLVENNA_AUTH_CACHE_TTL`（默认 2 秒），撤销最迟在这个时间后对所有副本生效。
- 客户端不会刷新 bearer token，轮换顺序是：签发新 token → 更新 Kubernetes Secret → 重启作业 → 撤销旧 token。
- 首个管理员由 `bootstrap-admin` 命令创建，token 写入权限为 600 的文件，不打印到日志。
- 管理面的身份与 token：`GET|POST /colvenna/admin/v1/principals`、`POST /colvenna/admin/v1/principals/{name}/disable`、`GET|POST /colvenna/admin/v1/principals/{name}/tokens`（签发时 `ttl` 默认 `P90D`，上限 400 天）、`DELETE /colvenna/admin/v1/tokens/{id}`（撤销）。

审计事件在请求结果确定后，用独立事务写入 `audit_events`，失败的请求不会被记成成功：HTTP 200 但 compare-and-set 失败的提交记为 `FAILED`。记录范围是：所有写操作、凭证签发、认证与授权拒绝，以及服务端执行的前滚（`ROLL_FORWARD_COMMIT`）与隔离（`FENCE_TABLE`）。每条事件关联请求 id（同时作为响应头 `X-Request-Id` 返回）、principal、动作、资源、结果、HTTP 状态和时间。前滚与隔离事件关联的是触发它们的那次请求及其 principal。提交事件另记快照 id、`commitUser`、`commitIdentifier` 和 `commitKind`。审计事件可经管理面 `GET /colvenna/admin/v1/audit` 查询，见下文“授权”。

库与表的 owner、created_by、updated_by 记录谁建了、谁改了它们：这三个字段总会通过 GetTable/GetDatabase 返回，`sys.tables` 由此填充。`$partitions` 的分区级 created_by/updated_by 只对 `metastore.partitioned-table=true` 的表由目录维护；其他表的分区由客户端从文件列出，没有这两个字段。这是 Paimon 2.0.0 的客户端行为，服务端不强制所有表改由目录管理分区。

## 按表数据凭证

按表数据凭证让引擎不持有任何对象存储凭证：作业配置里只有 catalog 的 bearer token，表文件的读写一律用 `GET .../tables/{table}/token` 签发的临时凭证（客户端设 `data-token.enabled=true`，Paimon 的 `RESTTokenFileIO` 负责取用与续期）。

- 签发方式（`COLVENNA_DATA_TOKEN_MODE=aws-sts`）：服务端用自己的 web identity 调用 `AssumeRoleWithWebIdentity`，承担数据角色 `COLVENNA_DATA_TOKEN_ROLE_ARN`，并附带只覆盖该表根目录的会话策略。不用自己的角色会话做角色链，因为链式会话最长 1 小时，而 Paimon 客户端在凭证剩余不足 1 小时时就会刷新，那样每次文件操作都会请求 `/token`。web identity 由 `COLVENNA_DATA_TOKEN_WEB_IDENTITY` 选择：
  - `token-file`（默认，EKS 形态）：`AWS_WEB_IDENTITY_TOKEN_FILE` 处 Pod 的服务账号 token（IRSA）。
  - `cognito`（无服务器形态）：Lambda 执行角色的凭证本身就是角色会话，不能再链。服务端先用执行角色的凭证（签名调用，不是匿名）对身份池 `COLVENNA_DATA_TOKEN_COGNITO_IDENTITY_POOL_ID` 调 `GetOpenIdTokenForDeveloperIdentity`，登录名是开发者提供方 `COLVENNA_DATA_TOKEN_COGNITO_DEVELOPER_PROVIDER` 下的 STS 会话名（每个 principal 一个 Cognito 身份，CloudTrail 里的 identity id 与会话名都能对应到 principal），再用得到的 OpenID token 调 `AssumeRoleWithWebIdentity`，会话策略与时长和 `token-file` 相同。Cognito 客户端的区域取身份池 id 的前缀。数据角色的信任策略只信任 `cognito-identity.amazonaws.com`，并限定 `aud` 为该身份池、`amr` 为 `authenticated`；身份池不开未认证身份、不挂角色，只有服务端的执行角色有 `cognito-identity:GetOpenIdTokenForDeveloperIdentity` 权限（无服务器 spike 中实测，见 [调研报告](../research/serverless-spike.md) 第 1 节）。在没有出口的 VPC 中需要 `sts` 与 `cognito-identity` 接口端点。
  - 两种方式共用同一个不带凭证的 STS 客户端与同一个 `AssumeRoleWithWebIdentity` 调用。STS（`cognito` 下还有 Cognito）限流时 `/token` 返回可重试的 503，其他拒绝返回 500。
- 会话策略：表根按存储的文本解析（不经 URI，名字里的任何字符都不会提前结束路径），写入 ARN 与条件前把 `*`、`?`、`$` 转义成 `${*}`、`${?}`、`${$}`。对象操作只允许表根目录键本身和 `<表根>/*`（S3A 探测目录时会 HEAD 根键，403 会被当成错误）；列举只允许前缀 `<表根>/*`，不含裸前缀，避免列出名字以本表名开头的兄弟表（如 `orders_v2`）。实际权限是会话策略与数据角色策略的交集，数据角色本身只能触及仓库前缀。服务端只用一个数据角色，表与数据域之间的隔离边界是会话策略；按数据域拆角色留作后续选项。仓库桶使用 SSE-S3，策略不含 KMS 权限；改用客户管理的 KMS 密钥时须同时授予 `kms:Decrypt` 与 `kms:GenerateDataKey`。
- 有效期 `COLVENNA_DATA_TOKEN_DURATION`，默认 `PT3H`，允许 `PT1H30M`–`PT12H`。同一 principal、同一张表、同一访问级别的凭证在副本内缓存，剩余有效期不足 70 分钟就重新签发，因此客户端拿到的凭证总能用 10 分钟以上才进入刷新窗口。
- 返回的键：`fs.s3a.access.key` / `fs.s3a.secret.key` / `fs.s3a.session.token` / `fs.s3a.aws.credentials.provider`（`TemporaryAWSCredentialsProvider`）/ `fs.s3a.endpoint.region` 供 Flink 与 Spark 的 Paimon S3 FileIO 使用，并同时给出它映射到这些键的别名 `s3.access-key` / `s3.secret-key` / `s3.aws.credentials.provider`，客户端自己 catalog 选项里的静态 `s3.*` 键因此无法盖过临时凭证；同值的 `s3.access-key-id` / `s3.secret-access-key` / `s3.session-token` / `s3.region` 供 Doris 的 native reader 使用，它只识别这组键名（在 Colvenna Lake 的 Doris 4.1.4 与自建的 Paimon 2.0.0 读取器上实测）。
- STS 会话名是 `colvenna-<principal>`，CloudTrail 中可以把对象访问归到具体服务身份；principal 名含 STS 不允许的字符或超长时，加上哈希后缀，两个 principal 不会共用会话名。每次签发（含缓存命中与失败）都记审计事件，记录会话名、`accessKeyId`（非密钥，可与 CloudTrail 数据事件逐次对应）、是否缓存命中、访问级别、前缀和过期时间，不记任何密钥；凭证对象的 `toString` 也不含密钥。
- 撤销的作用范围：撤销 token 或停用 principal 后，`/token` 立即（最迟一个认证缓存周期）返回 401，不再续发；已经发出的凭证仍在其有效期内可用，最长为 `COLVENNA_DATA_TOKEN_DURATION`（默认 3 小时）。需要立刻作废时，在数据角色上执行 IAM 的“撤销活动会话”（附加按 `aws:TokenIssueTime` 拒绝的策略），所有已签发的表凭证同时失效。这是会中断业务的紧急手段：Paimon 的 `RESTTokenFileIO` 只在凭证剩余不足 1 小时时重新取用，遇到 403 不会刷新；colvenna-catalog 各副本的缓存也会继续发出旧凭证，直到其剩余不足 70 分钟。因此撤销会话后须先重启 colvenna-catalog 副本以清空缓存，再重启受影响的作业（Doris 刷新 catalog），否则表数据最多会有约 `COLVENNA_DATA_TOKEN_DURATION` 减 1 小时（默认 2 小时）不可读写。
- 只为托管表签发；外部表在仓库之外，数据角色触及不到，返回 400。
- STS 调用与返回给客户端的 S3 区域都是 `COLVENNA_DATA_TOKEN_REGION`（默认取 `AWS_REGION`），即要求仓库桶与服务端在同一区域，不支持跨区域的桶。凭证的访问级别由授权决定：对该表有 `WRITE` 的身份拿到读写凭证，只有 `READ` 的拿到只读凭证（`s3:GetObject` 与列举），没有 `READ` 的得到 403。表级凭证不提供列级或行级隔离：同一个数据文件里的列与行无法用对象路径分开。

## 授权

服务端按服务身份（principal）授权。授权存于 `catalog_grants`，由管理面维护：

- `POST /colvenna/admin/v1/grants`
- `GET /colvenna/admin/v1/grants?principal=`
- `DELETE /colvenna/admin/v1/grants/{id}`

创建时的请求体：

```json
{"principal": "...", "scope": "...", "database": "...", "object": "...",
 "privilege": "...", "effect": "...", "columns": ["..."],
 "purpose": "...", "environment": "...", "approvedBy": "...", "approval": "...",
 "expiresAt": "..."}
```

每条授权都必须写明用途、审批人、审批单和失效时间，缺一项回 400，所以每条授权都能追溯到一次审批；`environment` 省略时取本服务的环境（`COLVENNA_ENVIRONMENT`，默认 `dev`），`effect` 省略时为 `ALLOW`。只有本服务环境内、尚未到期的授权才参与判定，到期即失效。每次增删都会推进策略版本，并记录一条审计，详情写全该授权的各项内容；撤销后数据行虽已删除，审计里仍保留它曾授予的内容。

- **范围**：`CATALOG`（整个目录）、`DATABASE`、`TABLE`、`VIEW`、`FUNCTION`。分支与系统表（`t$branch_b`、`t$snapshots`）按其所属的表判定；视图与函数按完整名称判定，不做 `$` 拆分。判定目标取自请求路径，不从资源字符串回解析。
- **级别**：`READ` ⊂ `WRITE` ⊂ `MANAGE`。
  - `ALLOW` 允许该级别及以下，`DENY` 禁止该级别及以上。
  - 只要有覆盖到该对象的 `DENY`，它就优先于所有 `ALLOW`。
  - 管理员 principal 没有隐含的目录权限：管理面与数据访问分开授予。管理员要访问表时也须有显式授权，同样要写明审批，并接受复核。
  - 没有任何授权的服务身份什么都看不到。
- **动作分类**：每个目录动作在 `Authorizer` 中都有且只有一种判定规则，未登记的动作一律拒绝（fail closed）。另有一个测试扫描全部路由，保证每个动作都已登记。
  - `READ`：读取表、视图、函数及其快照、tag、分支、consumer、分区，查询鉴权（`/auth`），签发凭证。
  - `WRITE`：提交、回滚，tag、分支、分区的增删，consumer 重置。
  - `MANAGE`：库、表、视图、函数的增删改与改名，登记、替换表，schema 回滚。
    - 改名还需要目标名上的 `MANAGE`。
    - 建表、视图、函数还需要新名称本身上的 `MANAGE`，对该名称的 `DENY` 因此生效。
    - 删库还需要库中每个对象上的 `MANAGE`，以免级联删除绕过对象上的 `DENY`。
- **列举**：库与对象的列举只返回调用方能看到的项。
  - **分页**：从过滤后的结果组页，下一页 token 是最后一个返回项的键，绝不会是调用方看不到的名称。
  - **库的可见性**：库在以下两种情况下可见：调用方对整个库有 `READ`；或调用方能读到库中的至少一个对象，且库没有被 `DENY READ`。
  - **按 id 读表**：调用方无权读取时返回 404，不泄露表名；审计记为 `DENIED`，资源是表 id，不写表名。
- **列级判定**：只由表级授权决定。若存在表级的、带 `columns` 的 `READ ALLOW`，而没有其他不带列清单的表级授权，调用方只能读这些列，`/auth` 会拒绝选中其他列的查询（不指定列视为选中全部列）。库级或目录级授权不会放宽表级的列限制。
- **只读凭证与 consumer**：只有 `READ` 的身份拿到的凭证只能读对象，唯一的例外是写该表（及其分支）的 `consumer/` 目录：带 consumer id 的流读要在那里记录进度。
- **生效时间**：
  - 授权在副本内按 `COLVENNA_AUTH_CACHE_TTL` 缓存，增删与到期最迟在一个缓存周期后对所有副本生效。
  - 生效之后，`/token` 不再为该身份签发或续签凭证。已发出的凭证仍在其有效期内可用，见上文“按表数据凭证”的撤销说明。
- **拒绝**：被拒绝的请求返回 403，资源类型对应 Paimon 客户端的 `TableNoPermissionException`、`DatabaseNoPermissionException`、`ViewNoPermissionException`。

**默认库**：库 `default` 对每个已认证的服务身份都可见（`GET_DATABASE` 与各列举动作），因为 Flink 与 Spark 的 Paimon catalog 启动时会查找它。可见不等于可访问，库中的对象仍按授权判定；对它的 `READ` 拒绝会把它隐藏。安装脚本会建好这个库。

**审计**：

- **记录范围**：每个作用于目录对象的请求都会记录，允许的读取与列举（含库的列举）也在内：目录与授权的允许和拒绝都可追溯。只有取配置（`/v1/config`）不涉及目录对象，不作为判定记录；被拒绝的取配置请求（如无效 token）仍照常记录。
- **详情**：包括策略版本、所需级别（允许的列举只返回可见项，没有所需级别；因库不可见而拒绝的列举为 `READ`），以及判定所依据的授权 id：允许时是据以允许的授权（`/auth` 拒绝某些列时，是限定列的那条授权），拒绝时是命中的 `DENY`（没有授权允许时为空）。被拒绝的请求记为 `DENIED`，拒绝也带策略版本与所需级别。
- **资源**：
  - 作用于一张表、一个视图或一个函数的请求，不论成功、失败还是被拒，都记 `<库>.<名>`，按 `resource=<库>.<表>` 能查到它的全部事件（失败的错误里常只有裸表名，例如被隔离的提交，事件不用它）；分支与系统表按路径记（`<库>.<表>$branch_b`）；按 id 读表被拒时记表 id（见上文"按 id 读表"）；
  - 路由本身不带资源的请求（建库、改名、授权）记的是请求体中的对象名，改名记源对象的全名，失败与被拒也一样；
  - 建表、登记表、建视图、建函数成功时记新对象的全名；
  - 在库级判定处就被拒绝的建对象请求，此时还没读请求体，记的是库名；
  - 授权已通过、之后失败的建对象请求（例如名称已存在），记的是新对象的全名。
- **查询接口**：`GET /colvenna/admin/v1/audit` 按以下条件筛选，返回请求 id、时间、principal、动作、资源、结果、HTTP 状态和详情，按时间倒序，单次最多 1000 条：
  - `principal`、`action`、`resourceType`、`resource`、`outcome`；
  - 时间范围 `since`/`until`（ISO-8601）；
  - 条数 `limit`。

**执行方与边界**：服务端只认识服务身份（principal），看不到终端用户。一个引擎服务的不同用户（例如 Doris 的两个用户经同一个 catalog 查询）到达服务端时是同一个 principal；终端用户的权限与查询审计由引擎自己执行，要把一次访问归到具体用户，须结合引擎的审计日志。各项控制的执行方：

| 控制 | 执行方 | 能保证什么 | 不能保证什么 |
| --- | --- | --- | --- |
| 列举、读取、创建、修改、删除目录对象 | colvenna-catalog | 每个请求执行前判定；没有授权或命中 `DENY` 的请求被拒绝并审计 | — |
| 提交快照、回滚、tag、分支 | colvenna-catalog | 没有 `WRITE` 的身份不能经 catalog 发布快照 | 能直接写 S3 的写入方绕过 catalog 写出的快照只能事后发现：表被隔离（`FENCED`，见“提交协议”） |
| 表文件的读与写 | AWS IAM 会话策略（按表数据凭证） | 每份凭证只覆盖一张表的目录；只有 `READ` 时只能读对象，以及写本表 `consumer/` 下的进度 | 只在 `COLVENNA_DATA_TOKEN_MODE=aws-sts` 时成立。不启用时，表文件只受引擎自身 IAM 角色的约束，`READ` 授权挡不住 S3 写入；启用时，引擎自己的角色也不能另有读写表数据的权限 |
| 撤销与到期 | colvenna-catalog 与 AWS STS | 最迟一个认证缓存周期（默认 2 秒）后生效，此后不再签发、续签数据凭证 | 已签发的数据凭证在有效期内仍可使用，见“按表数据凭证”的撤销说明 |
| 列级判定 | Paimon 客户端 | 表开启 `query-auth.enabled=true` 时，客户端先调用 `/auth`，不允许读的列被拒绝 | 不是隔离边界：持有该表数据凭证的引擎能直接读出所有列的文件，敏感列应放进独立的表。各引擎是否执行 query-auth 未验证 |
| 行过滤、列脱敏，终端用户的库、表、列、行权限 | 引擎（例如 Doris 的 RBAC） | 由引擎按其用户与角色判定 | 服务端不提供行列策略，也不参与、不审计这一层 |
| 管理面（服务身份、token、授权、审计查询） | colvenna-catalog | 只有管理员身份能调用 | — |

**升级**：从没有授权的版本升级后，所有服务身份（包括管理员）都没有目录权限。须在切换流量前为它们创建授权。

## 已知限制

- 只有 `serve` 要求 TLS（或在 Lambda 中要求回环明文）；`migrate` 与 `bootstrap-admin` 不开端口，可以在没有证书的维护环境中运行。
- 各项等待分别有上限（锁等待、S3 调用、连接池取连接的 10 秒），Lambda 中它们的和也必须短于 29 秒，但没有覆盖整个请求的截止时间：一个请求还可能依次等待自治池写入（锁等待 5 秒）和服务端 FileIO 的 S3A 调用（各有其超时），所以极端情况下仍可能超过网关超时。这时网关回 504，提交仍按“提交协议”只生效一次。
- 内部错误只回通用信息，详情写入服务端日志，以 `X-Request-Id` 关联。两种 500 例外，信息面向用户：Paimon 拒绝 schema 变更时抛出的 `IllegalStateException`，按上游语义原样返回；以及响应体超过 `COLVENNA_MAX_RESPONSE_BYTES` 时的错误，写明上限与响应的字节数，列举中单独一项就超限时还写明是哪一项。
- schema 文件沿用 Paimon `SchemaManager` 的写法，不是条件写。同一时刻只有持锁的副本写 schema，但已经失去锁、仍在执行的副本理论上可能事后留下与 PostgreSQL 不一致的 schema 文件。`GetTable` 以 PostgreSQL 为准；两者是否一致（id 与字段、键、选项）由“提交协议”一节的一致性核对检查。

> 注：本文中不带链接的 issue 与 PR 编号（如 #93）是本项目早期私有仓库的，本仓库没有对应的页面；带链接的编号（如 [#9](https://github.com/colvenna/colvenna-catalog/issues/9)）是本仓库现在的。
