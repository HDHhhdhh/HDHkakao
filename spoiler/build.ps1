param(
    [switch]$SkipTests,
    [switch]$SkipBridgeBuild,
    [switch]$RestoreDependencies,
    [string]$NodeExecutable = 'C:\Program Files\nodejs\node.exe'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildDirectory = Join-Path $projectRoot 'build'
$sourceDirectory = Join-Path $projectRoot 'src'
$assetDirectory = Join-Path $projectRoot 'assets'
$testDirectory = Join-Path $projectRoot 'test'
$bridgeDirectory = Join-Path $projectRoot 'bridge'
$bridgeListener = Join-Path $bridgeDirectory 'dist\spoiler-bridge.mjs'
$bridgeAuth = Join-Path $bridgeDirectory 'dist\spoiler-auth.mjs'
$embeddedNode = [System.IO.Path]::GetFullPath($NodeExecutable)
$nodeLock = Get-Content -LiteralPath (Join-Path $projectRoot 'tools\runtime-lock.json') -Raw | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $embeddedNode) -or (Get-FileHash -LiteralPath $embeddedNode -Algorithm SHA256).Hash -ne $nodeLock.sha256) {
    throw 'Node runtime differs from tools/runtime-lock.json. Supply the pinned node.exe using -NodeExecutable.'
}
if ($SkipBridgeBuild) { throw 'Prebuilt bridge reuse is disabled. Build the pinned runtime from current sources.' }
$nodeVersion = & $embeddedNode --version
if ($nodeVersion -ne $nodeLock.version) { throw 'Node runtime version differs from runtime-lock.json.' }

$compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$framework = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
$wpf = Join-Path $framework 'WPF'

if (-not (Test-Path -LiteralPath $compiler)) {
    throw 'The built-in .NET Framework compiler was not found.'
}

$iconPng = Join-Path $assetDirectory 'spoiler-icon-1024.png'
$iconIco = Join-Path $assetDirectory 'spoiler.ico'
if (-not (Test-Path -LiteralPath $iconPng)) {
    throw 'assets\spoiler-icon-1024.png is required.'
}
if (-not (Test-Path -LiteralPath $iconIco)) {
    throw 'assets\spoiler.ico is required. Run scripts\prepare-assets.ps1 first.'
}

New-Item -ItemType Directory -Force -Path $buildDirectory | Out-Null

