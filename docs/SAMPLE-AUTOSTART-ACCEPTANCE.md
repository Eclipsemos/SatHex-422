# 统一样例服务常驻与整机重启验收

2026-11-20。主板已从仅安装但未启用的实验配置切换为统一样例服务常驻。外设继续使用用户确认的文件样例；软件/算法为sample-v1/v2及CPU求和样例。主分支未合并。

## 当前运行方式

- gs422-payload-lab 0.1.0~lab3：active/enabled，启动入口为/usr/bin/gs422-payload-lab。
- gs422-measurements-lab：active/enabled，提供真实RK温度/UTC与已确认的缺测码。
- Docker、SSH：active/enabled。
- 原gs422 2.6.0 AT服务：inactive/disabled，程序及配置保留。
- ttyS6控制和ttyS8只读上注由统一样例服务占用；电脑COM3应设115200/8N1，使用二进制帧，不能继续用9600/8N1 AT命令测试当前运行服务。
- 固定管理地址192.168.137.2，SSH用户名hzx保持可用。SSH正常关机命令仍为`sudo poweroff`。

lab3增加对Docker及测量服务的Requires/After依赖；ExecStartPre先生成一份完整新鲜测量，再启动接收进程。两服务Restart=on-failure，5秒间隔，避免启动竞态；错误配置仍不会伪造测量通过检查。升级保留既有可信包、F0、fixture_io_root和重启配置。

## 时间与重启证据

先检查RTC仍为2021年，与已校准系统时间不一致。按工具UTC时间源校准系统后执行hwclock --systohc --utc，读回RTC为当前2026-11-20。

启动统一样例服务后，经真实COM3发送K1109，TYPE15/16取回受理记录state=scheduled、delay_seconds=10。原boot_id为a2cc7b56-5f0e-484c-98db-a17f476910a9，重启后为8c890057-eca7-42c7-85dd-43e3ed0e2ba7，证明发生整机重启。

重启后无需进入开发目录手动运行，Docker、SSH、测量和统一载荷服务自动恢复，NRestarts均为0。UART日志READY及TELEMETRY_VALID=True，固定IP保持192.168.137.2/24。UTC保持当前日期和秒值，没有回到固件旧时间。此证据是一次正常重启，不是断电/RTC电池耐久或持续NTP同步验收。

## 连续物理查询

tools/probe_payload_soak.ps1在COM3发送120次TYPE11请求，间隔500ms，每次验证完整74B响应，随后Python独立验证头/长度/SUM/尾标志、序号和61B布局。持续约63秒，120/120通过；RK温度为真实有效值，后摩FF、电压/功耗FFFF保持明确无效；UTC与主机最大误差1秒；65个不同心跳值，符合1Hz推进。

主机完整往返中位数25.18125ms，最大93.4843ms，包含Windows/USB和整帧传输，不能作为“单机2ms内开始发送”的证明。服务无自动重启，样例软件/算法容器保持停止待指令状态；载荷管理器活跃不意味着算法正在运行。

## 回退方式

如需恢复此前AT测试方式，在SSH终端执行：

```sh
sudo systemctl disable --now gs422-payload-lab.service gs422-measurements-lab.service
sudo systemctl enable --now gs422.service
```

然后电脑串口改回9600/8N1。此处仅记录操作，本轮结束时仍保持新二进制样例服务运行和开机自启。

## 范围

本轮修改服务启动顺序及包版本，未改变Python运行逻辑，沿用164项单元测试。验证使用已安装服务、实际重启和120帧物理查询，不增加虚构单测数量。全部证据在goal-evidence-20261120：autostart-reboot.hex、autostart-before.json、autostart-soak.json、autostart-soak-summary.json、goal-autostart-after.log、goal-autostart-final.log。

物理UART8数据上传仍缺第二转换器；外部星敏/固存/下传为明确样例，后摩未装、功耗电压未核验，完整硬件及持续长时验收仍未完成。短时稳定运行不等于全目标已经达成。
