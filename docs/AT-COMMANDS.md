# RS422 AT 命令使用手册

适用版本：**gs422 2.2.0，协议修订 4**。更新日期：2026-11-06。
本文统一说明当前已实现的全部 AT 命令、参数、返回值、单位、错误和配置。
示例数值仅用于说明格式，不代表设备实时读数。

## 1. 串口和通用规则

| 项目 | 设置 |
|---|---|
| 波特率 | 9600 |
| 数据位 / 校验 / 停止位 | 8 / 无 / 1，即 8N1 |
| 流控 | 无 |
| 字符编码 | UTF-8；命令关键字使用大写 ASCII |
| 发送结束符 | CR、LF、CRLF 均可；调试工具建议选择 CRLF |
| 回复结束符 | CRLF，每条回复一行 |
| 单条命令长度 | 不含结束符最多 512 字节；这不是回复长度上限 |

命令区分大小写，不自动去掉首尾空格；空行忽略。没有命令回显或主动上报，
查询回复后不会再附加一行 `OK`。未知命令、非法 UTF-8 或超长命令返回 `ERROR`。
表中的 `<容器>`、`<页码>` 等是占位符，发送时替换为实际值，不要包含尖括号。

下表明确列出的基本查询可带 `?`；不要给配网命令或 Docker 子命令追加 `?`。
响应按完成顺序发送，没有请求编号。建议收到上一条回复再发下一条，尤其不要
同时发送多条 Docker 请求。CPU 一行较长，9600 波特率下建议约每秒查询一次。

## 2. 全部命令速查

| 命令 | 可用别名 | 功能 |
|---|---|---|
| `AT+STA` | `AT+STA?` | NPU 温度与功耗、RK 温度、12V 电流 |
| `AT+CPU` | `AT+CPU?` | 整机和各核使用率、负载、频率、温度 |
| `AT+IP1` | `AT+IP1?` | 第一物理以太网接口 IPv4 和子网掩码 |
| `AT+IP2` | `AT+IP2?` | 第二物理以太网接口 IPv4 和子网掩码 |
| `AT+WIFI` | `AT+WIFI?` | 当前 Wi-Fi 的 SSID、IPv4 和子网掩码 |
| `AT+WIFI=<SSID>,<密码>` | 无 | 连接 Wi-Fi，验证成功后保存配置 |
| `AT+DOCKER` | `AT+DOCKER?` | Docker Engine 版本、容器及镜像数量 |
| `AT+DOCKER=LIST,<页码>` | `AT+DOCKER=LIST` 默认第 0 页 | 分页查看全部容器 |
| `AT+DOCKER=IMAGES,<页码>` | `AT+DOCKER=IMAGES` 默认第 0 页 | 分页查看镜像 |
| `AT+DOCKER=INFO,<容器>` | 无 | 容器状态、健康检查、退出码等 |
| `AT+DOCKER=STATS,<容器>` | 无 | 运行中容器的 CPU 和内存占用 |
| `AT+DOCKER=START,<容器>` | 无 | 启动白名单内的已有容器 |
| `AT+DOCKER=STOP,<容器>` | 无 | 停止白名单内的容器 |
| `AT+DOCKER=RESTART,<容器>` | 无 | 重启白名单内的容器 |

## 3. AT+STA：设备状态

发送 `AT+STA` 或 `AT+STA?`，回复示例：

```text
+STA:NPU_TEMP=30.10C,NPU_POWER=4.920W,RK_TEMP=35.15C,VIN12_CURRENT=1262mA
```

| 字段 | 含义 | 单位和精度 |
|---|---|---|
| NPU_TEMP | 配置所选 NPU 卡报告的最高温度 | C，摄氏度，2 位小数 |
| NPU_POWER | hm_smi 报告的板卡功耗 | W，瓦，3 位小数 |
| RK_TEMP | RK3588 SoC 温度 | C，摄氏度，2 位小数 |
| VIN12_CURRENT | 配置的 12V 输入电流传感器读数 | mA，毫安，整数 |

例如 `1262mA = 1.262A`。数值已完成单位转换，接收端不要再次按旧协议缩放。
采集失败、尚未取得或过期的字段显示 `-`，不附单位，例如 `NPU_POWER=-`。
`0.000W` 和 `0mA` 是有效零值。

后台独立采集各传感器，查询立即读取缓存；默认缓存有效期 5 秒。
各字段采样时刻可能不同。NPU 功耗来自 hm_smi 的 Board_Power/Total_Power，
不是多卡求和；无法确定唯一功耗时显示 `-`。

兼容性：1.0.0 的回复是 `+STA:3010,4920,3515,1262`，2.0.0 起改为上述
带字段名和单位的格式，旧接收程序需调整。