$npmCli = Join-Path (Split-Path -Parent $embeddedNode) 'node_modules\npm\bin\npm-cli.js'
if (-not (Test-Path -LiteralPath $npmCli)) {
    throw 'The pinned Node installation must include npm-cli.js.'
}
if ($RestoreDependencies) {
    # npm ci replaces only this project-owned dependency directory. Do not
    # follow a node_modules junction into another checkout.
    $dependencyDirectory = Join-Path $bridgeDirectory 'node_modules'
    if ((Test-Path -LiteralPath $dependencyDirectory) -and
        ((Get-Item -LiteralPath $dependencyDirectory).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw 'Dependency restoration refuses a node_modules reparse point.'
    }
    Push-Location $bridgeDirectory
    try {
        & $embeddedNode $npmCli ci --ignore-scripts --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Locked dependency restoration failed.' }
    }
    finally { Pop-Location }
}

if (-not $SkipTests) {
    Push-Location $bridgeDirectory
    try {
        & $embeddedNode $npmCli test
        if ($LASTEXITCODE -ne 0) {
            throw 'Spoiler bridge tests failed.'
        }
    }
    finally {
        Pop-Location
    }
}

if (-not $SkipBridgeBuild) {
    Push-Location $bridgeDirectory
    try {
        & $embeddedNode $npmCli run build
        if ($LASTEXITCODE -ne 0) {
            throw 'Spoiler bridge build failed.'
        }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $bridgeListener) -or -not (Test-Path -LiteralPath $bridgeAuth)) {
    throw 'The bundled listener/auth bridge files are missing.'
}
if (-not (Test-Path -LiteralPath $embeddedNode)) {
    throw 'C:\Program Files\nodejs\node.exe is required to produce the single-file build.'
}

# The exact canonical text embedded here is parsed strictly by
# EmbeddedRuntime.  The version is a digest of the three ordered entries, so
# every payload change gets a new direct-child cache directory.
$runtimeManifestPath = Join-Path $buildDirectory 'embedded-runtime.manifest'
$runtimePayloads = @(
    [pscustomobject]@{ File = 'node.exe'; Resource = 'Spoiler.Runtime.Node'; Path = $embeddedNode },
    [pscustomobject]@{ File = 'spoiler-bridge.mjs'; Resource = 'Spoiler.Runtime.Bridge.Listener'; Path = $bridgeListener },
    [pscustomobject]@{ File = 'spoiler-auth.mjs'; Resource = 'Spoiler.Runtime.Bridge.Auth'; Path = $bridgeAuth }
)
$runtimeEntryLines = foreach ($payload in $runtimePayloads) {
    $item = Get-Item -LiteralPath $payload.Path
    $sha256 = (Get-FileHash -LiteralPath $payload.Path -Algorithm SHA256).Hash.ToLowerInvariant()
    '{0}|{1}|{2}|{3}' -f $payload.File, $payload.Resource, $item.Length, $sha256
}
$runtimeVersionSeed = ($runtimeEntryLines -join "`n") + "`n"
$runtimeVersionBytes = [System.Text.Encoding]::UTF8.GetBytes($runtimeVersionSeed)
$runtimeVersionHasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $runtimeVersionHash = [System.BitConverter]::ToString($runtimeVersionHasher.ComputeHash($runtimeVersionBytes)).Replace('-', '').ToLowerInvariant()
}
finally {
    $runtimeVersionHasher.Dispose()
}
$runtimeVersion = 'v1-' + $runtimeVersionHash.Substring(0, 16)
$runtimeManifestText = "SpoilerRuntimeManifest/1`nversion=$runtimeVersion`n" + ($runtimeEntryLines -join "`n") + "`n"
[System.IO.File]::WriteAllText($runtimeManifestPath, $runtimeManifestText, [System.Text.UTF8Encoding]::new($false))

$references = @(
    (Join-Path $wpf 'PresentationCore.dll'),
    (Join-Path $wpf 'PresentationFramework.dll'),
    (Join-Path $wpf 'WindowsBase.dll'),
    (Join-Path $wpf 'UIAutomationProvider.dll'),
    (Join-Path $wpf 'UIAutomationTypes.dll'),
    (Join-Path $framework 'System.Xaml.dll'),
    (Join-Path $framework 'System.Drawing.dll'),
    (Join-Path $framework 'System.Windows.Forms.dll'),
    (Join-Path $framework 'System.Runtime.Serialization.dll'),
    (Join-Path $framework 'System.Security.dll')
)

$referenceArguments = $references | ForEach-Object { '/reference:' + $_ }
$resources = @('/resource:' + $iconPng + ',Spoiler.Assets.Icon')
$resources += '/resource:' + $iconIco + ',Spoiler.Assets.TrayIcon'
$resources += '/resource:' + $runtimeManifestPath + ',Spoiler.Runtime.Manifest'
$resources += '/resource:' + $embeddedNode + ',Spoiler.Runtime.Node'
$resources += '/resource:' + $bridgeListener + ',Spoiler.Runtime.Bridge.Listener'
$resources += '/resource:' + $bridgeAuth + ',Spoiler.Runtime.Bridge.Auth'
$avatarMap = @{
    'avatar-dog.png' = 'Spoiler.Assets.Avatar.Dog'
    'avatar-bear-v2.png' = 'Spoiler.Assets.Avatar.Bear'
    'avatar-mountain-v2.png' = 'Spoiler.Assets.Avatar.Mountain'
    'avatar-coffee-v2.png' = 'Spoiler.Assets.Avatar.Coffee'
}
foreach ($asset in $avatarMap.GetEnumerator()) {
    $path = Join-Path $assetDirectory $asset.Key
    if (Test-Path -LiteralPath $path) {
        $resources += '/resource:' + $path + ',' + $asset.Value
    }
}

