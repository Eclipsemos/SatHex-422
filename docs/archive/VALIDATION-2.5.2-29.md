# 29 的 20 MS/s LTE_DCI 接收修复（2.5.2）

机器：192.168.5.29；2026-11-16。使用本机编译的厂家 UHD 4.8.0.0、r25-dmafix2、Spaceborne dc55446。

## 定位

原 LTE_DCI 预设为 1815 MHz、20 MS/s、20 MHz 带宽、120 ms、channel 0、RX2、45 dB。
Python 采集先接收整段 FC32 到内存，停止 RX 后才编码并写 SSD；溢出发生在 recv 阶段，尚未写 IQ 文件。
因此本问题不能归因于 SSD 写入速度。

厂家默认帧为 3088 字节，每帧 768 个 SC16 样本，20 MS/s 约需 26042 包/秒。
16360 字节帧每帧 4086 样本，约 4895 包/秒，逐包处理频率降低约 5.3 倍。
这些值来自本机厂家源码的 B200 默认帧与接收帧参数处理，不改变驱动/FPGA。

| 配置 | 120 ms 重复测试 |
|---|---|
| 原默认小帧、默认 CPU 调度 | 0/3 通过，3 次 overflow |
| 16360 字节帧、默认 CPU 调度 | 0/3 通过，3 次 overflow |
| 默认小帧、绑定 CPU4–7 | 3/8 通过，其余 overflow/out-of-sequence |
| 16360 字节帧、绑定 CPU4–7 | 11/11 通过，每次准确 240 万样本 |

同样使用大帧和大核，延长到 1 秒的 3 次采集也全部通过，每次准确 2000 万样本、80 MB SC16。
每次通过均要求非零 IQ、完整样本数、连续时间戳、零 metadata 错误，并完成文件同步和 SHA256。
证据支持“小包处理成本与 CPU 调度余量共同限制接收”；未声称已精确定位厂家库中某一把锁。

## 实现与配置

新增两个可选的本机配置键，写入 `/etc/gs422-sdr.json`：

```json
{
  "capture_recv_frame_size": 16360,
  "capture_cpus": [4, 5, 6, 7]
}
```

以上片段需合并到原有配置，保留 socket、repo、mount、images 等字段。29 已部署并启用。
包默认配置不启用此配置，其他机器保持原行为，需按板卡验证后自行启用。
配置仅影响采集子进程和其线程；解码进程、CPU governor、全局优先级及生产串口线程不变。
未启用 RT 权限、sudo 采集或实验 LD_PRELOAD，也未绕过丢样检查。

帧大小目前只接受已验证的 16360；CPU 必须是非负、无重复的整数，并处于服务允许的 CPU 集合。
非法配置以 CAPTURE_CONFIG/CAPTURE_CPU_UNAVAILABLE 失败，不静默回落。
task.json 增加 capture_profile，capture/request.json 保存实际 device_args。
AT 命令及返回格式不变。升级后需重启 gs422-sdr 和 gs422，安装时保留现场配置。

```text
AT+SDR=PRESET,LTE_DCI
AT+SDR=START
AT+SDR=STATUS
AT+SDR=RESULT
```

恢复旧行为可删除这两个配置键并重启 gs422-sdr；原现场配置保存在 `/etc/gs422-sdr.json.before-debug20`。

## 证据与边界

- 对照记录：`/media/hzx/ssd/debug20-20261116/`、`debug20-followup-20261116/`、`debug20-one-second-20261116/`。
- [机器可读对照结果](debug20-29-results.json)。
- 正式 AT 测试：`~/live-sdr29-debug20-v252.log`。
- LTE_DCI 任务 `59340741e622`：准确 240 万样本，metadata_errors=0，continuity_checked=true，最终 DONE。
- NR `cc80ebdf4dec`、LTE 广播 `010df24026d7`、LTE_DCI `59340741e622` 三个正式任务全部 DONE，均 DETECTED=0/DCI=0；未宣称解出有效基站内容。
- 完整 `SDR_INSTALLED_PTY_PASS`，包括忙碌保护、启动/解码阶段取消、重启后查询三个历史结果；真实 RS422 对端未参与。
- 47 项 422 单元测试通过，新增配置校验、CPU 不可用及旧配置兼容检查。

升级过程中 dpkg 等待保留现场配置的交互确认，第一次 AT 回归仍连接旧进程并复现原失败；
保留配置、完成安装并核对新 PID/2.5.2 后重跑，分别保留日志，没有把旧进程的测试算作新版结果。
本轮是 LTE_DCI 短任务修复及有限次 1 秒测试，不代表 20 MS/s 长时间采集、CPU 满载或更高采样率已验收。
