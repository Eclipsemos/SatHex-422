# SatHex-422：自主串口服务 gs422

独立实现 `shanhe-serial 0.2.2` 的串口功能：STA、IP1、IP2、WIFI 查询及配网。
另支持 `AT+CPU` / `AT+CPU?`：后台约 1 秒采样的整机/各核使用率、IO 等待、系统负载、
各核心组频率、SoC 温度、在线核心数和实际采样窗口。
Python 3.10+，Linux ARM64；不需要旧源码，不包含旧厂商代码。底层仍依赖系统驱动、
后摩 `hm_smi` 和 NetworkManager。当前不含 HOUMO、SDR 等新指令。

串口测试直接查看 **[当前可测试的 AT 命令](docs/AT-COMMANDS.md)**。
详细协议及历史验证记录保存在 [docs/archive](docs/archive/)。

当前版本 **2.4.0**：新增 `AT+DISK` 存储列表、整盘容量、挂载位置及文件系统使用量。
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
已完成的验证及范围见 [2.4.0 验证记录](docs/archive/VALIDATION-2.4.0.md)。

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

生成 `dist/gs422_2.4.0_all.deb`，纯 Python 包标记为 all，可在 ARM64 运行。包安装
只注册服务，不自动占用现场串口。验证配置后切换：

```sh
sudo dpkg -i dist/gs422_2.4.0_all.deb
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