$sources = Get-ChildItem -LiteralPath $sourceDirectory -Filter '*.cs' |
    Where-Object { $_.Name -ne 'NotificationCaptureService.cs' } |
    ForEach-Object { $_.FullName }
$applicationOutput = Join-Path $buildDirectory 'Spoiler.exe'
$applicationArguments = @(
    '/nologo',
    '/target:winexe',
    '/optimize+',
    '/platform:x86',
    '/utf8output',
    ('/out:' + $applicationOutput),
    ('/win32icon:' + $iconIco),
    ('/win32manifest:' + (Join-Path $projectRoot 'app.manifest'))
) + $referenceArguments + $resources + $sources

& $compiler $applicationArguments
if ($LASTEXITCODE -ne 0) {
    throw 'Spoiler compilation failed.'
}

# Exercise the actual finished winexe, including resource reads, first-use
# extraction, deny-write verification handles, sanitized child environment,
# and the extracted embedded Node executable.
$runtimeSelfTestReport = Join-Path $buildDirectory 'runtime-self-test.txt'
$runtimeSelfTestRoot = Join-Path $buildDirectory 'runtime-self-test-cache'
$runtimeSelfTestProcess = Start-Process `
    -FilePath $applicationOutput `
    -ArgumentList @(
        '--runtime-self-test',
        ('--runtime-self-test-report="' + $runtimeSelfTestReport + '"'),
        ('--runtime-self-test-root="' + $runtimeSelfTestRoot + '"')
    ) `
    -WindowStyle Hidden `
    -PassThru
if (-not $runtimeSelfTestProcess.WaitForExit(45000)) {
    try { $runtimeSelfTestProcess.Kill() } catch { }
    throw 'Embedded runtime self-test exceeded 45 seconds.'
}
$runtimeSelfTestProcess.Refresh()
if ($runtimeSelfTestProcess.ExitCode -ne 0) {
    throw 'Embedded runtime self-test failed. See build\runtime-self-test.txt.'
}
$runtimeSelfTestText = Get-Content -LiteralPath $runtimeSelfTestReport -Raw
if (-not $runtimeSelfTestText.StartsWith('PASS: embedded runtime self-test', [System.StringComparison]::Ordinal) `
    -or $runtimeSelfTestText -notmatch [regex]::Escape('resource=Spoiler.Runtime.Node') `
    -or $runtimeSelfTestText -notmatch [regex]::Escape('resource=Spoiler.Runtime.Bridge.Listener') `
    -or $runtimeSelfTestText -notmatch [regex]::Escape('resource=Spoiler.Runtime.Bridge.Auth') `
    -or $runtimeSelfTestText -notmatch 'node_version=v[0-9]+') {
    throw 'Embedded runtime self-test report is incomplete.'
}

