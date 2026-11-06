# RS422 compatibility protocol, revision 1

Independent implementation of the installed shanhe-serial 0.2.2 documented wire
protocol. No legacy source or executable is included in this repo.

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
| `AT+STA` | `+STA:npu_temp,npu_power,soc_temp,current` | 0.01 Celsius, mW, 0.01 Celsius, mA |
| `AT+IP1` | `+IP1:ipv4,netmask` or `+IP1:ERR` | first physical Ethernet interface |
| `AT+IP2` | `+IP2:ipv4,netmask` or `+IP2:ERR` | second physical Ethernet interface |
| `AT+WIFI` | `+WIFI:ssid,ipv4,netmask` or `+WIFI:ERR` | activated Wi-Fi with usable IPv4 |
| `AT+WIFI=ssid,password` | `+WIFI:OK` or `+WIFI:ERR` | connect and persist after verification |

Example `+STA:3010,4920,3515,1262`: 30.10 C, 4.920 W, 35.15 C, 1262 mA.
**Legacy units must not silently change to Celsius or Watts.**

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
