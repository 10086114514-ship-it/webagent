param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Lang = "zh-Hans"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

# 优先精确匹配，其次回退 zh-Hans / en-US / 用户档案语言
$engine = $null
$engineTag = $null
$candidates = @($Lang)
if ($Lang -ne "zh-Hans") { $candidates += "zh-Hans" }
$candidates += @("en-US")

foreach ($tag in $candidates) {
    try {
        $langObj = New-Object Windows.Globalization.Language $tag
        $e = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($langObj)
        if ($null -ne $e) { $engine = $e; $engineTag = $tag; break }
    }
    catch { }
}
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    $engineTag = "user-profile"
}
if ($null -eq $engine) {
    [pscustomobject]@{ error = "no ocr engine"; lang = $Lang } | ConvertTo-Json -Compress | Out-File -Encoding UTF8 $Out
    exit 2
}

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$words = New-Object System.Collections.ArrayList
foreach ($line in $result.Lines) {
    foreach ($w in $line.Words) {
        [void]$words.Add([pscustomobject]@{
                t = $w.Text
                x = [int][math]::Round($w.BoundingRect.X)
                y = [int][math]::Round($w.BoundingRect.Y)
                w = [int][math]::Round($w.BoundingRect.Width)
                h = [int][math]::Round($w.BoundingRect.Height)
            })
    }
}
[pscustomobject]@{ lang = $engineTag; words = $words } | ConvertTo-Json -Compress -Depth 6 | Out-File -Encoding UTF8 $Out
