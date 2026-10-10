[CmdletBinding(DefaultParameterSetName = 'Single')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Single')][string]$ImagePath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Server')][switch]$Server,
    [string]$Language = 'ko',
    [double]$Scale = 2.0
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapTransform, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
        $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    if ($null -eq $asTask) { throw 'Windows Runtime asynchronous adapter is unavailable.' }

    function Await-WinRt($operation, [Type]$resultType) {
        $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
        return $task.GetAwaiter().GetResult()
    }

    $engines = @{}
    function Invoke-ImageOcr([string]$OcrPath, [string]$OcrLanguage, [double]$OcrScale) {
        $stream = $null
        $bitmap = $null
        try {
            if ([double]::IsNaN($OcrScale) -or [double]::IsInfinity($OcrScale) -or $OcrScale -lt 1 -or $OcrScale -gt 3) {
                throw 'Scale must be between 1 and 3.'
            }
            if (-not $engines.ContainsKey($OcrLanguage)) {
                $languageObject = New-Object Windows.Globalization.Language($OcrLanguage)
                $newEngine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($languageObject)
                if ($null -eq $newEngine) { throw "Windows OCR language is not installed: $OcrLanguage" }
                if ($engines.Count -ge 8) { $engines.Clear() }
                $engines[$OcrLanguage] = $newEngine
            }
            $engine = $engines[$OcrLanguage]
            $file = Await-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($OcrPath)) ([Windows.Storage.StorageFile])
            $stream = Await-WinRt ($file.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
            $decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $width = [int]$decoder.PixelWidth
            $height = [int]$decoder.PixelHeight
            if ($width -le 0 -or $height -le 0) { throw 'Image dimensions are invalid.' }
            $maxDimension = [double][Windows.Media.Ocr.OcrEngine]::MaxImageDimension
            $boundedScale = [Math]::Min($OcrScale, $maxDimension / [Math]::Max($width, $height))
            $scaledWidth = [uint32][Math]::Max(1, [Math]::Floor($width * $boundedScale))
            $scaledHeight = [uint32][Math]::Max(1, [Math]::Floor($height * $boundedScale))
            $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
            $transform.ScaledWidth = $scaledWidth
            $transform.ScaledHeight = $scaledHeight
            $bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync(
                [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
                [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied,
                $transform,
                [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation,
                [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage
            )) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $result = Await-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $scaleX = [double]$scaledWidth / $width
            $scaleY = [double]$scaledHeight / $height
            $lines = @(
                foreach ($line in $result.Lines) {
                    $left = [double]::PositiveInfinity
                    $top = [double]::PositiveInfinity
                    $right = 0.0
                    $bottom = 0.0
                    $words = @(
                        foreach ($word in $line.Words) {
                            $box = $word.BoundingRect
                            $x = [double]$box.X / $scaleX
                            $y = [double]$box.Y / $scaleY
                            $w = [double]$box.Width / $scaleX
                            $h = [double]$box.Height / $scaleY
                            $left = [Math]::Min($left, $x)
                            $top = [Math]::Min($top, $y)
                            $right = [Math]::Max($right, $x + $w)
                            $bottom = [Math]::Max($bottom, $y + $h)
                            @{ text = $word.Text; rect = @{ x = $x; y = $y; width = $w; height = $h } }
                        }
                    )
                    if ($words.Count -gt 0) {
                        @{ text = $line.Text; words = $words; rect = @{ x = $left; y = $top; width = $right - $left; height = $bottom - $top } }
                    }
                }
            )
            return @{ version = 1; width = $width; height = $height; language = $engine.RecognizerLanguage.LanguageTag; lines = $lines }
        } finally {
            if ($null -ne $bitmap) { $bitmap.Dispose() }
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }

    if (-not $Server) {
        $payload = Invoke-ImageOcr $ImagePath $Language $Scale
        [Console]::WriteLine(($payload | ConvertTo-Json -Depth 8 -Compress))
    } else {
        [Console]::WriteLine('{"version":1,"ready":true}')
        while ($null -ne ($requestLine = [Console]::In.ReadLine())) {
            $requestId = $null
            try {
                $request = $requestLine | ConvertFrom-Json
                if ($request.version -isnot [int] -or $request.version -ne 1 -or
                    $request.id -isnot [string] -or $request.id -cnotmatch '^[a-f0-9]{32}$') {
                    throw 'Invalid OCR request version or ID.'
                }
                $requestId = $request.id
                if ($request.path -isnot [string] -or [string]::IsNullOrWhiteSpace($request.path) -or
                    $request.language -isnot [string] -or $request.language -notmatch '^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$' -or
                    -not ($request.scale -is [int] -or $request.scale -is [long] -or $request.scale -is [double] -or $request.scale -is [decimal])) {
                    throw 'Invalid OCR image, language or scale.'
                }
                $payload = Invoke-ImageOcr $request.path $request.language $request.scale
                $reply = @{ version = 1; id = $requestId; ok = $true; result = $payload }
            } catch {
                $detail = $_.Exception.Message
                $reply = @{ version = 1; id = $requestId; ok = $false; error = $detail.Substring(0, [Math]::Min(1200, $detail.Length)) }
            }
            [Console]::WriteLine(($reply | ConvertTo-Json -Depth 10 -Compress))
        }
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
