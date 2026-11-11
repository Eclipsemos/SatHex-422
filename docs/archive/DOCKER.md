# Docker commands (gs422 2.2.0)

所有 AT 命令（含本页 Docker 功能）已统一收录到 [AT 命令使用手册](../AT-COMMANDS.md)。

通过本机 Docker Engine Unix socket 读取信息，不依赖 Docker Python SDK，
不执行串口提供的 shell。请求/响应使用协议规定的 CRLF。

| 指令 | 返回内容 |
|---|---|
| `AT+DOCKER` 或 `AT+DOCKER?` | STATE、VERSION、CONTAINERS、RUNNING、PAUSED、STOPPED、IMAGES |
| `AT+DOCKER=LIST,0` | INDEX、TOTAL、ID、NAME、IMAGE、STATE、NEXT |
| `AT+DOCKER=IMAGES,0` | INDEX、TOTAL、ID、TAG、TAG_COUNT、SIZE_BYTES、NEXT |
| `AT+DOCKER=INFO,容器名` | ID、NAME、IMAGE、STATE、HEALTH、EXIT_CODE、OOM_KILLED、RESTART_COUNT |
| `AT+DOCKER=STATS,容器名` | ID、CPU、MEM_USAGE_BYTES、MEM_LIMIT_BYTES |
| `AT+DOCKER=START,容器名` | START、ID、OK |
| `AT+DOCKER=STOP,容器名` | STOP、ID、OK |
| `AT+DOCKER=RESTART,容器名` | RESTART、ID、OK |

所有回复以 `+DOCKER:` 开始。容器参数接受完整名称或完整 64 位 ID，不接受短 ID。
LIST/IMAGES 可省略页码，默认 0；按完整 ID 排序，每页一项。使用 NEXT 继续，
`NEXT=-` 为最后一项，越界返回 INDEX、TOTAL、END=1。各页独立读取，没有跨页
快照；其他程序增删容器时应重新获取列表。镜像仅显示首个 TAG 和标签总数。
文本字段按 UTF-8 百分号编码，逗号、百分号等不会破坏协议；文本最多保留原始
160 个字符，完整 ID 不截断。接收端应先按逗号分字段，再解码字段值。

CPU 使用 Docker 语义：单个核心满载约 100%，可超过 100%，与 AT+CPU 的整机
归一化百分比不同。无有效差分窗口时为 `-`。内存字段以字节为单位，USAGE 是
Engine 原始 usage（含缓存），不等同于 docker stats CLI 扣减缓存后的数值。
容器未运行时 STATS 返回 NOT_RUNNING。未配置健康检查时 HEALTH=NONE。

## 控制配置

`/etc/gs422.conf`：

```ini
docker_socket=/var/run/docker.sock
docker_allow=
```

空白名单允许查询全部容器，拒绝所有控制。启用示例：

```ini
docker_allow=my-worker;another-worker
```

填写允许控制的完整容器名或完整 ID，用分号分隔，修改后重启 gs422。
名称授权也适用于以后同名重建的容器；需要固定实例时使用完整 ID。
每次控制先 inspect 验证白名单，再用捕获的不可变 ID 发起操作，避免重命名
导致请求转向其他容器。暂停、重启中或 dead 的容器返回 CONFLICT。

OK 表示 Engine 操作完成，不保证业务健康或进程持续运行。
STOP/RESTART 给进程 10 秒退出时间，之后 Engine 可能强制结束它。
控制请求发出后超时返回 OUTCOME_UNKNOWN，操作可能已生效；先用 INFO 查询，
不要盲目重发。服务不自动重试控制操作。

这一版只管理已有容器，不支持创建、删除、exec、拉取镜像或任意 API。
现有 4G/5G 脚本使用 `docker run --rm`，任务结束后容器即删除，不能用 START
重建任务；创建算法任务需要另外定义输入路径、资源和输出等协议。

## 并发与错误

Docker 查询与控制各有独立 worker，队列容量分别为 8 和 1，不阻塞 STA/CPU。
排队超过 5 秒的请求不执行。子进程总预算分别为查询 10 秒、控制 22 秒；
Engine JSON 响应上限 2 MiB，超过返回 TOO_LARGE。LIST 虽然串口分页，仍需
读取 Engine 列表，因此大量对象超过该上限时无法返回列表。

错误格式为 `+DOCKER:ERR,代码`，排队超时也可能只返回 `+DOCKER:ERR`。
代码包括 BAD_COMMAND、BUSY、NOT_FOUND、EXACT_TARGET_REQUIRED、NOT_ALLOWED、
NOT_RUNNING、CONFLICT、DENIED、BAD_REQUEST、ENGINE_ERROR、API_VERSION、
UNAVAILABLE、TIMEOUT、INVALID_RESPONSE、TOO_LARGE、OUTCOME_UNKNOWN。

客户端探测 `/version`，在 Engine 支持范围内选择最高 1.44、最低 1.24 的 API。
参考 [Engine API](https://docs.docker.com/reference/api/engine/) 和
[v1.44 API](https://docs.docker.com/reference/api/engine/version/v1.44/)。

## 验证

`python3 -m unittest discover -s tests -v` 覆盖解析、白名单、不可变 ID、
API 协商、资源计算和控制超时不重试。
`sudo python3 tests/live_docker.py` 创建唯一命名的临时容器，仅使用本地已有
4G 镜像，不拉取镜像；经 PTY 测试完整生命周期和并发查询，最后删除本次容器。
`--installed` 可测试已安装程序。测试不会启动、停止或删除其他容器。