## 4. AT+CPU：CPU 状态

发送 `AT+CPU` 或 `AT+CPU?`，回复示例：

```text
+CPU:USAGE=12.5%,IOWAIT=0.0%,LOAD1=1.00,LOAD5=0.50,LOAD15=0.20,CORE_COUNT=8,CORE_USAGE=0:0.0%|1:0.0%|2:0.0%|3:0.0%|4:0.0%|5:0.0%|6:0.0%|7:100.0%,FREQ=0-3:1296.0MHz|4-5:1608.0MHz|6-7:1608.0MHz,TEMP=37.00C,WINDOW_MS=1000
```

| 字段 | 含义 | 单位 / 格式 |
|---|---|---|
| USAGE | 整机 CPU 忙碌比例，按全部在线核心归一化 | 0～100%，1 位小数 |
| IOWAIT | 内核统计的 CPU I/O 等待比例 | %，1 位小数；不是磁盘使用率 |
| LOAD1 / LOAD5 / LOAD15 | 最近 1 / 5 / 15 分钟系统平均负载 | 无单位，2 位小数；不是百分比 |
| CORE_COUNT | 当前在线逻辑核心数 | 整数 |
| CORE_USAGE | 各核使用率 | `核编号:百分比`，用 `\|` 分隔；编号从 0 开始 |
| FREQ | 各调频组驱动报告的当前频率 | `核心组:频率MHz`，用 `\|` 分隔 |
| TEMP | RK3588 SoC 温度，与 STA 的 RK_TEMP 共用采集数据 | C，2 位小数 |
| WINDOW_MS | 使用率计算的实际采样间隔 | 毫秒 |

例如 8 核机器仅 1 核满载时，整机 USAGE 约为 12.5%。FREQ 的 `0-3` 表示
第 0～3 核共用调频组，不连续编号用 `+` 连接。频率不是采样窗口的平均频率。

后台约每秒采样一次，使用率取两次 CPU 计数差分；查询不等待新采样。
初次启动、采样中断或在线核心改变后，需重新积累两次有效样本。
暂不可用的值为 `-`，例如 `USAGE=-`、`0:-`、`FREQ=-`；有效空闲为 `0.0%`。
默认 CPU 缓存有效期 5 秒，温度单独过期。

## 5. AT+IP1 / AT+IP2：有线网络

```text
发送：AT+IP1
回复：+IP1:192.168.5.135,255.255.255.0

发送：AT+IP2?
回复：+IP2:192.168.6.135,255.255.255.0
```

顺序固定为 IPv4、子网掩码；没有可用 IPv4 或查询失败时分别返回
`+IP1:ERR`、`+IP2:ERR`。只返回主要可用 IPv4，不返回全部地址、网关或 DNS。
获得地址不代表已经验证能访问互联网。

IP1/IP2 按物理网卡硬件路径等信息自动排序，不根据当前连线或有无 IP 重新排序；
不保证等同于 Linux 的 eth0/eth1 或机箱面板标签。可用 `/etc/gs422.conf` 中的
`ethernet1`、`ethernet2` 指定接口。默认自动模式排除 Docker 等虚拟网卡。

## 6. AT+WIFI：查询和配网

查询 `AT+WIFI` 或 `AT+WIFI?`：

```text
+WIFI:LabWiFi,192.168.5.140,255.255.255.0
```

字段顺序为 SSID、IPv4、子网掩码。无已激活且具有可用 IPv4 的 Wi-Fi，
或查询失败时，回复 `+WIFI:ERR`。

配网示例（密码仅为示例）：

```text
发送：AT+WIFI=LabWiFi,ExamplePass123
成功：+WIFI:OK
失败：+WIFI:ERR
```

| 参数 | 规则 |
|---|---|
| SSID | 1～32 个 UTF-8 字节，不能包含控制字符；空格保留 |
| 密码 | 8～63 个可打印 ASCII 字符，或 64 位十六进制 PSK |
| 开放网络 | 密码留空，但保留逗号，例如 `AT+WIFI=GuestWiFi,` |
| 逗号 | 参数中用 `\,` 表示 |
| 反斜杠 | 参数中用 `\\` 表示 |

例如 SSID 为 `Lab,WiFi` 时发送 `AT+WIFI=Lab\,WiFi,ExamplePass123`。
查询回复中的 SSID 也采用这一反斜杠转义规则，不能直接用逗号简单拆分。
不支持企业认证、WEP 或仅 SAE 的 WPA3 网络。

