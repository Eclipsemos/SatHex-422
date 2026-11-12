# gs422 2.5.0 SDR 任务验证

日期：2026-11-12，设备135，Ubuntu22.04 / RK3588 / ARM64。新增 AT+SDR；Spaceborne 使用 SatHex-dev `bb2ae945581f353db73956455329481f25cc107f` 与已有修复版 NR 镜像。

## 已验证

- 第一方 unittest **46项全部通过**：协议参数/注入拒绝、单任务互斥、参数与任务持久化、异常重启标记、清理失败阻止新任务、进程取消/超时/非零退出，以及失败部分结果不误报成功、未确认信号不误报成功。
- 包安装为2.5.0；物理串口服务 `gs422` 和后台任务服务 `gs422-sdr` 均 active/enabled。
- 使用安装后的程序、独立 PTY、与主串口相同的 ProtectHome/ProtectSystem/PrivateTmp/NoNewPrivileges 设置，完整发送 AT 命令。未打开生产 UART。
- 运行中 STA/CPU 可查询；重复 START、运行中 PRESET 被 BUSY 拒绝；错误参数被 BAD_COMMAND 拒绝。
- 启动阶段取消成功；解码阶段取消成功并确认对应 Docker 容器不存在。
- 解码阶段对后台任务服务实施一次 SIGKILL 故障注入：systemd 自动恢复，任务标为 INTERRUPTED/SERVICE_RESTART，遗留算法容器已清理；未自动重跑任务。
- 后台服务重启后，历史成功任务仍可按 ID 查询；参数配置持久化。

## 现场结果

| 任务 ID | AT 任务参数/操作 | 结果 |
|---|---|---|
| `a0289783a613` | NR，2130.15MHz，4MS/s，4MHz，500ms，45dB，channel1 | DONE，PCI690，10PBCH / 10MIB |
| `53d76fedb66c` | LTE广播预设，1815MHz，2MS/s，300ms，channel0 | DONE，PCI402，1个确认小区 |
| `9671ebddbcff` | LTE_DCI预设，20MS/s、120ms | 采集发生 RX overflow；FAILED/CAPTURE_FAILED，未继续解码不完整 IQ |
| `159415f9d567` | LTE任务解码阶段STOP | CANCELLED，容器清理确认 |
| `77bb8863f411` | LTE解码阶段杀死任务服务 | INTERRUPTED/SERVICE_RESTART，自动恢复服务并清理容器 |

另有 NR channel0 和 LTE 的采样正常结束但没有确认小区，返回 DONE/DETECTED=0；该状态不是算法识别成功。现场信号和采集吞吐会影响结果。对历史成功产物使用同一摘要函数核对，LTE DCI 正确报告PCI402、92条DCI，NR正确报告PCI690、9次PBCH/MIB；这是摘要回归，不作为本轮新采集的DCI成功证据。

首轮PTY测试发现后台服务的 ProtectSystem=strict 使共享锁 `/tmp/sathex-m2sdr-1001.lock` 不可写，已在专用任务服务中放行共享 `/tmp`。不使用 PrivateTmp，以保留与同用户手动采集进程的锁互斥。主422服务的权限设置未放宽。

完整三预设的PTY脚本在LTE_DCI溢出处失败，未把该次运行记为全通过。后续通过 `--resume-results` 对已成功任务补测取消、重启和故障恢复，得到 SDR_CONTROL_PTY_PASS；失败现场保留。当前只报告LTE/NR广播的实采成功及DCI错误处理正确，不宣称20MS/s稳定。

## 证据与复测

135：`/home/hzx/gs422-2.5.0-work/` 保存源码副本、测试日志、包与升级前备份。
`/media/hzx/ssd/gs422-tasks/<ID>/` 保存真实任务状态、IQ、manifest、解码日志与JSONL。

```sh
python3 -m unittest discover -s tests -v
# 以下显式测试会采样和启动/停止任务，需要没有用户任务时执行
sudo python3 tests/live_sdr.py
# 使用已有成功结果验证控制与故障恢复，不重做三套完整采集
sudo python3 tests/live_sdr.py --resume-results a0289783a613 53d76fedb66c --crash-recovery
```

实际对端的物理422收发、断电重启、长时间连续运行仍需现场验收。NR当前只有15kHz SSB广播；未验收NR DCI/SIB1。此功能不发射射频信号，不提供IQ串口传输、自动扫频或任务数据删除。
