# 当前可测试的 AT 命令

适用版本：gs422 2.4.0。串口设置 **9600 / 8N1 / 无流控**，发送时加 **CRLF**。
命令使用大写，一次发一条，收到回复后再发下一条。

## 直接查询

以下命令可以直接复制到串口调试工具：

| 命令 | 查看什么 |
|---|---|
| `AT+STA` | NPU 温度（C）、NPU 功耗（W）、RK 温度（C）、12V 电流（mA） |
| `AT+CPU` | CPU 总使用率、各核使用率、IO 等待、1/5/15 分钟负载、核心数、频率、温度 |
| `AT+DISK` / `AT+DISK?` | 磁盘列表、容量及各文件系统的挂载位置、总量、已用、可用、使用率 |
| `AT+DISK=1` | 查询存储列表第 1 页（第一页为 0） |
| `AT+IP1` | 第一物理网口的 IPv4 和子网掩码 |
| `AT+IP2` | 第二物理网口的 IPv4 和子网掩码 |
| `AT+WIFI` | 当前 Wi-Fi 名称、IPv4 和子网掩码 |
| `AT+IP1=CONFIG` / `AT+IP2=CONFIG` | 网口名、DHCP/静态配置、网关、DNS、实际地址及待确认状态 |
| `AT+SSH` / `AT+SSH?` | SSH 状态、开机启动状态、配置端口、socket 激活状态 |
| `AT+DOCKER` | Docker 版本、容器数量、运行数量和镜像数量 |
| `AT+DOCKER=LIST,0` | 第 0 页容器：名称、完整 ID、镜像和状态 |
| `AT+DOCKER=IMAGES,0` | 第 0 页镜像：标签、ID 和大小（字节） |

Docker 列表每页一项。回复 `NEXT=1` 就把页码改成 `1` 继续查；`NEXT=-` 或 `END=1` 表示结束。
`AT+DOCKER=LIST`、`AT+DOCKER=IMAGES` 省略页码时也从第 0 页开始。
STA、CPU、IP1、IP2、WIFI、DOCKER 基本查询都支持末尾加 `?`，例如 `AT+CPU?`。

STA 回复示例：

```text
+STA:NPU_TEMP=30.10C,NPU_POWER=4.920W,RK_TEMP=35.15C,VIN12_CURRENT=1262mA
```

`-` 表示数据暂不可用；`1262mA` 等于 `1.262A`。CPU 的 LOAD1/5/15 是负载，不是百分比。

## 查看存储情况

直接发送 `AT+DISK`。135 的 eMMC 与 NVMe SSD 会在同一页显示，多行回复以
`+DISK:END,NEXT=-` 结束。若 NEXT 是数字，例如 `1`，继续发送 `AT+DISK=1`。
每页最多 8 条明细，长路径较多时会减少每页条数；每次查询读取当前状态。

| 字段 | 含义 |
|---|---|
| COUNT / PAGE / PAGES / ROWS | 磁盘数、当前页、总页数、本页明细条数 |
| DEVICE / MODEL / SIZE / SIZE_BYTES | 整块盘设备名、型号、容量（GiB）、容量（字节） |
| PARTITIONS | 直接分区数量，包括没有文件系统的启动分区 |
| VOLUME / DISKS / FS / MOUNT | 文件系统所在设备、所属盘、文件系统类型、挂载点 |
| STATE | MOUNTED 已挂载；UNMOUNTED 未挂载；UNAVAILABLE 使用量暂不可读 |
| TOTAL / USED / AVAILABLE / USE | 文件系统总量、已用、普通用户可用空间、使用率 |

容量统一使用 **GiB（1 GiB = 1024³ 字节）**，保留两位小数。整盘容量与文件系统容量不同。
USED 不包含文件系统保留空闲块，因此 USED + AVAILABLE 可能小于 TOTAL；
USE 按 USED / (USED + AVAILABLE) 计算。未挂载或读不到的使用量显示 `-`，不表示空盘。
同一文件系统多次挂载仅统计一次，显示一个可读取的挂载点。
型号、路径的空格和特殊字符使用百分号编码，例如空格为 `%20`。

只显示整盘及有文件系统或挂载记录的卷；跳过 loop、zram、内存盘和 eMMC boot/rpmb 辅助设备。
没有文件系统的分区只计入 PARTITIONS。此命令只读，不挂载、不格式化，也不检测硬盘健康。

135 的回复示例（2026-11-11，使用量会变化）：

