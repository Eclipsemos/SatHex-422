# 本机业务执行器接口

用户后续授权用样例验证部署和算法，新增显式选择的DemoDocker后端：仅task_id=1的CPU求和样例、软件v1/v2和故障包，不填入默认actions。样例实际验收与正式业务缺口见[验收记录](DEMO-ACCEPTANCE-20261120.md)。

2026-11-19范围更新：按用户要求，task_start/task_stop/task_pause/software_resume/software_stop五个算法相关映射明确留空，不提供假算法。非算法业务继续按[业务设计草案](PAYLOAD-SERVICES-DESIGN.md)开发；其中新增服务和遥测页目前属于设计，尚未接入执行器。

实验入口：`python3 -m gs422.space_service --config /etc/gs422.conf --adapters /etc/gs422-space-adapters.json`。示例配置是空映射，只有接收门控在通信服务内部执行，其余业务在未接入前返回错误7。空配置不能视为19条业务指令已实现。

配置文件由管理员维护，不从RS422上传。`actions`将设计稿中的action名称映射到固定绝对路径argv。调用时标准输入为UTF-8 JSON：

```json
{"action":"task_start","parameters":{"task_id":2,"priority":3,"mode":1,"npu":false}}
```

成功输出：`{"ok":true,"data":{"task_id":2}}`；失败输出`{"ok":false}`并可非零退出。返回的data编码后最多490字节，经TYPE16异步请求读取，不能主动发串口。控制服务只在后端成功返回后增加执行成功计数；返回成功意味着执行器承诺已完成请求的动作，如启动成功或持久化受理成功。长任务应由独立受控服务管理，不能让一次执行器进程无限阻塞等待算法完成。后台任务的后续终态遥测需随业务接口另行设计。

默认超时5秒，可配置不超过30秒。输出总量最多4096字节；超时/输出超限清理进程组，记录失败，不自动重试。执行器需要自己保证动作原子性和审计，超时不能证明副作用未发生。执行器不应直接派生需跨调用存活的后台进程，应通过systemd/Docker/本机IPC调度已有管理服务。

TYPE16 CODE00 BODY为 `HAS_RESULT:u8`；非空时再跟 `SEQ:u16 CODE:u8 STATUS:u8 ERROR:u16 DATA_LEN:u16 DATA:0..490B`。DATA为JSON，空字节串表示无附加数据。FIFO读取即出队，最多64项，队列溢出遥测可见；不提供断线重传保证，若业务要求可靠结果下传，应补充确认/游标协议。

接入前必须提供：任务ID和运行程序映射、单/多任务及优先级策略、NPU选择方式、软件停止/恢复边界、星敏输入接口与落盘目录、固存协议及CRC32参数、历史文件ID和索引、数传/测控回放接口、OTA文件格式/签名/分区和回滚机制。默认不执行重启、目录清理或分区写入。

实验服务systemd文件仅作为手动部署模板，不加入现有2.6.0正式包，也不自动启用。模板的ProtectSystem=strict使本机文件系统只读；业务写操作应委托独立管理服务，或在完成权限审查后明确列出所需ReadWritePaths，不能笼统开放整个文件系统。
