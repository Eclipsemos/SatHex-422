# SatHex-422：自主串口服务 gs422

[独立实验包](docs/LAB-PACKAGE-INSTALLATION.md)已在X26安装并验证，可与gs422 2.6.0共存；新服务默认禁用，外设驱动与可信软件目录尚未配置完整。

[分段流与32H进度查询](docs/SEGMENTED-PROGRESS-PROFILE.md)已完成项目定义和主板回归，162项测试通过；真实COM3进度查询通过，物理上传仍未完成。

[软件包索引验收](docs/PACKAGE-INDEX-ACCEPTANCE.md)：历史索引、初始F0恢复和重复请求保护已通过主板真实Docker测试，158项单元测试通过；现场catalog及全部硬件配置仍未完成。

优先查看[当前全目标验收清单](docs/CURRENT-ACCEPTANCE.md)：23条遥控与44项遥测逐项区分样例、物理证据和配置缺项。153项板上测试通过不等于所有现场配置完成。

[真实遥测与缺测编码](docs/MEASURED-TELEMETRY-ACCEPTANCE.md)：用户确认后摩温度对象及FF/FFFF无效码；新版74B帧已在真实COM3读回，151项板上测试通过。无效标记正确不代表未安装传感器已完成测量验收。

[后台服务配置与生命周期](docs/PAYLOAD-DAEMON-CONFIGURATION.md)已通过主板独立进程启停/续传/过期测量保护验证，147项单元测试通过；真实外设和测量源仍未全部配置。

[统一执行器样例验收](docs/UNIFIED-PAYLOAD-ACCEPTANCE.md)：主板144项单元测试通过，22项非重启遥控在同一实例执行业务成功。正式现场配置和真实外设验收仍未完成。

[软件持久化与候选清理](docs/DURABLE-SOFTWARE-ACCEPTANCE.md)已完成真实Docker进程中断恢复测试，主板140项单元测试通过；统一部署与全部硬件验收仍进行中。

[K1109整板重启](docs/K1109-REBOOT-ACCEPTANCE.md)已通过真实COM3验收，重启后IP、Docker、SSH、AT服务恢复；板上135项单元测试通过。全部业务配置目标仍未完成。

新增[旧D0351显式接收配置](docs/LEGACY-D0351-PROFILE.md)，主板131项单元测试及12项文件业务路径通过；收到文件不等于激活软件，全目标仍进行中。

[全指令验收推进](docs/GOAL-PROGRESS-20261120.md)：主板127项单元测试通过，新增11项文件业务/K表OTA执行路径验证；完整配置与物理外设验收仍在进行。

最新[Docker样例逐项验收](docs/DEMO-ACCEPTANCE-20261120.md)：板上116项单元测试通过，真实样例软件上注/v1→v2升级/失败回退、算法生命周期及物理COM3容器控制已验证。该后端为显式LAB配置，生产算法仍未注册，全表尚未完成。

用户明确上注更新对象为载荷软件。[软件主备目录与回退设计](docs/X26-OTA-STORAGE-DESIGN.md)已替代此前系统A/B提案，不升级Ubuntu、不重新分区；软件激活后端尚未实现。

20261119新遥控/遥测与OTA资料的[设计入口及待确认冲突](docs/REVISION-20261119.md)、
[44字段布局](docs/TMG-LAYOUT-20261119.md)、[来源清单](docs/spec-20261119.json)。
新增OTA编解码与暂存为离线参考组件，尚未接入现场UART；Flash后端与全功能实机验收尚未完成。
本次[阶段验收记录](docs/ACCEPTANCE-20261119.md)：Linux 112项测试通过，按用户确认的TYPE分流和CRC-16/CCITT-FALSE设计；[接口映射及双UART接入层](docs/X26-INTERFACE-MAPPING.md)使用ttyS6控制、ttyS8只收。未接入业务单独列出，整体功能验收尚未通过。

本开发分支包含试验星01二进制协议原型：[帧分配草案](docs/SATELLITE-FRAMES.md)、
[业务执行器接口与未接入范围](docs/SPACE-ADAPTERS.md)、[联调记录](docs/SPACE-VALIDATION.md)。
原型为独立入口，不默认切换现有AT服务；业务执行器尚待接入，不能作为完整载荷功能验收。
算法相关五项映射按用户要求留空；其余业务按[非算法业务设计](docs/PAYLOAD-SERVICES-DESIGN.md)分阶段实现。
X26现有Docker故障见[内核检查结果](docs/DOCKER-X26-DIAGNOSIS.md)。