```text
+DISK:COUNT=2,PAGE=0,PAGES=1,ROWS=5
+DISK:DEVICE=/dev/mmcblk0,MODEL=-,SIZE=116.48GiB,SIZE_BYTES=125074145280,PARTITIONS=7
+DISK:DEVICE=/dev/nvme0n1,MODEL=K-RICARD%20NVME%20SSD%20256GB,SIZE=238.47GiB,SIZE_BYTES=256060514304,PARTITIONS=1
+DISK:VOLUME=/dev/mmcblk0p6,DISKS=mmcblk0,FS=ext4,MOUNT=-,STATE=UNMOUNTED,TOTAL=-,USED=-,AVAILABLE=-,USE=-
+DISK:VOLUME=/dev/mmcblk0p7,DISKS=mmcblk0,FS=ext4,MOUNT=/,STATE=MOUNTED,TOTAL=114.12GiB,USED=21.03GiB,AVAILABLE=88.25GiB,USE=19.2%
+DISK:VOLUME=/dev/nvme0n1p1,DISKS=nvme0n1,FS=ext4,MOUNT=/media/hzx/ssd,STATE=MOUNTED,TOTAL=234.39GiB,USED=58.55GiB,AVAILABLE=163.92GiB,USE=26.3%
+DISK:END,NEXT=-
```

## 查询指定容器

先用 LIST 找到容器名，再把下面的 `my-worker` 替换为实际名称，也可使用完整容器 ID（不支持短 ID）：

| 命令 | 查看什么 |
|---|---|
| `AT+DOCKER=INFO,my-worker` | 状态、健康检查、退出码、是否内存不足退出 |
| `AT+DOCKER=STATS,my-worker` | CPU 使用率、内存使用量和限制（字节） |

STATS 需要容器正在运行，否则返回 `+DOCKER:ERR,NOT_RUNNING`。
容器 CPU 使用率按单核 100% 计算，可超过 100%；AT+CPU 的整机使用率范围为 0～100%。

## 会修改状态的命令

### 手动设置有线 IP

135 当前映射：**IP1=enP4p65s0，IP2=eth0**。当前 SSH 连接使用 IP2 的 `192.168.5.135`。
先用 CONFIG 核对网口，IP1 和 IP2 均支持以下操作：

表中 `IP2` 可全部替换为 `IP1`；确认和回滚必须使用本次设置的同一个网口。

| 命令（以 IP2 为例） | 操作 |
|---|---|
| `AT+IP2=STATIC,192.168.5.135,255.255.255.0,192.168.5.1,1.1.1.1;8.8.8.8` | 设置 IPv4、掩码、网关、DNS；按实际网络替换参数 |
| `AT+IP2=DHCP` | 恢复自动获取 IPv4、网关和 DNS |
| `AT+IP2=CONFIRM` | 确认本次配置并永久保存，重启后继续使用 |
| `AT+IP2=ROLLBACK` | 取消本次配置，恢复原配置 |

操作顺序：发送 STATIC 或 DHCP → 收到 `PENDING` → 检查新网络 → 在回复的
`CONFIRM_SEC` 秒内发送同一网口的 CONFIRM。窗口约 **120 秒**，不确认自动回滚。
已连接网口会立即试用配置，SSH 可能断开；通过 422 确认不依赖网络。
IP 配置失败回复 ERR，不要继续发送 CONFIRM。

未连接网口返回 `APPLIED=0`，仅暂存配置；CONFIRM 保存后，接线时自动连接。
无网关或 DNS 填 `-`，多个 DNS 用分号，最多 3 个。掩码使用点分十进制，支持 /1～/30；
不修改 IPv6。一次只能有一个 IP 配置事务，期间 Wi-Fi 配网和另一个网口配置返回 BUSY。
CONFIG 中 ADDRESS/MASK/GATEWAY/DNS 表示配置（有待确认操作时显示待确认值）；
LIVE_IP 表示实际地址。DHCP 模式配置地址显示 `-` 属正常。

### SSH 管理

| 命令 | 操作 |
|---|---|
| `AT+SSH=ON` | 开启 SSH 并设置开机启动 |
| `AT+SSH=OFF` | 停止 SSH 监听并取消开机启动 |
| `AT+SSH=PORT,2222` | 设置 SSH 端口并保存；正在运行时重新加载 |
| `AT+SSH=PORT,22` | 恢复默认端口 |

修改端口立即生效，没有 IP 配置的确认步骤。以后连接例如：
`ssh -p 2222 hzx@192.168.5.135`。OFF 不强制结束已有会话，但新连接会被拒绝。
端口冲突、配置校验失败时保留原配置。仅管理已安装的 OpenSSH `ssh.service`；
socket 激活模式暂不支持修改。当前不修改用户、密码、密钥或防火墙规则。

### Wi-Fi 和 Docker

