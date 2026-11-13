# 2.5.1：SDR 解码缓存

服务为采集/解码子进程传入 `SATHEX_DECODER_CACHE=<output_root>/.decoder-cache`。
该目录位于已挂载的 SSD、现有 systemd 可写路径内，跨任务复用，不更改 AT 协议或任务参数。
需要同时更新 Spaceborne SatHex-dev 的 `tools/sathex_decode.py`；旧工具会忽略此环境变量。

Spaceborne 工具还加入采集 worker 的进程生命周期隔离、预分配内存和分阶段计时。
这是规避厂家 UHD PCIe 回调释放后访问的应用层处理，未替换厂家库、内核驱动或 FPGA。

135 同一历史 IQ 回放：LTE 广播约103秒降至热缓存4.39秒；DCI约234秒降至10.73秒。
冷缓存首次分别33.34秒和90.16秒；加速包含 FFT 规划复用，不省略三组PSS搜索，也不减少输入IQ。
满载接收仍偶发超时，高采样率仍会溢出；失败采集不发布成功manifest。

46项422单元测试通过。现场原始记录：
`/media/hzx/ssd/sathex/optimize-20261112/`。
其中 `integration-results.json` 保存安装后新任务验收，`decode-results.json` 保存同IQ对比。
本轮未修改调度优先级，没有新增长期压力负载。

安装后LTE冷缓存任务 `9eccd7ee43a4` 用时38.60秒，热缓存任务 `872c0a3d5970`
用时9.54秒（其中解码4.15秒），均确认PCI402。NR任务 `6f0f680081e2`
用时10.54秒，确认PCI690、11次PBCH/MIB。PTY验证查询成功，采集中取消后，
新NR任务 `4a3b89738701` 成功完成；未操作实体串口。
