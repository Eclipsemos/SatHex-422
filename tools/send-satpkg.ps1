param(
 [Parameter(Mandatory)][string]$Package,
 [Parameter(Mandatory)][ValidateSet(1,2)][int]$TargetSlot,
 [string]$PortName='COM4',
 [string]$UploadPort='COM5'
)
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
$upload=[IO.Ports.SerialPort]::new($UploadPort,115200,'None',8,'One')
$upload.WriteTimeout=5000
$port.ReadTimeout=20000
$gate=$false;$pending=$false;$activated=$false
function Cmd([string]$payload,[string]$expect,[int]$n){
 $p=[Convert]::FromHexString($payload);$sum=0;foreach($v in $p){$sum+=$v}
 $hex='EB90'+('{0:X4}' -f $p.Length)+$payload+('{0:X4}' -f ($sum -band 65535))+'09D7'
 (Query $hex $expect $n).RxPayload
}
function Poll{
 $p=Cmd '3253000000000000' '3353' 21
 [pscustomobject]@{received=[Convert]::ToUInt32($p.Substring(20,8),16);errors=[Convert]::ToUInt32($p.Substring(28,8),16);percent=[Convert]::ToByte($p.Substring(36,2),16);state=$p.Substring(40,2)}
}

function Send-Block([int]$seq){
 $offset=$seq*499
 $n=[Math]::Min(499,$stream.Length-$offset)
 $frame=[byte[]]::new(512)
 $frame[0]=235;$frame[1]=144;$frame[2]=1;$frame[3]=248;$frame[4]=58
 for($j=0;$j -lt 4;$j++){$frame[8-$j]=($seq -shr (8*$j)) -band 255}
 [Array]::Copy($stream,$offset,$frame,9,$n)
 $sum=0
 for($j=4;$j -lt 508;$j++){$sum+=$frame[$j]}
 $frame[508]=($sum -shr 8) -band 255;$frame[509]=$sum -band 255;$frame[510]=9;$frame[511]=215
 $upload.Write($frame,0,512)
 while($upload.BytesToWrite -gt 0){Start-Sleep -Milliseconds 5}
 Start-Sleep -Milliseconds 20
}
try{
 if($PortName -eq $UploadPort){throw 'Control and upload ports must differ'}
 $path=(Resolve-Path -LiteralPath $Package).Path
 $data=[IO.File]::ReadAllBytes($path)
 $z=[IO.Compression.ZipFile]::OpenRead($path)
 try{
  $entry=$z.GetEntry('manifest.json')
  if(!$entry -or $entry.Length -gt 32768){throw 'Missing or oversized manifest'}
  $reader=[IO.StreamReader]::new($entry.Open())
  try{$manifest=$reader.ReadToEnd() | ConvertFrom-Json}finally{$reader.Dispose()}
 }finally{$z.Dispose()}
 if($manifest.format -ne 'sathex.satpkg' -or $manifest.format_version -ne 1 -or $manifest.version -cnotmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'){throw 'Invalid package manifest'}
 $stream=[byte[]]::new($data.Length+6)
 for($j=0;$j -lt 4;$j++){$stream[3-$j]=($data.Length -shr (8*$j)) -band 255}
 $table=[int[]]::new(256)
 for($i=0;$i -lt 256;$i++){
  $v=$i -shl 8
  for($j=0;$j -lt 8;$j++){if($v -band 32768){$v=(($v -shl 1) -bxor 4129) -band 65535}else{$v=($v -shl 1) -band 65535}}
  $table[$i]=$v
 }
 $crc=0
 foreach($byte in $data){$crc=(($crc -shl 8) -band 65535) -bxor $table[(($crc -shr 8) -bxor $byte) -band 255]}
 $stream[4]=$crc -shr 8;$stream[5]=$crc -band 255
 [Array]::Copy($data,0,$stream,6,$data.Length)
 $total=[int][Math]::Ceiling($stream.Length/499.0)
 $port.Open();$upload.Open();$port.DiscardInBuffer()
 $before=Cmd '3056000000000000' '3356' 0
 $status=Poll
 if($status.state -in '10','20','21','22','30'){throw 'Another operation is active'}
 $null=Cmd '3017010000000000' '311700' 8;$gate=$true
 $null=Cmd ('3055{0:X2}0100000000' -f $TargetSlot) '315500' 8
 $null=Cmd ('3053{0:X2}{1:X2}01000000' -f (2+$TargetSlot),$TargetSlot) '315300' 8;$pending=$true
 Send-Block 0;Start-Sleep -Milliseconds 250
 if((Poll).received -ne 1){throw 'First data frame not confirmed'}
 for($i=1;$i -lt $total;$i++){Send-Block $i}
 Start-Sleep -Milliseconds 300
 for($i=0;$i -lt $total;$i++){
  $p=Cmd '3253000000000000' '3353' 21
  $missing=[Convert]::ToUInt32($p.Substring(12,8),16)
  if($missing -ge $total){break}
  Send-Block $missing
 }
 $final=Poll
 if($final.received -ne $total -or $final.percent -ne 100){throw 'Incomplete upload'}
 $pending=$false
 $null=Cmd '3054000000000000' '315400' 8
 $null=Cmd '3214' '3323' 2
 $candidate=Cmd '3056000000000000' $before 0
 $null=Cmd '3004000000000000' '310400' 8
 $null=Cmd '3214' '3331' 2
 $expected='3356'+[Convert]::ToHexString([Text.Encoding]::ASCII.GetBytes($manifest.version))+'00'
 $actual=Cmd '3056000000000000' $expected ($expected.Length/2)
 [pscustomobject]@{previous=[Text.Encoding]::ASCII.GetString([Convert]::FromHexString($before.Substring(4,$before.Length-6)));version=$manifest.version;frames=$total;errors=$final.errors;Z56=$actual}
}finally{
 try{
  if($pending){$null=Cmd '3054FF0000000000' '3154' 8}
 }finally{
  try{if($gate){$null=Cmd '3017020000000000' '311700' 8}}finally{$upload.Dispose();$port.Dispose()}
 }
}