程序通过 NetworkManager 执行连接、验证 SSID 和 IPv4，成功后保存连接配置；
失败尝试回滚。`OK` 不包含互联网连通性验证。配网会切换 Wi-Fi 无线开关，
影响所有 Wi-Fi 无线设备，可能中断 Wi-Fi 管理连接；操作期间查询可返回 ERR。
默认配网时限为 45 秒（`wifi_timeout`），同一时间只接受一个配网请求。

## 7. AT+DOCKER：Docker 信息和控制

### 7.1 Docker 状态

发送 `AT+DOCKER` 或 `AT+DOCKER?`：

```text
+DOCKER:STATE=READY,VERSION=29.1.3,CONTAINERS=2,RUNNING=0,PAUSED=0,STOPPED=2,IMAGES=3
```

| 字段 | 含义 |
|---|---|
| STATE | 成功访问 Engine 时为 READY，不代表所有容器或业务健康 |
| VERSION | Docker Engine 版本 |
| CONTAINERS | 容器总数 |
| RUNNING / PAUSED / STOPPED | Engine 报告的运行 / 暂停 / 停止容器数量 |
| IMAGES | Engine 报告的镜像数量 |

### 7.2 容器和镜像列表

```text
AT+DOCKER=LIST
AT+DOCKER=LIST,0
AT+DOCKER=IMAGES
AT+DOCKER=IMAGES,0
```

页码从 0 开始，为 1～6 位十进制数字，省略时为 0。每页一项，按完整 ID 排序。
下列 `<完整ID>` 是展示占位符，实际回复为完整 ID：

```text
+DOCKER:LIST,INDEX=0,TOTAL=2,ID=<完整ID>,NAME=my-worker,IMAGE=my-image:1.0,STATE=running,NEXT=1
+DOCKER:IMAGES,INDEX=0,TOTAL=1,ID=sha256:<完整ID>,TAG=my-image:1.0,TAG_COUNT=1,SIZE_BYTES=104857600,NEXT=-
```

| 字段 | 含义 |
|---|---|
| INDEX / TOTAL | 当前页码 / 条目总数 |
| ID | 容器完整 ID 或带 `sha256:` 前缀的镜像 ID |
| NAME / IMAGE / STATE | 容器名称 / 镜像引用 / Engine 状态，如 created、running、exited |
| TAG / TAG_COUNT | 镜像首个标签 / 标签总数；没有标签时 TAG=- |
| SIZE_BYTES | Engine 报告的镜像大小，字节；不是独占磁盘空间 |
| NEXT | 下一页页码；最后一页为 `-` |

列表为空或页码越界时回复例如 `+DOCKER:LIST,INDEX=2,TOTAL=2,END=1`。
按 NEXT 继续请求，直到 `NEXT=-` 或 `END=1`。每页都是一次独立读取，
其他程序增删对象时可能改变页码，应重新获取列表。

### 7.3 容器参数和文本编码

INFO、STATS、START、STOP、RESTART 使用相同的 `<容器>` 参数：
完整容器名（不带前导 `/`）或完整 64 位容器 ID，不接受短 ID。
参数长度为 1～128 个字符，首字符为英文字母或数字，其余可用英文字母、数字、
下划线、点、连字符。不能用镜像名代替容器名。

Docker 回复的名称、镜像引用等文本采用 UTF-8 百分号编码，例如逗号为 `%2C`、
百分号为 `%25`。先按逗号分字段，再解码字段值。文本最多保留原始 160 个字符，
完整 ID 不截断。较长名称应使用返回的 ID 作为后续操作参数。
此编码与 WIFI 的反斜杠转义不同。

### 7.4 容器详情

发送 `AT+DOCKER=INFO,my-worker`：

```text
+DOCKER:INFO,ID=<完整ID>,NAME=my-worker,IMAGE=my-image:1.0,STATE=running,HEALTH=NONE,EXIT_CODE=0,OOM_KILLED=0,RESTART_COUNT=0
```

| 字段 | 含义 |
|---|---|
| ID / NAME / IMAGE / STATE | 完整容器 ID / 名称 / 镜像引用 / 当前状态 |
| HEALTH | 健康检查状态；没有配置健康检查时为 NONE |
| EXIT_CODE | Engine 记录的退出码；应结合 STATE 判断 |
| OOM_KILLED | 是否被内存不足机制杀死，1 是、0 否 |
| RESTART_COUNT | Engine 记录的 RestartCount |

### 7.5 容器资源占用

发送 `AT+DOCKER=STATS,my-worker`：

```text
+DOCKER:STATS,ID=<完整ID>,CPU=25.0%,MEM_USAGE_BYTES=483328,MEM_LIMIT_BYTES=67108864
```

