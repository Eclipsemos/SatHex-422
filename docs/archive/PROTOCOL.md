# RS422 protocol, revision 4 (gs422 2.2.0)

完整中文使用说明：[全部 AT 命令手册](../AT-COMMANDS.md)。

Independent implementation derived from the shanhe-serial 0.2.2 documented wire
protocol. Revision 2 intentionally replaces the STA numeric tuple with labeled,
unit-bearing fields. Other commands retain their previous format. No legacy
source or executable is included in this repo.
Revision 3 adds CPU queries; all revision 2 replies are unchanged.
Revision 4 adds Docker queries and allowlisted container controls; previous replies
are unchanged. See [Docker commands](DOCKER.md) for syntax and limits.

## Transport

- 9600 baud, 8 data bits, no parity, 1 stop bit, no flow control.
- Commands are case-sensitive UTF-8; no surrounding spaces are stripped.
- Input terminators: CR, LF, CRLF. Output terminator: CRLF.
- Empty frames are ignored; no echo or unsolicited reports.
- Maximum input: 512 bytes excluding terminators. Oversized frames are discarded
  through the terminator and return one `ERROR`, then parsing resumes.
- Invalid UTF-8, unknown or damaged commands: `ERROR`.
- Queries accept the spelling below and the same spelling with a final `?`.

| Command | Reply | Units / semantics |
|---|---|---|
| `AT+STA` | `+STA:NPU_TEMP=...C,NPU_POWER=...W,RK_TEMP=...C,VIN12_CURRENT=...mA` | Celsius, W, Celsius, mA |
| `AT+DOCKER` | `+DOCKER:STATE=READY,...` | Engine status; subcommands in [DOCKER.md](DOCKER.md) |
| `AT+CPU` | `+CPU:USAGE=...,IOWAIT=...,LOAD1=...,LOAD5=...,LOAD15=...,CORE_COUNT=...,CORE_USAGE=...,FREQ=...,TEMP=...,WINDOW_MS=...` | CPU telemetry; see below |
| `AT+IP1` | `+IP1:ipv4,netmask` or `+IP1:ERR` | first physical Ethernet interface |
| `AT+IP2` | `+IP2:ipv4,netmask` or `+IP2:ERR` | second physical Ethernet interface |
| `AT+WIFI` | `+WIFI:ssid,ipv4,netmask` or `+WIFI:ERR` | activated Wi-Fi with usable IPv4 |
| `AT+WIFI=ssid,password` | `+WIFI:OK` or `+WIFI:ERR` | connect and persist after verification |

Example:

```text
+STA:NPU_TEMP=30.10C,NPU_POWER=4.920W,RK_TEMP=35.15C,VIN12_CURRENT=1262mA
```

| Field | Meaning | Display |
|---|---|---|
| NPU_TEMP | Selected NPU card's highest reported temperature | Celsius, 2 decimal places |
| NPU_POWER | hm_smi reported board power | Watts, 3 decimal places |
| RK_TEMP | RK3588 SoC temperature | Celsius, 2 decimal places |
| VIN12_CURRENT | Current from the configured 12V sensor | Integer milliamps |

ASCII `C` means degrees Celsius; ASCII avoids degree-symbol encoding issues on
serial terminals. Decimal separator is always `.`. Order and names are fixed.
Each unknown/expired value is simply `-` (e.g. `NPU_TEMP=-`), without a unit suffix.
Valid zero is `0.00C`, `0.000W` or `0mA`.

**Breaking wire-format change:** before 2.0.0 the same reading was
`+STA:3010,4920,3515,1262` (centi-Celsius, mW, centi-Celsius, mA). Consumers of the
old four integers must be updated. Config divisors and internal sampling units
are unchanged; conversion is applied exactly once when formatting the reply.

## CPU query

`AT+CPU` and `AT+CPU?` return one ASCII line with CRLF. Example (illustrative):

```text
+CPU:USAGE=12.5%,IOWAIT=0.0%,LOAD1=1.00,LOAD5=0.50,LOAD15=0.20,CORE_COUNT=8,CORE_USAGE=0:0.0%|1:0.0%|2:0.0%|3:0.0%|4:0.0%|5:0.0%|6:0.0%|7:100.0%,FREQ=0-3:1296.0MHz|4-5:1608.0MHz|6-7:1608.0MHz,TEMP=37.00C,WINDOW_MS=1000
```

- USAGE: host-wide busy percentage, normalized to 0..100 across online cores.
  One busy core of eight is about 12.5% total. CORE_USAGE gives each logical core,
  sorted by ID, each normalized to 0..100. IDs are zero-based; separators are `|`.
- A dedicated background collector samples roughly every 1 second. Percentages
  use differences between two `/proc/stat` snapshots, not lifetime averages.
  WINDOW_MS is the actual monotonic-time separation, not a fixed promised period.
  Query processing does not wait for a sample. This cadence is independent of
  `sample_interval_ms`, which continues to control NPU/SoC/current.
- Total = delta(user+nice+system+idle+iowait+irq+softirq+steal).
  Busy = delta(user+nice+system+irq+softirq). Guest times are already included in
  user/nice and are not added again. Steal and I/O wait are not CPU execution.
