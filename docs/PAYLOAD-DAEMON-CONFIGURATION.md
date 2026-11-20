# 统一载荷后台服务配置与生命周期验证

2026-11-20。gs422.payload_service提供独立长运行入口，模式明确为lab。它组合DurableDemo、FileServices、LegacyUpload、Staging、Revised及DualUART，可选绑定BoardReboot；不自动启用，不覆盖已部署AT服务。

后续已制作并安装独立gs422-payload-lab 0.1.0~lab1，路径/usr/lib/gs422-payload-lab、入口/usr/bin/gs422-payload-lab，默认保持disabled。当前安装与前置条件见LAB-PACKAGE-INSTALLATION.md，下文早期手动源码入口仅供历史参考。

后续用户确认传感器无效码，已新增gs422.measurements真实快照生产者及物理COM3测试，详见MEASURED-TELEMETRY-ACCEPTANCE.md。单项缺测可用明确无效码发布完整帧；整份快照缺失或过期仍按下文抑制遥测。

## 配置与检查

参考config/gs422-payload-lab.example.json和deploy/gs422-payload-lab.service。完整配置必须包括两个不同UART绝对路径、互不嵌套的状态/记录目录、测量快照路径和最大年龄、显式fixture许可、本地可信镜像目录、OTA容量和重启开关。未知字段、路径重叠、伪布尔值、无界年龄、非不可变Docker image ID均拒绝。

```sh
python3 -m gs422.payload_service --config /path/to/config.json --check-config
python3 -m gs422.payload_service --config /path/to/config.json
```

check-config只确认配置语法并报告未绑定硬件，不宣称系统已可联试。默认样例目录为空、reboot=false、allow_fixture_telemetry=false。现场需先明确可信软件包目录和真实测量生产者，不能把例子中的空目录理解为软件部署成功。

systemd模板WorkingDirectory=/usr/lib/gs422，需先将本分支代码正确安装到对应目录，配置保存到/etc/gs422-payload-lab.json，再手动启动。模板与AT、旧RS422及旧实验服务互斥；未安装到当前主板启动项。不要在当前2.6.0包上直接启用此模板，因为原正式包不含新入口。

## 测量契约

独立生产者应以临时文件+原子替换发布JSON：

```json
{
  "source": "measured",
  "monotonic": 12345.0,
  "values": {
    "TMG004": 1795161600,
    "TMG005": 1234,
    "TMG006": 45,
    "TMG007": 46,
    "TMG034": 1250
  }
}
```

以上仅格式示例，不是本板测量。值为协议原码：功率0.01W、电压0.01V、温度整摄氏度；UTC须同步。monotonic是本机同次启动的单调时钟，不可用地面时钟或mtime代替。必须完整提供这五项，禁止静默补0。状态计数、算法和文件遥测由实际后端补充，保留字段才可固定0。

source=fixture只在显式allow_fixture_telemetry=true时接受，启动日志会打印该标志，生产部署不得伪装为measured。五项数据缺失、类型/范围错误、时间在未来或超过最大年龄都会失效。请求路径独立检查deadline；即使后台采集被慢操作阻塞，也不能继续发送过期61B遥测。当前协议没有定义缺测哨兵，因此失效时抑制TYPE12回复，控制版本查询继续可用；这个临时策略仍须与星务通信故障恢复策略协调，不等于完整缺测协议已定版。

## 生命周期与测试

服务独占两路UART，上注描述符只读；SIGTERM停止收发、退出采集和执行workers、关闭文件/数据库并释放锁，已部署的软件容器不因管理服务退出而被删除。初始快照不合格时在创建状态和打开串口前拒绝启动。

主板完整147项单元测试通过，8.524秒。新增3项验证配置约束、显式fixture权限/时效/缺字段，以及遥测失效时控制仍可工作。

tests/live_payload_service.py在主板启动真实独立Python后台进程和两个PTY，明确使用fixture测量文件。首先接收700B文件的第一包，正常停止进程并重新启动；接收计数仍为1，再发送余包并CRC成功。暂停测量生产者后TYPE12无回复但TYPE30版本查询可用，恢复生产者后遥测恢复。上注口无应答，正常退出码0，测试目录清理完成。

本机日志goal-evidence-20261120/goal-service-unit.log和goal-service-live.log；主板源码/home/hzx/goal-service-20261120。正式gs422、Docker和SSH保持运行，未修改现有启动配置。

## 尚未配置成功的项目

这不是“全表正式部署完成”。FileServices在此通用入口没有真实source/sink/capture驱动绑定，相关请求会拒绝；之前22项统一执行成功来自显式配置测试脚本，不应混同此默认服务。可信镜像目录默认空；实际物理传感器快照生产者未接入；数据缺测协议未最终定义；UART8物理输入缺第二转换器。需补齐并逐项验收后才能启用长期现场配置，不能以本轮启动/恢复测试替代这些缺口。
