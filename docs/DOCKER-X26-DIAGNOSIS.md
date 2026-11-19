# X26 Docker 启动故障检查 2026-11-19

实际运行内核为厂商5.10.198 #61，`/proc/config.gz`中：

```text
# CONFIG_OVERLAY_FS is not set
# CONFIG_BRIDGE is not set
# CONFIG_VETH is not set
```

`/proc/filesystems`没有overlay，`/sys/module/overlay`不存在，且模块目录没有可加载的overlay.ko。虽然modules.builtin声称overlay内置、modinfo显示builtin，但与实际内核配置/注册状态矛盾，说明根文件系统的模块元数据不能代表运行中的内核能力。

Docker日志：`failed to mount overlay: no such device`，`prior storage driver overlay2 failed: driver not supported`。原有故障发生于本次gs422安装之前。不存在/etc/docker/daemon.json覆盖配置。

恢复路径：获取与此板硬件设备树/驱动匹配的厂商内核及模块，启用OverlayFS，并补齐Docker所需namespace/cgroup、veth/bridge/netfilter等能力；升级前准备可回退启动镜像并核对厂商刷写流程，不能只替换同版本号的随机模块。修复后先用临时目录验证overlay挂载，再启动Docker检查原有镜像与容器。本次仅诊断，未改写内核、Docker配置或/var/lib/docker。

不自动改成vfs或另建数据根目录：切换存储驱动会让原有镜像/容器不可见，且不能补齐缺失的容器网络能力。来源：[Docker官方存储驱动说明](https://docs.docker.com/engine/storage/drivers/select-storage-driver/)、[OverlayFS前提条件](https://docs.docker.com/engine/storage/drivers/overlayfs-driver/)。
