param([string]$PortName='COM3')
$ErrorActionPreference='Stop'
$port=[IO.Ports.SerialPort]::new($PortName,115200,'None',8,'One')
$port.ReadTimeout=3500
function Send-Payload([byte[]]$p) {
    $sum=0
    foreach($b in $p){$sum+=$b}
    [byte[]]$f=@(235,144,0,$p.Length)+$p+@(($sum -shr 8),($sum -band 255),9,215)
    $port.Write($f,0,$f.Length)
}
function Read-Payload {
    if($port.ReadByte() -ne 235 -or $port.ReadByte() -ne 144){throw 'header'}
    $n=256*$port.ReadByte()+$port.ReadByte()
    if($n -gt 504){throw 'length'}
    $buf=[byte[]]::new($n)
    $sum=0
    for($i=0;$i -lt $n;$i++){ $buf[$i]=$port.ReadByte(); $sum+=$buf[$i] }
    if(256*$port.ReadByte()+$port.ReadByte() -ne ($sum -band 65535)){throw 'sum'}
    if($port.ReadByte() -ne 9 -or $port.ReadByte() -ne 215){throw 'tail'}
    return ,$buf
}

function Get-AppState {
    Send-Payload ([byte[]]@(17,1,0,5,0))
    $tm=Read-Payload
    if ($tm.Length -ne 66 -or $tm[0] -ne 18) { throw 'unexpected telemetry' }
    return @{state=(($tm[41] -shr 2) -band 3);configured=$tm[35];running=$tm[36];completed=$tm[37]}
}
try {
    $port.Open()
    $cases=@('0000000000','FF00000000','00FF000000','0000FF0000','000000FF00',
             '00000000FF','0102030405','FFFFFFFFFF')
    $used=@{}
    foreach ($case in $cases) {
        $argsBytes=[Convert]::FromHexString($case)
        $expected=0
        for($i=0;$i -lt 5;$i++){ $expected+=($i+1)*[int]$argsBytes[$i] }
        Send-Payload ([byte[]](@(33,131,1)+$argsBytes))
        $deadline=[DateTime]::UtcNow.AddSeconds(9)
        $running=$false
        $result=$null
        do {
            Start-Sleep -Milliseconds 60
            $tm=Get-AppState
            if($tm.state -eq 1 -and $tm.running -eq 1){$running=$true}
            if($null -eq $result){
                Send-Payload ([byte[]]@(21,1,0,6,0))
                $r=Read-Payload
                if($r[0] -ne 22){throw 'expected result FIFO'}
                if($r[5] -ne 0){
                    if($r[8] -ne 131 -or $r[9] -ne 3){throw "unexpected completion: $([Convert]::ToHexString($r))"}
                    $result=[Text.Encoding]::UTF8.GetString($r,14,$r.Length-14) | ConvertFrom-Json
                }
            }
            if($null -ne $result -and $tm.state -eq 3){break}
        } while([DateTime]::UtcNow -lt $deadline)
        if(!$running -or $tm.state -ne 3 -or $tm.configured -ne 1 -or $tm.completed -ne 1){throw "APP1 telemetry $case"}
        if($null -eq $result -or $result.words -ine ('01'+$case) -or $result.value -ne $expected -or
           $result.app_exit -ne 0 -or $result.exit -ne 0 -or $result.device -ne 2 -or
           $result.file_id -lt 1 -or $used.ContainsKey($result.file_id)){throw "incorrect result for $case"}
        $used[$result.file_id]=$true
        [pscustomobject]@{words=$case;value=$result.value;file_id=$result.file_id;app_exit=$result.app_exit;state='1 -> 3'}
    }
} finally { $port.Dispose() }