if (-not $SkipTests) {
    # This regression suite is pure pixel geometry and does not create windows
    # or call the native monitor APIs used by the production window hook.
    $workAreaTestOutput = Join-Path $buildDirectory 'Spoiler.WindowWorkAreaTests.exe'
    $workAreaTestArguments = @(
        '/nologo', '/target:exe', '/optimize+', '/platform:x86', '/utf8output',
        ('/out:' + $workAreaTestOutput), '/main:Spoiler.Tests.WindowWorkAreaTests',
        (Join-Path $sourceDirectory 'WindowWorkArea.cs'),
        (Join-Path $testDirectory 'WindowWorkAreaTests.cs')
    )
    & $compiler $workAreaTestArguments
    if ($LASTEXITCODE -ne 0) { throw 'Window work-area test compilation failed.' }
    & $workAreaTestOutput
    if ($LASTEXITCODE -ne 0) { throw 'Window work-area tests failed.' }

    $storeTestOutput = Join-Path $buildDirectory 'Spoiler.StoreTests.exe'
    $storeTestArguments = @(
        '/nologo',
        '/target:exe',
        '/optimize+',
        '/platform:x86',
        '/utf8output',
        ('/out:' + $storeTestOutput),
        ('/main:Spoiler.Tests.StoreTests')
    ) + $referenceArguments + @(
        (Join-Path $sourceDirectory 'Models.cs'),
        (Join-Path $sourceDirectory 'NotificationGrouping.cs'),
        (Join-Path $sourceDirectory 'NotificationStore.cs'),
        (Join-Path $testDirectory 'StoreTests.cs')
    )

    & $compiler $storeTestArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Store test compilation failed.'
    }

    & $storeTestOutput
    if ($LASTEXITCODE -ne 0) {
        throw 'Store tests failed.'
    }

    $locoSourceFiles = @(
        (Join-Path $sourceDirectory 'Models.cs'),
        (Join-Path $sourceDirectory 'IMessageSource.cs'),
        (Join-Path $sourceDirectory 'IReplyMessageSource.cs'),
        (Join-Path $sourceDirectory 'CredentialVault.cs'),
        (Join-Path $sourceDirectory 'EmbeddedRuntime.cs'),
        (Join-Path $sourceDirectory 'MessageCatchUpCursor.cs'),
        (Join-Path $sourceDirectory 'InboxStore.cs'),
        (Join-Path $sourceDirectory 'NotificationStore.cs'),
        (Join-Path $sourceDirectory 'LocoMessageSource.cs'),
        (Join-Path $sourceDirectory 'LocoMessageSource.Inbox.cs'),
        (Join-Path $sourceDirectory 'LocoMessageSource.Authentication.cs'),
        (Join-Path $sourceDirectory 'LocoMessageSource.Replies.cs'),
        (Join-Path $sourceDirectory 'LocoBridgeProtocol.cs'),
        (Join-Path $sourceDirectory 'LocoBridgeLauncher.cs'),
        (Join-Path $sourceDirectory 'LocoJson.cs'),
        (Join-Path $sourceDirectory 'NotificationGrouping.cs')
    )
    $locoTestOutput = Join-Path $buildDirectory 'Spoiler.LocoCoreTests.exe'
    $locoTestArguments = @(
        '/nologo',
        '/target:exe',
        '/optimize+',
        '/platform:x86',
        '/utf8output',
        ('/out:' + $locoTestOutput),
        ('/main:Spoiler.Tests.LocoCoreTests')
    ) + $referenceArguments + $locoSourceFiles + @(
        (Join-Path $testDirectory 'LocoCoreTests.cs')
    )

    & $compiler $locoTestArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'LOCO core test compilation failed.'
    }

    & $locoTestOutput
    if ($LASTEXITCODE -ne 0) {
        throw 'LOCO core tests failed.'
    }

    $roomMetadataTestOutput = Join-Path $buildDirectory 'Spoiler.RoomMetadataTests.exe'
    $roomMetadataTestArguments = @(
        '/nologo', '/target:exe', '/optimize+', '/platform:x86', '/utf8output',
        ('/out:' + $roomMetadataTestOutput), '/main:Spoiler.Tests.RoomMetadataTests'
    ) + $referenceArguments + $locoSourceFiles + @(
        (Join-Path $testDirectory 'RoomMetadataTests.cs')
    )
    & $compiler $roomMetadataTestArguments
    if ($LASTEXITCODE -ne 0) { throw 'Room metadata tests compilation failed.' }
    & $roomMetadataTestOutput
    if ($LASTEXITCODE -ne 0) { throw 'Room metadata tests failed.' }

    $replyTestOutput = Join-Path $buildDirectory 'Spoiler.ReplySourceTests.exe'
    $replyTestArguments = @(
        '/nologo', '/target:exe', '/optimize+', '/platform:x86', '/utf8output',
        ('/out:' + $replyTestOutput), '/main:Spoiler.Tests.ReplySourceTests'
    ) + $referenceArguments + $locoSourceFiles + @(
        (Join-Path $testDirectory 'ReplySourceTests.cs')
    )
    & $compiler $replyTestArguments
    if ($LASTEXITCODE -ne 0) { throw 'Reply source tests compilation failed.' }
    & $replyTestOutput (Join-Path $testDirectory 'fixtures\reply-source-fixture.mjs') $embeddedNode
    if ($LASTEXITCODE -ne 0) { throw 'Reply source tests failed.' }

    $conversationTestOutput = Join-Path $buildDirectory 'Spoiler.ConversationSourceTests.exe'
    $conversationTestArguments = @(
        '/nologo', '/target:exe', '/optimize+', '/platform:x86', '/utf8output',
        ('/out:' + $conversationTestOutput), '/main:Spoiler.Tests.ConversationSourceTests'
    ) + $referenceArguments + $locoSourceFiles + @(
        (Join-Path $testDirectory 'ConversationSourceTests.cs')
    )
    & $compiler $conversationTestArguments
    if ($LASTEXITCODE -ne 0) { throw 'Conversation source tests compilation failed.' }
    & $conversationTestOutput (Join-Path $testDirectory 'fixtures\conversation-source-fixture.mjs') $embeddedNode
    if ($LASTEXITCODE -ne 0) { throw 'Conversation source tests failed.' }

    $inboxTestOutput = Join-Path $buildDirectory 'Spoiler.InboxStoreTests.exe'
    & $compiler /nologo /target:exe /platform:x86 /utf8output ('/out:' + $inboxTestOutput) /main:Spoiler.Tests.InboxStoreTests $referenceArguments `
        (Join-Path $sourceDirectory 'Models.cs') (Join-Path $sourceDirectory 'NotificationGrouping.cs') `
        (Join-Path $sourceDirectory 'MessageCatchUpCursor.cs') (Join-Path $sourceDirectory 'InboxStore.cs') `
        (Join-Path $testDirectory 'InboxStoreTests.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Inbox tests compilation failed.' }
    & $inboxTestOutput
    if ($LASTEXITCODE -ne 0) { throw 'Inbox tests failed.' }

    $productionTestOutput = Join-Path $buildDirectory 'Spoiler.LocoProductionTests.exe'
    & $compiler /nologo /target:exe /platform:x86 /utf8output ('/out:' + $productionTestOutput) $referenceArguments `
        (Join-Path $sourceDirectory 'Models.cs') (Join-Path $sourceDirectory 'IMessageSource.cs') (Join-Path $sourceDirectory 'IReplyMessageSource.cs') `
        (Join-Path $sourceDirectory 'CredentialVault.cs') (Join-Path $sourceDirectory 'EmbeddedRuntime.cs') `
        (Join-Path $sourceDirectory 'MessageCatchUpCursor.cs') (Join-Path $sourceDirectory 'InboxStore.cs') `
        (Join-Path $sourceDirectory 'NotificationStore.cs') (Join-Path $sourceDirectory 'LocoMessageSource.cs') `
        (Join-Path $sourceDirectory 'LocoMessageSource.Inbox.cs') (Join-Path $sourceDirectory 'LocoMessageSource.Authentication.cs') `
        (Join-Path $sourceDirectory 'LocoMessageSource.Replies.cs') `
        (Join-Path $sourceDirectory 'LocoBridgeProtocol.cs') `
        (Join-Path $sourceDirectory 'LocoBridgeLauncher.cs') (Join-Path $sourceDirectory 'LocoJson.cs') `
        (Join-Path $sourceDirectory 'NotificationGrouping.cs') (Join-Path $testDirectory 'LocoProductionTests.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Listener production tests compilation failed.' }
    & $productionTestOutput (Join-Path $testDirectory 'fixtures\loco-production-fixture.mjs') $embeddedNode
    if ($LASTEXITCODE -ne 0) { throw 'Listener production tests failed.' }

    $groupingTestOutput = Join-Path $buildDirectory 'Spoiler.GroupingTests.exe'
    $groupingTestArguments = @(
        '/nologo',
        '/target:exe',
        '/optimize+',
        '/platform:x86',
        '/utf8output',
        ('/out:' + $groupingTestOutput),
        ('/main:Spoiler.Tests.GroupingTests')
    ) + $referenceArguments + @(
        (Join-Path $sourceDirectory 'Models.cs'),
        (Join-Path $sourceDirectory 'NotificationGrouping.cs'),
        (Join-Path $testDirectory 'GroupingTests.cs')
    )

    & $compiler $groupingTestArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Notification grouping test compilation failed.'
    }

    & $groupingTestOutput
    if ($LASTEXITCODE -ne 0) {
        throw 'Notification grouping tests failed.'
    }

    $roomModelTestOutput = Join-Path $buildDirectory 'Spoiler.ChatRoomGroupingTests.exe'
    & $compiler /nologo /target:exe /optimize+ /platform:x86 /utf8output ('/out:' + $roomModelTestOutput) `
        /main:Spoiler.Tests.ChatRoomGroupingTests $referenceArguments `
        (Join-Path $sourceDirectory 'Models.cs') (Join-Path $sourceDirectory 'ChatRoomGrouping.cs') `
        (Join-Path $testDirectory 'ChatRoomGroupingTests.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Chat room model tests compilation failed.' }
    & $roomModelTestOutput
    if ($LASTEXITCODE -ne 0) { throw 'Chat room model tests failed.' }

    $runtimeTestOutput = Join-Path $buildDirectory 'Spoiler.RuntimeTests.exe'
    $runtimeTestArguments = @(
        '/nologo',
        '/target:exe',
        '/optimize+',
        '/platform:x86',
        '/utf8output',
        ('/out:' + $runtimeTestOutput),
        ('/reference:' + $applicationOutput),
        ('/main:Spoiler.Tests.RuntimeTests'),
        (Join-Path $testDirectory 'RuntimeTests.cs')
    )

    & $compiler $runtimeTestArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Embedded runtime contract test compilation failed.'
    }

    $runtimeContractRoot = Join-Path $buildDirectory ('runtime-contract-cache-' + [Guid]::NewGuid().ToString('N'))
    $runtimeContractReport = Join-Path $buildDirectory 'runtime-contract-tests.txt'
    & $runtimeTestOutput $runtimeContractRoot $runtimeContractReport
    if ($LASTEXITCODE -ne 0) {
        throw 'Embedded runtime contract tests failed.'
    }

    $automationReferences = @(
        (Join-Path $wpf 'UIAutomationClient.dll'),
        (Join-Path $wpf 'UIAutomationProvider.dll'),
        (Join-Path $wpf 'UIAutomationTypes.dll')
    ) | ForEach-Object { '/reference:' + $_ }
    $uiTestOutput = Join-Path $buildDirectory 'Spoiler.UiContractTests.exe'
    $uiTestArguments = @(
        '/nologo',
        '/target:exe',
        '/optimize+',
        '/platform:x86',
        '/utf8output',
        ('/out:' + $uiTestOutput),
        ('/reference:' + $applicationOutput),
        ('/main:Spoiler.Tests.UiContractTests')
    ) + $referenceArguments + $automationReferences + @(
        (Join-Path $testDirectory 'UiContractTests.cs'),
        (Join-Path $testDirectory 'RoomUiFixtures.cs')
    )

    & $compiler $uiTestArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Grouped inbox UI contract test compilation failed.'
    }

    & $uiTestOutput
    if ($LASTEXITCODE -ne 0) {
        throw 'Grouped inbox UI contract tests failed.'
    }

    foreach ($suite in @('RoomRenderingTests', 'DesignStateTests', 'InboxUiMergeTests', 'KoreanWrappingTests')) {
        $suiteOutput = Join-Path $buildDirectory ('Spoiler.' + $suite + '.exe')
        $suiteMain = if ($suite -eq 'InboxUiMergeTests') { $suite } else { 'Spoiler.Tests.' + $suite }
        $suiteSources = @((Join-Path $testDirectory ($suite + '.cs')))
        if ($suite -ne 'InboxUiMergeTests') { $suiteSources += (Join-Path $testDirectory 'RoomUiFixtures.cs') }
        $suiteArguments = @('/nologo', '/target:exe', '/optimize+', '/platform:x86', '/utf8output',
            ('/out:' + $suiteOutput), ('/reference:' + $applicationOutput), ('/main:' + $suiteMain)) + $referenceArguments + $automationReferences + $suiteSources
        & $compiler $suiteArguments
        if ($LASTEXITCODE -ne 0) { throw ($suite + ' compilation failed.') }
        & $suiteOutput
        if ($LASTEXITCODE -ne 0) { throw ($suite + ' failed.') }
    }

    $roomPreviewOutput = Join-Path $buildDirectory 'RoomUiPreview.exe'
    $roomPreviewArguments = @(
        '/nologo', '/target:exe', '/optimize+', '/platform:x86', '/utf8output',
        ('/out:' + $roomPreviewOutput), ('/reference:' + $applicationOutput),
        '/main:Spoiler.Tests.RoomUiPreview'
    ) + $referenceArguments + @(
        (Join-Path $testDirectory 'RoomUiFixtures.cs'),
        (Join-Path $testDirectory 'RoomUiPreview.cs')
    )
    & $compiler $roomPreviewArguments
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic room preview compilation failed.' }

}

$provenanceInputs = @(
    (Join-Path $projectRoot 'build.ps1'),
    (Join-Path $projectRoot 'app.manifest'),
    (Join-Path $projectRoot 'tools\runtime-lock.json'),
    (Join-Path $bridgeDirectory 'package.json'),
    (Join-Path $bridgeDirectory 'package-lock.json'),
    (Join-Path $bridgeDirectory 'build.mjs'),
    $iconPng,
    $iconIco
)
$provenanceInputs += foreach ($asset in $avatarMap.Keys) {
    $path = Join-Path $assetDirectory $asset
    if (Test-Path -LiteralPath $path) { $path }
}
$provenanceInputs += Get-ChildItem -LiteralPath $sourceDirectory -Filter '*.cs' | ForEach-Object { $_.FullName }
$provenanceInputs += Get-ChildItem -LiteralPath (Join-Path $bridgeDirectory 'src') -Filter '*.mjs' | ForEach-Object { $_.FullName }
$provenanceEntries = foreach ($path in ($provenanceInputs | Sort-Object)) {
    [pscustomobject]@{
        path = $path.Substring($projectRoot.Length + 1).Replace('\', '/')
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$provenance = [ordered]@{
    schema = 'SpoilerBuildProvenance/1'
    node_version = $nodeVersion
    runtime_version = $runtimeVersion
    runtime_manifest_sha256 = (Get-FileHash -LiteralPath $runtimeManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    payloads = @($runtimeEntryLines)
    dependencies = [ordered]@{ 'agent-messenger' = '2.37.1'; esbuild = '0.28.2' }
    inputs = @($provenanceEntries)
    executable_sha256 = (Get-FileHash -LiteralPath $applicationOutput -Algorithm SHA256).Hash.ToLowerInvariant()
}
[System.IO.File]::WriteAllText((Join-Path $buildDirectory 'build-provenance.json'),
    ($provenance | ConvertTo-Json -Depth 8), [System.Text.UTF8Encoding]::new($false))

$applicationHash = (Get-FileHash -LiteralPath $applicationOutput -Algorithm SHA256).Hash.ToLowerInvariant()
Get-Item -LiteralPath $applicationOutput | Select-Object FullName,Length,LastWriteTime,@{Name='SHA256';Expression={$applicationHash}}
