# 已安装统一样例配置与物理控制验收

后续状态更新：lab3已设置常驻和开机自启，原AT停用；详见SAMPLE-AUTOSTART-ACCEPTANCE.md。下文描述此前测试后停用阶段。

2026-11-20。用户确认星敏、固存及下传外设尚未接入，目前只能做软件样例测试；软件和算法亦已授权使用样例。本记录明确样例层级，不将文件驱动称为真实外设。

## 当前主板保存的配置

- 独立gs422-payload-lab已升级至0.1.0~lab2，与正式gs422 2.6.0共存。
- /etc/gs422-payload-lab.json含2个本机编译、离线导入的可信软件归档及不可变Docker image ID。
- 软件版本sample-v1已健康检查通过并作为持久基线；sample-v2包已注册。F0初始索引绑定sample-v1并经过新管理实例核验。
- 软件容器cba98734bf8d4ddfe4a671ebde45d08ab4e2f0c633a909845f469166403bb8322及样例算法容器保留但已停止。后续测试可复用，无需每次临时创建目录。
- 样例镜像标签sathex-reference-provision-4028dd1d9e:sample-v1及sample-v2保留；没有删除原hello-world镜像或gallant_ramanujan容器。
- fixture_io_root=/var/lib/sathex-payload-lab/fixture-io，记录数据库位于/run/sathex-payload-records的tmpfs。外部固存文件号7提供三帧样例，星敏样例以100ms间隔最多20帧，回放写入两个本地文件，均在日志中明确标记FILE_FIXTURES。
- measurements仍读取真实RK温度和UTC，后摩未安装为FF，电压/功耗缩放未验证为FFFF；allow_fixture_telemetry=false。本次UTC已校准，未宣称持续NTP或RTC恢复已验收。
- enable_reboot=true，K1109后端已绑定，但本轮不重复重启。重启与活动样例任务互斥。

测试后gs422-payload-lab和gs422-measurements-lab均停止且disabled，恢复正式AT服务active/enabled。配置已保存，但未把实验服务改为开机默认服务。当前正式使用串口仍为9600/8N1 AT；样例二进制服务测试时为115200/8N1。

## 安装后的真实COM3测试

共20次控制操作、版本查询与一帧完整遥测，所有响应经过独立帧解码验证SUM/长度/尾标识、SEQ及status=3/error=0：

- 软件恢复→算法启动（ID1、CPU求和5050）→暂停→恢复→停止→软件停止。
- D0352双输出链路配置、D0353文件1/两帧配置、K1111回放、K1112停止；两个实际输出文件内容均为fixture-alphafixture-beta（25B）。
- D0354读取固存样例文件7的3帧，K1114异步结果为frames=3/bytes=45/state=2，再执行K1113停止。
- K1115样例记录开始和结束，K1110删除3个本实例完整记录。
- K1101/K1102/K1103门控，K1119业务版本回读sample-v1。

重复的恢复、记录开关算两次操作，不能把20次操作误称20个不同表格条目。本轮没有物理上传数据到ttyS8；K1116/K1117、D0351及软件包上注仍引用之前PTY证据，K1109引用此前实际重启证据。

最终物理遥测：TMG006=51°C，TMG007=FF，TMG005/TMG034=FFFF，TMG015=1、TMG017=1、APP1停止状态0，TMG037=3、TMG038=45、TMG036=3，保留字段0。业务值来自实际运行的样例驱动和Docker，而不是硬编码成功状态。

## 配置与保护验证

deploy/provision_sample_catalog.py仅允许空lab catalog且无既有软件状态时执行；再次调用明确拒绝覆盖。备份在/var/lib/sathex-payload-lab/provision-4028dd1d9e/config.before.json。注册完成后重新实例化已安装DurableDemo，确认sample-v1、F0归档及两个容器均停止。check-config返回trusted_packages=2、hardware_drivers=file_fixtures、fixture_telemetry=false。

新增2项文件fixture测试及板上完整164项单元测试通过，8.884秒。脚本异常时停止/删除本次创建对象并恢复本次写入配置，保留诊断文件，不递归清理未知目录。

证据：goal-evidence-20261120/goal-provision-catalog.log、goal-labcfg-unit.log、installed-fixture-requests.json、installed-fixture-results.json。实际配置可用不代表外设或所有参数分支已经覆盖。

## 仍需完成

后续可在此持久样例配置上完成控制时限和进一步参数测试；物理快速上注仍缺第二转换器。真实外设验收留待设备接入，后摩留待装卡；SSD未安装、NPU加速未提供，不虚构相应能力。新服务未设开机自启，涉及持续UTC同步及业务状态恢复的长时验收仍未完成。main保持未合并，全目标仍进行中。
