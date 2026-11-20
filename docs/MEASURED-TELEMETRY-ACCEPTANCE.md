# 真实测量与无效值遥测验收

2026-11-20。用户明确：TMG007继续代表后摩算力卡温度，当前未安装，不能替换成RK3588内置NPU热区。用户同时确认以下缺测编码。

| 字段 | 有效量 | 无效原码 | 接收端解释 |
|---|---|---|---|
| TMG005 | 功率0.01W | FFFF | 未测得/未配置，不显示655.35W |
| TMG006 | RK温度，整°C | FF | 无效，不显示255°C |
| TMG007 | 后摩卡温度，整°C | FF | 当前未安装/无效，不使用RK内置NPU替代 |
| TMG034 | 电压0.01V | FFFF | 未测得/未配置，不显示655.35V |

这意味着最高原码被保留，物理温度最大可编码254°C、电压/功率最大655.34对应单位。raw编码器仍允许无效原码；工程量转换函数拒绝把真实数值量化成无效标记。tmg.physical_value将这些标记解码为None。原始Excel未修改，应将该协议补充同步给接收端。

## 新增测量生产者

gs422.measurements读取配置指定的soc-thermal/temp，按Linux thermal毫摄氏度转换；执行指定hm_smi读取后摩卡，未检测到设备、超时或无有效读数均为FF。绝不读取npu-thermal冒充后摩卡。电压与功率需要显式path、divisor和verified=true，否则为FFFF。

板上is6105a存在vin/vout/iout，实读曾为12562/12656/825；但无法从已提供的驱动说明核实缩放和具体电源测点。芯片厂商说明支持VIN/VOUT/IOUT及功率监测，并不能证明这块板当前sysfs值的单位或整机测量覆盖范围，因此本次没有把VIN×IOUT或标称12V乘电流填入整机功耗。

参考：[IS6105A厂商产品说明](https://www.innovisionsemi.com/en/product/IS6105A)。后续需驱动源码、板级电源测点说明或仪表对照，才能把电压/功耗从无效切换为已核验有效。

生产者先要求utc_verified=true，发布带同机monotonic时间的完整测量快照，采用临时文件+原子替换。模板utc_verified=false避免错误时钟直接被接受。当前仅配置项表示操作员已校时，不具备自动时间同步状态核验；部署时需另行提供持续校时/失同步处理。

## 主板与物理链路结果

1. 主板原时钟停留固件旧日期，按当前工具UTC时间源进行了本次系统校时；没有宣称RTC已经写入或NTP已经同步。
2. 主板hm_smi返回No match pci device，符合用户未安装后摩卡的情况。
3. 新生产者实测快照source=measured，allow_fixture_telemetry=false，包含RK真实温度、UTC及明确无效传感器标记。
4. 短时运行两个systemd transient服务：测量生产者120秒上限，统一载荷服务90秒上限；控制ttyS6和只读上注ttyS8均成功打开。正式AT服务在测试期间停止，结束自动恢复。
5. Windows通过真实COM3/115200/8N1发送TYPE11遥测请求，收到74B完整帧。独立Decoder核验头、长度、累加和、尾标志及61B布局；TMG006=52°C，TMG007=FF，TMG005/TMG034=FFFF，UTC与主机差小于10秒。
6. 已主动停止两项实验服务，正式gs422、Docker、SSH均active。不存在持续发布假测量的测试进程，也未设置新的自启服务。

完整151项主板单元测试通过，7.266秒。新增测试覆盖缺后摩不替代、无效值解码、工程量边界、显式传感器缩放、缺失/异常数据、未校时拒绝及原子快照写入。

本机原始证据：goal-evidence-20261120/measured-frame.hex、measured-result.json、measurement-config.json、measured-service-config.json、goal-measured-unit.log。源目录/home/hzx/goal-measured-20261120。

## 完成边界

已证明当前缺卡时仍能返回完整遥测帧并明确标无效，以及真实RK温度和本次UTC传输。没有证明后摩传感器实际测量、电压/功耗准确性、连续时间同步、物理UART8收包、真实星敏/固存/下传外设。这些仍是全目标的未完成项，不能以无效值编码正确代替传感器测量成功。
