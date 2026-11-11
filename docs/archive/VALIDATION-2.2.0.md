# gs422 2.2.0 validation — 192.168.5.135

Date: 2026-11-06 (device clock). Protocol revision 4.

- All 25 unittest cases passed on the ARM64 target, including six Docker cases.
- Fixed the existing CPU PTY test's timing assumption: a 500 ms cache TTL may
  legitimately expire between 1-second samples. Polling now waits for fresh data
  while checking every query remains under 500 ms; CPU expiry has separate tests.
- Real Docker lifecycle tested through a PTY against both source and installed
  service: status, container/image paging, inspect, stopped-stats rejection,
  start, running stats, restart, stop, deny non-allowlisted targets, reject EXEC.
- Tests created only unique `gs422-test-*` containers using the existing local
  4G image with no network or host mounts, read-only rootfs and resource limits.
  Each temporary container was removed; no original containers were controlled.
- Installed test reported container CPU 0.2%, memory 483328 bytes / 67108864.
  Concurrent STA/CPU replies during stop returned in approximately 10/11 ms
  on PTY. This does not measure physical 9600-baud transmission latency.
- Engine 29.1.3 supports API 1.44–1.52; client selected 1.44. Status also worked
  from the running systemd service's mount namespace.
- Installed `dist/gs422_2.2.0_all.deb`; retained 2.1.0 package for rollback.
  `/etc/gs422.conf` preserved byte-for-byte; SHA256:
  `3f93b5ad34b3dfa62c5304c193c0bdf9d04cd1fe78133c569553267a329b1996`.
  Backup: `/etc/gs422.conf.before-2.2.0-20261106-171058`.
- gs422 active and enabled; only PID 121868 held `/dev/ttyS6` at verification.
  shanhe-serial and rs422 were inactive. All four sensor collectors valid.
- Production control allowlist remains empty (default for absent config key).
  A direct installed START against an existing container returned NOT_ALLOWED.
- Final state: two original stopped hello-world containers, zero running
  containers, three images, no test containers remaining.

Pending user acceptance: physical RS422 queries, selecting actual container
names/IDs for control, reboot and long-duration operation. Current algorithm
wrappers use `docker run --rm`; starting a new algorithm task is outside this
existing-container lifecycle protocol.

Rollback to previous gs422 release, preserving configuration:

```sh
sudo systemctl stop gs422
sudo dpkg --force-confold -i /home/hzx/gs422-source/dist/gs422_2.1.0_all.deb
sudo systemctl start gs422
```