独立实现 `shanhe-serial 0.2.2` 的串口功能：STA、IP1、IP2、WIFI 查询及配网。
另支持 `AT+CPU` / `AT+CPU?`：后台约 1 秒采样的整机/各核使用率、IO 等待、系统负载、
各核心组频率、SoC 温度、在线核心数和实际采样窗口。
Python 3.10+，Linux ARM64；不需要旧源码，不包含旧厂商代码。底层仍依赖系统驱动、
后摩 `hm_smi` 和 NetworkManager。支持 AT+SDR 后台采集/解码任务；当前不含 HOUMO、IO 等新指令。

串口测试直接查看 **[当前可测试的 AT 命令](docs/AT-COMMANDS.md)**。
详细协议及历史验证记录保存在 [docs/archive](docs/archive/)。

当前版本 **2.6.0**：SDR 默认使用 SatHex 原生 SC16 接收、实时线程和异步 NVMe 写盘。需先运行 SDR 部署工具的 `install-native`，并更新 Spaceborne 采集适配器。运行时缺失或不兼容时明确失败，不自动退回旧后端。已有机器配置应设置 `capture_backend=sc16-rt` 并删除旧 `capture_recv_frame_size/capture_cpus`；仅诊断时显式选择 `capture_backend=python`。AT 命令与采集参数范围保持不变，仍为先采后解。SDR 任务继续复用 LTE FFT 规划缓存。
保留 `AT+DISK` 存储列表、整盘容量、挂载位置及文件系统使用量。
保留有线静态 IPv4/DHCP 配置、确认保存/超时回滚，
以及 SSH 状态、启停、开机启动和端口设置。保留 Docker 状态、容器/镜像分页列表、详情、资源查询，
以及白名单内已有容器的启动、停止和重启。白名单默认留空，详见
[当前可测试的 AT 命令](docs/AT-COMMANDS.md)。保留 CPU 查询和 STA 名称及单位，示例：

```text
+STA:NPU_TEMP=30.10C,NPU_POWER=4.920W,RK_TEMP=35.15C,VIN12_CURRENT=1262mA
```

分别为 NPU 温度、NPU 功耗、RK3588 温度、12V 电流。C 表示摄氏度，缺失值为 `-`。
这与 1.0.0 的四个纯数字格式不兼容，接收端需同步调整；IP/WIFI 等回复不变。
传感器配置中的除数无需修改。

## 开发与测试

依赖：python3、python3-gi、gir1.2-glib-2.0、network-manager、systemd、iproute2、util-linux 2.37+。
SSH 管理需机器已安装 OpenSSH Server。
不需要 pip 或在线下载。Windows 可运行 core 测试，Linux 运行全部测试。

```sh
python3 -m unittest discover -s tests -v
python3 -m gs422.service --show-interfaces /etc/gs422.conf
python3 deploy/build_deb.py
```

`tests/compare_legacy.py` 在独立 PTY 上比较新旧程序，使用固定传感器数据和真实只读
网络查询，不占用物理 UART；须先安装原 shanhe-serial。STA 按新格式和相同物理量
核对，其他命令要求回复一致。

`tests/live_wifi.py --allow-wifi-interruption` 会断开并重连 Wi-Fi，只能在有线管理
连接上以 root 显式运行。复用现有连接凭据但不打印密码，结束时恢复原 profile 并清理
本次测试 profile，不随 unittest 自动运行。

`tests/live_admin.py` 在临时 veth 和网络命名空间中验证 IP 设置、DHCP、确认及回滚，
需要 root 和 dnsmasq；`tests/live_ssh_admin.py` 使用独立、仅监听回环地址的 SSH 服务。
`sudo python3 tests/live_storage.py --installed` 用安装后的程序及虚拟串口核对磁盘容量，
不占用物理 UART。该存储验收脚本针对 135 的 eMMC + NVMe 布局。
存储验收见 [2.4.0 验证记录](docs/archive/VALIDATION-2.4.0.md)，SDR任务验收及限制见 [2.5.0 验证记录](docs/archive/VALIDATION-2.5.0.md)。