- IOWAIT: delta(iowait)/delta(total). It is a kernel accounting estimate, not disk
  utilization or a reliable per-device IO-pressure measurement. Negative counters
  or zero elapsed ticks invalidate that percentage window instead of giving spikes.
  Reference: [Linux /proc documentation](https://www.kernel.org/doc/html/v6.12/filesystems/proc.html).
- LOAD1/5/15: `/proc/loadavg` load averages, dimensionless, **not percentages**.
- CORE_COUNT: online logical CPU count from sysfs, not maximum possible cores.
  CPU hotplug re-warms usage for one interval; newly online cores are not shown as
  falsely busy. Gaps over 3 seconds, sampling failure or counter resets require
  a new valid pair of samples.
- FREQ: each cpufreq policy's affected online cores and `scaling_cur_freq` converted
  from kHz to MHz (1 decimal). `0-3` means cores 0 through 3; noncontiguous groups
  use `+`. This is the driver's frequency report, not a cycle-counter measurement
  or interval average. Unsupported frequency is `-`, never zero.
- TEMP: existing SoC collector's temperature, same sensor and TTL as RK_TEMP in STA.
  No second sensor read is triggered. CPU and temperature timestamps can differ.
- Before the first two samples, usage/window is `-`; available frequency/load/core
  count may already appear. Other missing fields are `-`. A valid idle CPU is 0.0%.
- CPU snapshots expire using `cache_ttl_ms` and monotonic time. Failed reads and
  queries never renew age. Individual unavailable fields remain `-`. Temperature
  expires independently. If all CPU data is unavailable, labeled fields remain
  in the reply with `-`, rather than returning invented zeroes.
- Each helper read has a 2-second deadline, with the same bounded-child handling
  as sensor reads. A stuck sysfs read cannot block serial RX or spawn endless helpers.

On the RK3588 the reply is about 280 bytes, roughly 0.3 seconds at 9600/8N1.
Polling once per second is suitable; avoid flooding CPU and other queries together.
This implementation targets small embedded CPU counts; it does not promise a
compact reply for servers with hundreds of cores.

## Sampling

NPU, SoC and current have independent collector threads. STA reads a snapshot and
never waits for collection. Successful fields expire independently after
`cache_ttl_ms` (5000 default). Failures and queries do not renew age. Missing,
expired or invalid values are `-`; zero is valid. Fields may differ in sample time.

Each collector waits `sample_interval_ms` after an attempt. hm_smi gets 3 seconds;
sensor reads get `sensor_timeout_ms` (2000 default). Output is bounded. TERM/KILL
target only collector-created process groups. Kernel-stuck children are tracked
and not multiplied on future attempts.

NPU temperature is the selected card's highest reported temperature. Power is the
tool's Board_Power/Total_Power, not a sum or average; conflicting values are `-`.
Verified format: HMSW 1.5.0 / HM_SMI 1.0.0, one LQ50-24GB. Other formats and
multi-card systems need additional fixtures.

## Interface mapping

Candidates are Ethernet-type sysfs interfaces with a physical device link.
wireless/phy80211 distinguishes Wi-Fi. Sort by hardware path, dev_port,
phys_port_name, with name as a final tie-break. Mapping ignores link/IP state.
Explicit names override auto; auto excludes reserved wired interfaces. Legacy
eth0/eth1/wlan0 defaults become auto. IP queries return the primary usable IPv4;
multiple addresses are not exposed. No stale IP addresses are cached.

## Wi-Fi

Escape commas as `\,` and backslashes as `\\` in arguments and query SSIDs.
SSID: 1..32 UTF-8 bytes, no control characters. Password: empty for open Wi-Fi,
8..63 printable ASCII characters, or 64 hex PSK. Enterprise/WEP/SAE-only WPA3 are
unsupported. Argument spaces are preserved. Bad arguments return `+WIFI:ERR`.

Uses NetworkManager D-Bus; secrets never enter argv, logs or credential temp files.
NetworkManager saves the password in its protected profile after verification.
Flow: checkpoint Wi-Fi devices; radio off/on; create volatile profile; activate;
verify connection/SSID/IPv4; Save; destroy checkpoint. Failure deletes the attempt
and rolls back. A checkpoint timer guards against a lost process. Existing saved
profiles are not overwritten or deleted. Each success creates a saved gs422-*
profile; administrators may remove superseded profiles.

Radio off/on affects all Wi-Fi radios. Do not run live testing over the only Wi-Fi
management link. Rollback is best effort if NetworkManager or hardware fails.
Only one setting is allowed; a second gets immediate ERR. Wi-Fi queries and two
wired query workers run independently. During transition Wi-Fi queries can be ERR.
D-Bus calls have deadlines. No detailed external exception text is logged.

## Ordering and limits

One writer sends results in completion order, without request IDs. For precise
matching, send one outstanding request of a given kind, especially Wi-Fi.
Capacity: 128 wired requests, 32 Wi-Fi queries, 1 setting, 512 TX replies. Waiting
network work expires at 5 seconds. Refusals return command ERR. TX overflow or
write timeout terminates the process for systemd recovery. 9600 baud limits total
throughput; kernel acceptance of TX does not prove physical receiver delivery.

Stop cancels queued operations and collection, and requests Wi-Fi rollback.
Interrupted requests are not guaranteed a reply. Logs contain kinds, sampling
transitions and TX status, never raw frames, passwords or SSIDs.