| 字段 | 含义 |
|---|---|
| CPU | 单核满载约 100%，可超过 100%；无有效差分时为 `-` |
| MEM_USAGE_BYTES | Engine 原始内存使用量，字节，含缓存 |
| MEM_LIMIT_BYTES | Engine 报告的内存限制，字节 |

这与 AT+CPU 的整机归一化百分比口径不同。内存占用也不同于 Docker CLI
扣除部分缓存后的显示值。内存字段不可用时为 `-`。容器未运行时返回
`+DOCKER:ERR,NOT_RUNNING`，统计查询可能需要等待采样。

### 7.6 启动、停止和重启

```text
AT+DOCKER=START,my-worker
AT+DOCKER=STOP,my-worker
AT+DOCKER=RESTART,my-worker
```

成功回复分别为：

```text
+DOCKER:START,ID=<完整ID>,OK
+DOCKER:STOP,ID=<完整ID>,OK
+DOCKER:RESTART,ID=<完整ID>,OK
```

仅允许控制白名单内的已有容器。`OK` 表示 Engine 操作完成，不保证业务健康或
容器持续运行，可接着 INFO 查询。暂停、重启中或 dead 的容器返回 CONFLICT。
STOP/RESTART 给容器 10 秒退出时间，之后 Engine 可能强制结束进程。
若返回 OUTCOME_UNKNOWN，操作可能已执行；先查询 INFO，再决定下一步，
不要直接自动重试控制命令。

### 7.7 控制白名单配置

在机器的 `/etc/gs422.conf` 设置以下键（已有键应修改，不要重复添加）：

```ini
docker_socket=/var/run/docker.sock
docker_allow=my-worker;another-worker
```

`docker_allow` 使用完整容器名或完整 ID，以分号分隔；默认为空，允许查询，
拒绝全部控制。名称授权会适用于以后同名重建的容器，完整 ID 则固定到具体实例。
修改后在机器的 SSH 终端执行以下命令生效，它不是串口 AT 命令：

```sh
sudo systemctl restart gs422
```

2026-11-06 部署验证时，135 的白名单为空；后续以现场配置为准。
本版本不提供创建、删除、exec、拉取镜像等命令。现有 4G/5G 包装脚本使用
`docker run --rm`，任务退出后容器删除，START 不能据此重新创建算法任务。

### 7.8 Docker 错误和超时

通常回复 `+DOCKER:ERR,<错误码>`；排队超时或内部 worker 异常也可能只返回
`+DOCKER:ERR`。

| 错误码 | 含义 / 处理方式 |
|---|---|
| BAD_COMMAND | 子命令、参数数量或格式错误 |
| BUSY | 请求容量已满；等当前请求结束后再发 |
| NOT_FOUND | 对象或 API 路径未找到；核对容器是否仍存在 |
| EXACT_TARGET_REQUIRED | 参数未精确匹配名称或完整 ID；不要使用短 ID |
| NOT_ALLOWED | 容器不在控制白名单中 |
| NOT_RUNNING | STATS 对象没有运行 |
| CONFLICT | 容器状态不允许操作，或 Engine 报告冲突 |
| DENIED | Engine 拒绝请求 |
| BAD_REQUEST | Engine 拒绝请求参数 |
| ENGINE_ERROR | Engine 返回其他错误状态 |
| API_VERSION | 客户端与 Engine 没有可用的共同 API 版本 |
| UNAVAILABLE | 无法连接 socket、通信超时或 HTTP 通信失败 |
| TIMEOUT | 查询子进程超过总时间预算 |
| INVALID_RESPONSE | 返回数据格式异常 |
| TOO_LARGE | Engine JSON 响应超过 2 MiB |
| OUTCOME_UNKNOWN | 控制结果不确定；先查 INFO，不自动重试 |

Docker 查询和控制分别处理，容量为 8 个查询和 1 个控制（包含正在执行的请求），
不阻塞 STA/CPU 查询。等待执行达到 5 秒的请求不再执行；查询和控制子进程
总预算分别为 10 秒和 22 秒，不包含排队和串口传输时间。
即使列表在串口侧分页，仍需读取 Engine 完整列表，受 2 MiB 响应限制。

## 8. 未实现的设计项

此前讨论过的 `AT+HOUMO`、`AT+IO`、`AT+SDR`、`AT+MEM`、`AT+DISK`、
`AT+PWR`、`AT+THERM`、`AT+UPTIME`、`AT+ID`、`AT+VER`、`AT+CAP`、
`AT+TIME` 尚未加入当前程序，发送会返回 `ERROR`。单独的 `AT`、`AT+HELP`
也不支持；当前版本没有通过 AT 命令获取完整帮助或服务版本的接口。