## 配置

支持旧版配置键；必须核对板卡的路径和单位。135 已验证配置：

```ini
soc_temp_path=/sys/class/thermal/thermal_zone0/temp
soc_divisor=10
current_path=/sys/bus/i2c/devices/4-0040/iout
current_divisor=1
```

130 原电流接口为 `/sys/class/hwmon/hwmon8/curr1_input`，除数 1000，不能套用 135 的
除数。配置文件不存 Wi-Fi 密码。

## 安装与切换

生成 `dist/gs422_2.5.2_all.deb`，纯 Python 包标记为 all，可在 ARM64 运行。包安装
只注册服务，不自动占用现场串口。验证配置后切换：

```sh
sudo dpkg -i dist/gs422_2.5.2_all.deb
sudo cp /etc/shanhe-serial.conf /etc/gs422.conf
sudo systemctl disable --now rs422.service shanhe-serial.service
sudo systemctl enable --now gs422.service
sudo journalctl -u gs422.service -n 30 --no-pager
```

新服务与旧服务声明互斥，使用串口文件锁及 TIOCEXCL。旧安装包、程序和配置保留。
没有硬编码 ttyS6 的 systemd 条件，改 port 后重启服务即可。

从已安装的 gs422 升级时保留 `/etc/gs422.conf`，不要重新复制旧配置；安装后执行
`sudo systemctl restart gs422.service` 以加载新代码。

回退：

```sh
sudo systemctl disable --now gs422.service
sudo systemctl enable --now shanhe-serial.service
```

## 代码结构

- `gs422/core.py`：配置、协议、分帧、单位换算、缓存。
- `gs422/cpu.py`：CPU 计数差分、调频域、负载、缓存及只读采样子进程。
- `gs422/docker.py`：Docker Engine 本地 socket 查询、分页、容器控制白名单。
- `gs422/platform.py`：串口、网卡、IPv4、受控子进程。
- `gs422/network.py`：NetworkManager 查询、配网、持久化、回滚。
- `gs422/admin_protocol.py`：IP 与 SSH 管理命令参数校验。
- `gs422/ip_admin.py`：有线 IPv4 配置与 NetworkManager 自动回滚。
- `gs422/ssh_admin.py`：OpenSSH 状态、启停与端口配置。
- `gs422/storage.py`：只读磁盘及文件系统容量查询，分页输出；独立限时子进程。
- `gs422/service.py`：采集线程、查询队列、串口收发、生命周期。
- `deploy/`：离线打包及 systemd 服务。
- `tests/`：协议、缓存、并发、PTY、对照及现场配网测试。

还需实际 RS422 对端、整机重启及长期运行验收；PTY 不代替物理链路验收。

## SDR 任务服务（135）

需要已部署厂家 UHD、Spaceborne SatHex 工具、Docker 及 LTE/修复版 NR 镜像。包安装不会自动启用 SDR 服务；核对 `/etc/gs422-sdr.json` 中仓库、UHD、SSD 和镜像路径，以及 unit 的 User/Group 后执行：

```sh
sudo install -d -o hzx -g hzx -m 0750 /media/hzx/ssd/gs422-tasks
sudo systemctl enable --now gs422-sdr
sudo systemctl restart gs422
```

先确认 SSD 已挂载再创建目录。SDR 服务使用 hzx 身份与服务级 docker 附加组，不修改用户全局组；与主串口服务通过 `/run/gs422-sdr/control.sock` 通信。共享 `/tmp` 的按用户采集锁，避免与同用户手动采集重叠。其他不使用此锁的第三方采集程序仍需自行停止。

`gs422/sdr.py` 定义 AT 参数和短请求；`gs422/sdr_tasks.py` 管理单任务、落盘状态、限时进程和容器清理。需要取消时发送 STOP 并等待终态；不会自动重试失败任务。程序不删除任务数据。完整实机 PTY 验证用 `sudo python3 tests/live_sdr.py`，会真实采集 IQ、启动算法容器和重启任务服务，须在无其他任务时显式运行；不会占用生产 UART。
