# 独立实验包安装与部署前置条件

2026-11-20。本轮将已验证的新协议运行库制作成独立gs422-payload-lab包，在X26实际安装，避免开发源码目录充当长期安装路径。安装成功不等于外设和软件目录已配置成功。

## 安装结果

- 软件包：gs422-payload-lab 0.1.0~lab1，Architecture=all。
- 可与原gs422 2.6.0共存，程序目录/usr/lib/gs422-payload-lab，启动入口/usr/bin/gs422-payload-lab及/usr/bin/gs422-measurements-lab。
- 独立配置/etc/gs422-payload-lab.json、/etc/gs422-measurements-lab.json均为conffiles；后续安装保留管理员配置。
- 安装脚本只daemon-reload，不启动新服务、不修改/etc/gs422.conf、不接管UART。
- 原/etc/gs422.conf安装前后SHA256一致。gs422、Docker、SSH均active/enabled。
- 新gs422-payload-lab和gs422-measurements-lab均loaded、inactive/disabled。

主板包路径/home/hzx/goal-package-20261120/dist/gs422-payload-lab_0.1.0~lab1_all.deb；本机artifacts目录保留同一包。SHA256：f6eb636bdeb2c05a2f90becf1a4c12a8bcc23d066145ac4b4cefa0d714d0a33a。

## 安装后验证

- 启动器从独立安装目录导入代码；source-sha256.json列出的32个Python源码逐个hash一致。
- `gs422-payload-lab --config /etc/gs422-payload-lab.json --check-config`成功，但明确返回trusted_packages=0、hardware_drivers=not_bound、fixture_telemetry=false。
- 使用tests/live_payload_service.py --installed启动实际/usr/bin/gs422-payload-lab，在双PTY及明确fixture采集器下验证启停/上传续传、测量过期抑制、控制仍可用和采样恢复，退出码0。
- sudo systemd-analyze verify两项新服务返回0；输出中的旧test.service权限和snapd RestartMode警告不属于本包，未修改其他系统配置。
- 本轮运行库未修改，沿用162项通过的代码；增加的是打包与已安装入口验证，不虚增单元测试数量。

证据：goal-evidence-20261120/goal-package-installed.log、goal-package-systemd.log、goal-package-final.log。默认配置保持未启用，不会用空catalog或假外设启动一个“看似正常”的服务。

## 启用前仍缺什么

1. 后摩卡当前未装，按用户确认返回FF无效；电压/功耗暂为FFFF，尚未核实驱动单位及板级测点。
2. 必须配置真实测量生产者并确认当前UTC；示例utc_verified=false，防止启动时接受固件旧时间。
3. 必须注册实际允许的软件包，或明确保留长期样例软件的配置；当前样例镜像都已按测试清理，默认catalog空。
4. 星敏、固存、下传外设是否实际接入及接口定义尚未确认；通用服务目前没有这些真实驱动绑定。之前文件fixture的成功不能替代现场接入。
5. 第二只USB转RS422未接，无法从电脑对UART8做物理上注验收。

在这些条件明确前不自动enable新服务。需要回退/卸载实验包时，只处理gs422-payload-lab包及其新服务；原gs422 2.6.0继续可用。卸载不删除管理员持久化业务数据。
