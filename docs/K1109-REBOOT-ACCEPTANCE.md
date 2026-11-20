# K1109 物理串口重启验收

2026-11-20（电脑北京时间）。运行环境为X26新固件5.10.198 #78。目标是验证TYPE21/CODE84的实际整板重启，不以SSH断开单独证明成功。

## 实现

新增gs422.board_reboot显式后端，默认disabled。仅支持board_reboot、空参数；固定调用systemd-run创建10秒后的systemctl reboot任务，不接受串口传入shell。意图和调度结果通过文件fsync、原子替换、目录fsync持久保存；Linux flock保护并发。同一boot_id的重复请求不会安排第二次重启。调度超时或进程中断后保持outcome_unknown/intent，拒绝盲目重试，需本地查证systemd任务状态。

执行结果中的state=scheduled表示系统已受理，不等于设备已重启；完成验收必须观察不同boot_id以及服务恢复。该语义与结果JSON一并返回。调度权限未配置时失败，不写入默认正式执行器列表。

## 实测

测试前Docker无运行容器，正式gs422独占ttyS6。短时停止正式AT服务，启用120秒上限的transient实验二进制服务（115200/8N1），仅注册本次board_reboot后端。后台状态文件/var/lib/sathex-reboot-check/request.json采用root权限，重启动作不依赖SSH会话存活。

1. 通过Windows COM3发送TYPE21、SEQ77、CODE84、无参，未收到立即应答。
2. 通过TYPE15/SEQ78请求执行结果，TYPE16返回对应SEQ77、CODE84、status3/error0及state=scheduled。主机独立Decoder验证帧长度、累加和与尾标志。
3. 原boot_id为0646070e-f3ec-41c6-a4b1-1c52535bd941；约10秒后SSH被主板关闭。
4. 重新连接固定地址后boot_id为a2cc7b56-5f0e-484c-98db-a17f476910a9，uptime约1分钟，证明实际重启。
5. gs422/docker/ssh均active/enabled，NRestarts=0。enP4p65s0保持192.168.137.2/24，证明固定地址持久化和自动连接生效。
6. 正式AT服务恢复9600/8N1。14条查询完整返回，STA/CPU/DISK/IP1/SSH正常，Docker29.1.3 STATE=READY。IP2/WIFI未连接、SDR未启用按实际状态报错。原hello-world停止容器保留，没有本次新运行容器。

单元测试：135项全部通过，8.295秒。新增4项涵盖默认拒绝、同一boot重复/跨实例去重、调度超时不重试、新boot允许及参数拒绝。

证据文件（本机goal-evidence-20261120目录）：reboot-result.hex、reboot-before.json、reboot-after.json、reboot-after-at.json、goal-reboot-unit.log。主板保留/root权限的受理记录用于审计；当前没有未来重启计时任务（原transient timer已随重启退出）。

## 边界

本次仅对整板重启路径做真实物理验收，不代表其余业务完整。实验服务未设为开机自启，正式服务保持此前AT部署。后续统一部署时需显式绑定后端、在活动业务退出/持久化后调用，并沿用本次去重及受理语义。当前以测试前确认无活动业务为前提，尚未证明与正在写入/运行算法任务的协调停机。

主板RTC仍显示固件旧时间，本次不修改时钟；正式UTC遥测仍需解决时间同步。第二只上注转换器仍未连接，ttyS8物理链路未验收。