| 命令 | 操作 |
|---|---|
| `AT+WIFI=你的SSID,你的密码` | 连接 Wi-Fi，成功后保存；可能中断原 Wi-Fi 连接 |
| `AT+DOCKER=START,my-worker` | 启动已有容器 |
| `AT+DOCKER=STOP,my-worker` | 停止容器 |
| `AT+DOCKER=RESTART,my-worker` | 重启容器 |

Wi-Fi 成功回复 `+WIFI:OK`，失败回复 `+WIFI:ERR`。开放网络保留末尾逗号，例如
`AT+WIFI=GuestWiFi,`。SSID 或密码中的逗号写成 `\,`，反斜杠写成 `\\`。

Docker 控制成功回复例如 `+DOCKER:START,ID=完整ID,OK`。
**控制默认禁用**。需要在机器 `/etc/gs422.conf` 中设置允许控制的实际容器名：

```ini
docker_allow=my-worker;another-worker
```

修改已有配置项后，在 SSH 终端执行 `sudo systemctl restart gs422` 生效。
只支持已有容器的启停；不支持创建、删除、拉取镜像或执行容器内命令。

## 192.168.5.28 历史测试注意事项

以下为 2026-11-10 实测状态，接线或硬件变化后以实时回复为准：

- `AT+IP2` 返回 `192.168.5.28`；IP1 网口未连接，返回 `+IP1:ERR`。
- Wi-Fi 未连接，`AT+WIFI` 返回 `+WIFI:ERR`。
- RK 温度和电流可读；后摩 NPU 未识别，STA 中 NPU 温度、功耗显示 `-`。
- Docker 可查询，有一个已停止的 `gallant_ramanujan` 容器。
  可测试 `AT+DOCKER=INFO,gallant_ramanujan`；STATS 返回 NOT_RUNNING 属正常结果。
- Docker 控制白名单为空，START/STOP/RESTART 返回 NOT_ALLOWED。

## 常见回复

| 回复 | 含义 |
|---|---|
| `ERROR` | 命令不支持或格式错误 |
| `+DISK:ERR,BAD_COMMAND` | 存储命令格式错误，页码须为 0～999 |
| `+DISK:ERR,NO_PAGE` | 页码超出当前列表范围 |
| `+DISK:ERR,BUSY` | 正在处理另一条存储查询，稍后重试 |
| `+DISK:ERR,TIMEOUT` / `UNAVAILABLE` | 存储查询超时或暂不可用 |
| `+IP1:ERR` / `+IP2:ERR` / `+WIFI:ERR` | 无可用网络地址，或查询/配网失败 |
| `+IP2:PENDING,...` | 配置等待确认；按 CONFIRM_SEC 时限确认或回滚 |
| `+IP2:OK,SAVED,APPLIED=1` | 已保存且已应用；APPLIED=0 表示待接线生效 |
| `+IP2:ERR,BAD_COMMAND` | IP、掩码、网关或 DNS 格式不合法 |
| `+IP2:ERR,NO_PENDING` / `EXPIRED` | 没有此网口的待确认配置，或已超时 |
| `+IP2:ERR,BUSY` / `NOT_READY` | 另一配置待确认，或网卡正在切换；稍后再试 |
| `+IP2:ERR,ROLLBACK_PENDING` / `ROLLBACK_FAILED` | 恢复尚未完成或失败，查询 CONFIG 与 IP 确认状态 |
| `+SSH:ERR,PORT_IN_USE` | 端口已被占用 |
| `+SSH:ERR,CONFIG_CONFLICT` | 其他 SSH 配置阻止端口修改，已恢复原文件 |
| `+SSH:ERR,SOCKET_ACTIVATION` | 当前 SSH 使用 socket 激活，本版不支持修改 |
| `+SSH:ERR,NOT_INSTALLED` | 未找到可管理的 ssh.service |
| `+DOCKER:ERR,NOT_ALLOWED` | 容器未加入控制白名单 |
| `+DOCKER:ERR,NOT_FOUND` | 容器不存在，检查名称 |
| `+DOCKER:ERR,NOT_RUNNING` | 容器未运行，不能查询资源占用 |
| `+DOCKER:ERR,BAD_COMMAND` | Docker 子命令或参数错误 |
| `+DOCKER:ERR,BUSY` | 正在处理其他请求，稍后再试 |
| `+DOCKER:ERR,OUTCOME_UNKNOWN` | 控制结果不确定，先查 INFO，不要直接重复启停 |

HOUMO、IO、SDR、MEM、PWR、THERM、UPTIME、ID、VER、CAP、TIME、HELP
尚未实现。当前全部可测试命令已列在上面。

详细字段说明、协议和历史验证记录保存在 [archive](archive/)，日常测试看本页即可。
