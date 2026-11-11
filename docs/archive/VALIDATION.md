# gs422 1.0.0 验证与部署记录

本文件为 1.0.0 的历史记录。2.0.0 已改变 STA 格式，后续记录见
[VALIDATION-2.0.0.md](VALIDATION-2.0.0.md)。

测试设备：192.168.5.135，Ubuntu 22.04.5 LTS，ARM64，Linux 5.10.198。
设备记录日期：2026-11-06。测试通过有线 SSH 执行。

## 已完成

- Python unittest：12 项通过。包括输入分帧、CRLF、超长恢复、UTF-8/参数验证、
  单位换算、独立缓存 TTL、失效不续期、真实零值、网卡排序/覆盖、子进程超时/输出上限、
  慢 NPU 下的 PTY 查询、40 条突发 STA、缓存失效恢复、串口独占、并发和队列过期。
- 与 `/usr/sbin/shanhe-serial` 0.2.2 的 PTY 对照：13 种请求全部一致。源码版和
  已安装 `/usr/bin/gs422` 分别完成对照。传感器使用固定样本，IP/Wi-Fi 使用实际只读查询。
- 固定样本 STA：两版均返回 `+STA:3010,4920,3515,1262`。
- 实际 Wi-Fi：off/on 后连接当前 SSID，核验 IPv4，再 Save；确认 profile 已持久化。
- 实际 Wi-Fi 失败：不存在的 SSID 超时返回 ERR；第二条在途设置立即 ERR；checkpoint
  回滚后恢复原 Wi-Fi。测试结束恢复原 profile，并删除测试创建的 gs422 profile。
- 配网时 STA/IP2 两条回复总等待约 19 ms（PTY 条件，不能视为 9600 波特物理时延）。
- 检查测试服务日志未包含 Wi-Fi 密码；测试本身不将凭据写入 argv 或文件。
- systemd 单元检查通过。系统中其他已有单元有权限/未知字段警告，未修改这些无关单元。
- 已安装 gs422 1.0.0；gs422 active/enabled；shanhe-serial 和 rs422 inactive/disabled。
- 现场配置已复制到 `/etc/gs422.conf`：SoC 除数 10，135 的 iout 电流除数 1。
- 正式服务日志确认 SOC、CURRENT、NPU 温度及功耗全部采样有效。
- 部署后一次检查：NRestarts=0，MemoryCurrent=9166848 bytes（瞬时值）。

## 交付

- 本机源码：`D:\workspace\感算单元\dev\422`。
- 设备源码：`/home/hzx/gs422-source`。
- 安装包：`dist/gs422_1.0.0_all.deb`，纯 Python，兼容 ARM64。
- SHA256：`9d72a00d8e08fe77c357a85f5f0c7f291dc63e729494d90048795bdd17465989`。
- 程序：`/usr/bin/gs422`；实现：`/usr/lib/gs422/gs422`。
- 配置：`/etc/gs422.conf`；服务：`gs422.service`。
- 原程序、包和配置保留，配置备份在 `/var/backups/gs422`。

## 验收边界

尚未验证外部设备经物理 RS422 收到新程序回复、整机重启后工作、断电场景、长期运行、
多 NPU、多无线卡、所有密码/AP 组合。当前单卡工具输出格式已验证。
未添加用户规划中的 HOUMO、SDR 等扩展指令；下一步可在自主源码上开发。
所有测试结论均指上述样本与设备，不代表对任意流量或故障情形的保证。

现场建议发送 `AT+STA`、`AT+IP1`、`AT+IP2`、`AT+WIFI` 各一次，确认每条只回复一次，
STA 的温度仍是百分之一摄氏度，功耗仍是 mW。Wi-Fi 设置测试会影响无线连接，需有线管理。

回退：

```sh
sudo systemctl disable --now gs422.service
sudo systemctl enable --now shanhe-serial.service
```
