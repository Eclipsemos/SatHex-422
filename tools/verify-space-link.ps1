param(
    [string]$PortName = 'COM3',
    [int]$Baud = 115200,
    [ValidateSet('Even','Odd','None')][string]$Parity = 'Even',
    [string]$OutputPath = 'space-link-results.json'
)
# Run only against the temporary experimental service, not the production AT service.
$ErrorActionPreference = 'Stop'
$port = [System.IO.Ports.SerialPort]::new($PortName,$Baud,[System.IO.Ports.Parity]::$Parity,8,[System.IO.Ports.StopBits]::One)
$port.Handshake = [System.IO.Ports.Handshake]::None
$port.WriteTimeout = 1000
$port.DtrEnable = $false
$port.RtsEnable = $false
$records = [Collections.Generic.List[object]]::new()
function Frame([byte]$kind,[int]$seq,[byte]$code,[byte[]]$body=@()) {
    [byte[]]$payload = @($kind,1,($seq -shr 8),($seq -band 255),$code) + $body
    $sum = 0; foreach ($v in $payload) { $sum = ($sum + $v) -band 65535 }
    [byte[]](@(235,144,($payload.Length -shr 8),($payload.Length -band 255)) + $payload + @(($sum -shr 8),($sum -band 255),9,215))
}
function Receive([bool]$expected) {
    $bytes = [Collections.Generic.List[byte]]::new()
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $limit = if ($expected) { 2000 } else { 100 }
    while ($timer.ElapsedMilliseconds -lt $limit) {
        while ($port.BytesToRead -gt 0) { $bytes.Add([byte]$port.ReadByte()) }
        if (-not $expected -and $bytes.Count) { throw 'Unexpected immediate command reply' }
        if ($bytes.Count -ge 4) {
            if ($bytes[0] -ne 235 -or $bytes[1] -ne 144) { throw 'Invalid header' }
            $size = ([int]$bytes[2] * 256) + $bytes[3]
            if ($size -gt 504) { throw 'Oversize frame' }
            if ($bytes.Count -ge $size + 8) {
                if ($bytes.Count -ne $size + 8) { throw 'Extra bytes after response' }
                [byte[]]$p = $bytes.GetRange(4,$size).ToArray()
                $sum = 0; foreach ($v in $p) { $sum = ($sum + $v) -band 65535 }
                if ($sum -ne ([int]$bytes[4+$size] * 256 + $bytes[5+$size]) -or $bytes[6+$size] -ne 9 -or $bytes[7+$size] -ne 215) { throw 'Bad checksum/tail' }
                $records.Add([pscustomobject]@{kind=$p[0];sequence=([int]$p[2]*256+$p[3]);hex=[Convert]::ToHexString($bytes.ToArray());host_roundtrip_ms=$timer.Elapsed.TotalMilliseconds})
                return ,$p
            }
        }
        Start-Sleep -Milliseconds 1
    }
    if ($expected) { throw 'Response timeout' }
}
function Ask([int]$seq) {
    [byte[]]$f = Frame 17 $seq 0
    $port.Write($f,0,$f.Length)
    [byte[]]$p = Receive $true
    if ($p[0] -ne 18 -or $p.Length -ne 65 -or ([int]$p[2]*256+$p[3]) -ne $seq) { throw 'Invalid telemetry layout/sequence' }
    return ,$p
}
function U32([byte[]]$p,[int]$offset) {
    [uint32]([uint64]$p[$offset]*16777216 + [uint64]$p[$offset+1]*65536 + [uint64]$p[$offset+2]*256 + $p[$offset+3])
}
try {
    $port.Open()
    $null = Receive $false
    [byte[]]$baseline = Ask 1
    [byte[]]$gate = Frame 33 100 23 @(1,0,0,0,0,0)
    $port.Write($gate,0,$gate.Length)
    $null = Receive $false
    [byte[]]$after = Ask 2
    if ($after[61] -ne 1 -or $after[40] -ne 3 -or (U32 $after 13) -ne (U32 $baseline 13)+1) { throw 'Gate command not completed' }
    $port.Write($gate,0,$gate.Length)
    $null = Receive $false
    [byte[]]$duplicate = Ask 3
    if ((U32 $duplicate 13) -ne (U32 $after 13)) { throw 'Duplicate executed twice' }
    [byte[]]$bad = Frame 33 101 131 @(0,0,0,0,0,0)
    $port.Write($bad,0,$bad.Length)
    $null = Receive $false
    [byte[]]$rejected = Ask 4
    if ($rejected[40] -ne 4 -or $rejected[42] -ne 4) { throw 'Invalid parameters not rejected' }
    [byte[]]$missing = Frame 33 102 82
    $port.Write($missing,0,$missing.Length)
    $null = Receive $false
    [byte[]]$notConfigured = Ask 5
    if ($notConfigured[40] -ne 5 -or $notConfigured[42] -ne 7) { throw 'Missing adapter falsely succeeded' }
    [byte[]]$corrupt = Frame 17 6 0
    $corrupt[9] = $corrupt[9] -bxor 1
    $port.Write($corrupt,0,$corrupt.Length)
    $null = Receive $false
    [byte[]]$recovered = Ask 7
    if ((U32 $recovered 17) -lt (U32 $baseline 17)+1) { throw 'Checksum error not counted' }
    [byte[]]$poll = Frame 21 8 0
    $port.Write($poll,0,$poll.Length)
    [byte[]]$result = Receive $true
    if ($result[0] -ne 22 -or $result[5] -ne 1) { throw 'Missing async result' }
    [byte[]]$closeGate = Frame 33 103 23 @(0,0,0,0,0,0)
    $port.Write($closeGate,0,$closeGate.Length)
    $null = Receive $false
    [byte[]]$final = Ask 9
    if ($final[61] -ne 0) { throw 'Gate not closed' }
    'PASS: binary telemetry, no unsolicited/command ACK, gate, duplicate suppression, parameter rejection, missing adapter, checksum recovery, result poll'
} finally {
    if ($port.IsOpen) { $port.Close() }
    $port.Dispose()
    $records | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $OutputPath -Encoding utf8
}
