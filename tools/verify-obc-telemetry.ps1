param([string]$PortName='COM4')
$ErrorActionPreference='Stop'
$port=[IO.Ports.SerialPort]::new($PortName,115200,'None',8,'One')
$port.ReadTimeout=2000
function Query([string]$hex,[string]$prefix,[int]$payloadLength){
    $in=[Convert]::FromHexString($hex)
    $port.Write($in,0,$in.Length)
    $h=[byte[]]::new(4)
    for($i=0;$i -lt 4;$i++){$h[$i]=$port.ReadByte()}
    $n=256*[int]$h[2]+$h[3]
    if($h[0] -ne 235 -or $h[1] -ne 144 -or $n -gt 504 -or ($payloadLength -gt 0 -and $n -ne $payloadLength)){throw "unexpected head $([Convert]::ToHexString($h)) for $hex; length $n expected $payloadLength"}
    $p=[byte[]]::new($n); $sum=0
    for($i=0;$i -lt $n;$i++){$p[$i]=$port.ReadByte();$sum+=$p[$i]}
    $cs=256*$port.ReadByte()+$port.ReadByte()
    if($cs -ne ($sum -band 65535) -or $port.ReadByte() -ne 9 -or $port.ReadByte() -ne 215){throw 'checksum or tail'}
    $rx=[Convert]::ToHexString($p)
    if(!$rx.StartsWith($prefix)){throw "unexpected reply $rx"}
    $out=[ordered]@{Tx=$hex;RxPayload=$rx;Checksum=('{0:X4}' -f $cs)}
    if($p[0] -eq 18){$k=$prefix.Length/2+37;$out.VoltageRaw=256*[int]$p[$k]+$p[($k+1)]}
    [pscustomobject]$out
}
try{
    $port.Open();$port.DiscardInBuffer()
    Query 'EB9000083056000000000000008609D7' '3356' 0
    Query 'EB9000023214004609D7' '33' 2
    Query 'EB9000051101000100001309D7' '1201000100' 66
    Query 'EB90000111001109D7' '12' 62
    Query 'EB9000021100001109D7' '1200' 63
    $bad=[Convert]::FromHexString('EB9000083056000000000000008E09D7')
    $port.Write($bad,0,$bad.Length);Start-Sleep -Milliseconds 300
    if($port.BytesToRead){throw 'length-inclusive checksum unexpectedly accepted'}
}finally{$port.Dispose()}
