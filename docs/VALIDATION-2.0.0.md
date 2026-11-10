# gs422 2.0.0：STA 名称与单位显示

135 设备记录时间：2026-11-06 14:53 CST。

## 变更

`AT+STA` 和 `AT+STA?` 改为带名称和单位的单行回复。温度由内部百分之一摄氏度转为
两位小数 C，功耗由 mW 转为三位小数 W，电流仍为整数 mA。ASCII C 表示摄氏度。
缺失/过期值显示为 `字段名=-`。采集路径、除数、缓存 TTL、其他指令不变。
由于串口格式与 1.0.0 的纯数字回复不兼容，软件版本升级为 2.0.0，协议修订为 2。

## 验证

- 本机 core 测试 8 项通过。
- ARM64 设备自动测试 13 项通过，包括新增显示精度、负温度和零电流测试，以及
  缓存过期、慢采集、突发请求和独占串口回归。
- 新旧程序 13 种请求对照通过：STA 校验相同物理量的新显示，其余指令回复一致。
- 已安装 2.0.0，正式服务启动后 SOC/CURRENT/NPU 四项采集有效。
- 安装后的程序在独立 PTY 上读取真实传感器，STA 与 STA? 均返回：

```text
+STA:NPU_TEMP=30.20C,NPU_POWER=4.930W,RK_TEMP=35.15C,VIN12_CURRENT=1262mA
```

- `/etc/gs422.conf` 升级前后 SHA256 一致，已备份到 `/var/backups/gs422`。
- `/dev/ttyS6` 仅由新服务占用；开机启动保持启用。
- 没有重做会中断网络的 Wi-Fi 配网测试，相关代码未修改。
- 物理串口对端的新显示仍待用户验证；没有进行整机重启。

安装包：`dist/gs422_2.0.0_all.deb`。
SHA256：`c0cd2069874fbb70983f148b8ffd41356d49ed5bf2f57d47ca70ab4d89d54e62`。

如需回退到自研旧格式（设备上保留 1.0.0 安装包）：

```sh
sudo systemctl stop gs422.service
sudo dpkg -i /home/hzx/gs422-source/dist/gs422_1.0.0_all.deb
sudo systemctl start gs422.service
```
