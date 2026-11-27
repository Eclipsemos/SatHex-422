# X26 lab15 签名软件包上注

地面发送端只需 PowerShell 7、串口和一个 .satpkg。发送端无需登录板子、安装 Docker、持有发布私钥或写包的 SHA-256。更换地面电脑时只需重新选择实际端口。新目标板需要部署一次接收服务、可信公钥及算法依赖。原算法包及发布私钥不要发给接收板。

## 首次配置一块板

在 X26 上安装 lab15 的 deb 包，预置离线解码器镜像与采样数据。使用独立可信途径核对发布公钥的 DER SPKI SHA-256。联试公钥指纹为 `115cbabe4f6547bc54e70951b6be30401c41987f4f38f344a3e1fd7d3753e4b4`。该密钥用于本项目台架，正式发布应采用正式发布方公钥。运行以下命令，两个路径填板上实际 **capture 目录**（含 manifest.json）。已有 capture_job 配置时可以省略两个目录参数。

```sh
sudo dpkg -i gs422-payload-lab_0.1.0~lab15_all.deb
sudo python3 enable_signed_ota.py x26-lab-publisher-ed25519.pub.pem \
  --fingerprint 115cbabe4f6547bc54e70951b6be30401c41987f4f38f344a3e1fd7d3753e4b4 \
  --app0-capture /var/lib/sathex-bench/spaceborne-20261125/testdata/4g \
  --app1-capture /var/lib/sathex-bench/spaceborne-20261125/testdata/5g
```

保留本机已有的串口、测量和软件槽配置。已有的手动登记裸包继续可用；初始包 F0H–FFH 独立管理。

## 日常操作

发布方用 deploy/sign_release.py 打包，输入含 release.tar 和 release.json 的发布目录、deployed-images.json 和私钥。工具接受本地 Ed25519 PEM 或 Windows 用户的 DPAPI 密钥。已经签名的文件可以直接复制到其他地面机发送。

```powershell
pwsh -File .\send-satpkg.ps1 -Package .\20261127-auto-r1.satpkg -PortName COM4 -UploadPort COM5 -TargetSlot 1
```

TargetSlot 必须选目标板的非活动槽（1 或 2）。脚本先查版本及进度，再打开 OTA 门、清理非活动槽的候选文件、开始传输、分帧上传、补包、结束和加载。Z54 先核对文件 CRC；对于签名包，再验签、核对目标、载荷摘要、Docker 归档与镜像 ID、APP ID 和本机已安装的解码镜像。签名或兼容性检查失败时 Z54 返回 FFH，TMG014=40H。CRC 错仍返回 11H，TMG014=24H。普通文件模式不会授权激活。

Z54 成功后板端保存已验证候选，Z56 仍回旧版本。Z04 00H 在非活动槽加载候选，核对镜像 ID 和就绪版本。健康检查通过才将候选变成活动版本，并分配历史索引；失败时恢复上一版。APP 绑定按活动镜像选择，采样路径取本地配置。版本字符串或镜像 ID 与已有内容冲突时拒绝登记。同一包重发复用登记记录。Z56 查询帧为 `EB 90 00 08 30 56 00 00 00 00 00 00 00 86 09 D7`。

## 格式

ZIP_STORED 固定三项 manifest.json、manifest.sig、release.tar。签名覆盖原始 manifest.json 字节（Ed25519 64 字节签名）；清单绑定内部 release.tar 的大小、SHA-256、镜像配置 ID、版本号、目标 X26/RK3588J/linux/arm64、所需加载能力和 APP 的解码器镜像 ID。串口文件长度及 CRC 覆盖整个外包。签名注册记录保存在软件状态目录的 signed-releases.db，镜像按内部 SHA-256 归档；服务重启时用板端公钥复验登记清单，再恢复活动槽和历史索引。
