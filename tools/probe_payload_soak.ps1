param([string]$PortName='COM3',[int]$Count=120,[int]$IntervalMs=500,[string]$OutputPath='payload-soak.json')
$ErrorActionPreference='Stop'
if($Count -lt 1 -or $Count -gt 60000 -or $IntervalMs -lt 0){throw 'Invalid probe bounds'}
$port=[IO.Ports.SerialPort]::new($PortName,115200,[IO.Ports.Parity]::None,8,[IO.Ports.StopBits]::One)
$port.WriteTimeout=1000
$rows=[Collections.Generic.List[object]]::new()
try {
    $port.Open()
    for($i=1;$i -le $Count;$i++) {
        if($port.BytesToRead){throw 'Unexpected bytes before request'}
        [byte[]]$payload=@(17,1,($i -shr 8),($i -band 255),0)
        $sum=0;foreach($b in $payload){$sum+=$b}
        [byte[]]$frame=@(235,144,0,5)+$payload+@(($sum -shr 8),($sum -band 255),9,215)
        $timer=[Diagnostics.Stopwatch]::StartNew()
        $port.Write($frame,0,$frame.Length)
        $data=[Collections.Generic.List[byte]]::new()
        while($timer.ElapsedMilliseconds -lt 3000) {
            while($port.BytesToRead -gt 0){$data.Add([byte]$port.ReadByte())}
            if($data.Count -ge 4 -and $data.Count -ge 8+[int]$data[2]*256+$data[3]){break}
            Start-Sleep -Milliseconds 2
        }
        if($data.Count -ne 74){throw "Unexpected response length at request $i"}
        $rows.Add([pscustomobject]@{sequence=$i;host_utc=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds();elapsed_ms=$timer.Elapsed.TotalMilliseconds;hex=[Convert]::ToHexString($data.ToArray())})
        if($i % 30 -eq 0){Write-Output "Received $i complete frames"}
        Start-Sleep -Milliseconds $IntervalMs
    }
} finally {
    if($port.IsOpen){$port.Close()};$port.Dispose()
    $rows | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $OutputPath -Encoding utf8
}
