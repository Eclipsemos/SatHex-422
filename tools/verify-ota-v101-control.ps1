param([switch]$Installed, [string]$PortName='COM3')
$ErrorActionPreference = 'Stop'
$port = [IO.Ports.SerialPort]::new($PortName,115200,'None',8,'One')
$rows = [Collections.Generic.List[object]]::new()
function Send([string]$hex) {
    $bytes = [Convert]::FromHexString($hex)
    $port.Write($bytes,0,$bytes.Length)
}
function Read-Frame {
    $bytes = [Collections.Generic.List[byte]]::new()
    for ($i=0; $i -lt 4; $i++) { $bytes.Add($port.ReadByte()) }
    $length = 256*[int]$bytes[2]+$bytes[3]
    if ($length -gt 504 -or $bytes[0] -ne 235 -or $bytes[1] -ne 144) { throw 'frame header' }
    for ($i=0; $i -lt $length+4; $i++) { $bytes.Add($port.ReadByte()) }
    $a=$bytes.ToArray()
    $sum=0
    for($i=4;$i -lt 4+$length;$i++){ $sum += $a[$i] }
    if (($sum -band 65535) -ne (256*[int]$a[-4]+$a[-3]) -or $a[-2] -ne 9 -or $a[-1] -ne 215){throw 'frame checksum/tail'}
    $hex=[Convert]::ToHexString($a)
    $rows.Add($hex)
    return $hex
}
function Both([int]$value) {
    Send 'EB9000023214004609D7'
    $short=Read-Frame
    if ($short.Substring(8,4) -ne ('33{0:X2}' -f $value)){throw "short $short"}
    Send 'EB9000051101000100001309D7'
    $tm=Read-Frame
    if ($tm.Length -ne 148 -or $tm.Substring(76,2) -ne ('{0:X2}' -f $value)){throw "telemetry $tm expected $value"}
}
try {
    $port.ReadTimeout=3500
    $port.Open()
    if($Installed) {
        Send 'EB9000023214004609D7'
        $short=Read-Frame
        if ($short.Substring(8,2) -ne '33'){throw $short}
        Both ([Convert]::ToInt32($short.Substring(10,2),16))
    } else {
        Both 32
        Send 'EB9000083054000000000000008409D7'
        Start-Sleep -Milliseconds 80
        Both 33
        if((Read-Frame) -ne 'EB9000083154000000000000008509D7'){throw 'Z54 ACK'}
        Both 35
        Send 'EB9000083055020100000000008809D7'
        if((Read-Frame) -ne 'EB9000083155FF0000000000018509D7'){throw 'Z55 FF ACK'}
        Both 255
        Send 'EB9000083053040201000000008A09D7'
        if((Read-Frame) -ne 'EB9000083153000000000000008409D7'){throw 'recover Z53'}
        Both 32
    }
    Send 'EB9000083056000000000000008609D7'
    $v=Read-Frame
    $version=[Text.Encoding]::ASCII.GetString([Convert]::FromHexString($v.Substring(12,$v.Length-22)))
    if (!$Installed -and $version -ne 'CONTROL-QA'){throw "fixture version $version"}
    [pscustomobject]@{port=$PortName;profile='V1.0.2';installed=[bool]$Installed;version=$version;frames=$rows} | ConvertTo-Json
}finally { $port.Dispose() }
