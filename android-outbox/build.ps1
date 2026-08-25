[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$repoRoot = Split-Path -Parent $projectRoot
$androidSdk = 'C:\Users\fortytwo\Android\Sdk'
$androidJar = Join-Path $androidSdk 'platforms\android-34\android.jar'
$buildTools = Join-Path $androidSdk 'build-tools\34.0.0'
$r8Jar = Join-Path $projectRoot 'tools\r8-9.3.17.jar'
$buildRoot = Join-Path $projectRoot 'build'
$keystore = Join-Path $projectRoot 'signing\sms-reliable-outbox.jks'
$storePassword = 'sms-reliable-outbox-local-key'
$keyAlias = 'sms-reliable-outbox'

if (-not (Test-Path -LiteralPath $androidJar)) {
    throw "Missing Android platform jar: $androidJar"
}
if (-not (Test-Path -LiteralPath $r8Jar)) {
    throw "Missing R8/D8 tool: $r8Jar"
}

if (Test-Path -LiteralPath $buildRoot) {
    $resolvedBuild = (Resolve-Path -LiteralPath $buildRoot).Path
    $expectedBuild = Join-Path $projectRoot 'build'
    if ($resolvedBuild -ne $expectedBuild) {
        throw "Refusing to clean unexpected path: $resolvedBuild"
    }
    Remove-Item -LiteralPath $resolvedBuild -Recurse -Force
}

$classes = Join-Path $buildRoot 'classes'
$testClasses = Join-Path $buildRoot 'test-classes'
$dex = Join-Path $buildRoot 'dex'
$dist = Join-Path $buildRoot 'dist'
foreach ($path in @($classes, $testClasses, $dex, $dist)) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
}

Write-Output '=== run policy tests ==='
$backoffSource = Join-Path $projectRoot 'src\main\java\com\fortytwoo\smsoutbox\BackoffPolicy.java'
$testSource = Join-Path $projectRoot 'src\test\java\com\fortytwoo\smsoutbox\BackoffPolicyTest.java'
& javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -d $testClasses $backoffSource $testSource
if ($LASTEXITCODE -ne 0) { throw 'Test compilation failed' }
& java -cp $testClasses com.fortytwoo.smsoutbox.BackoffPolicyTest
if ($LASTEXITCODE -ne 0) { throw 'Policy tests failed' }

Write-Output '=== compile Android application ==='
$sources = @(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'src\main\java') -Recurse -Filter '*.java' | ForEach-Object FullName)
& javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -cp $androidJar -d $classes $sources
if ($LASTEXITCODE -ne 0) { throw 'Android compilation failed' }

$programJar = Join-Path $buildRoot 'program.jar'
Push-Location $classes
try {
    & jar cf $programJar .
    if ($LASTEXITCODE -ne 0) { throw 'Class packaging failed' }
} finally {
    Pop-Location
}

Write-Output '=== create DEX ==='
& java -cp $r8Jar com.android.tools.r8.D8 `
    --min-api 26 --lib $androidJar --output $dex $programJar
if ($LASTEXITCODE -ne 0) { throw 'D8 failed' }

Write-Output '=== package APK ==='
$resourceApk = Join-Path $buildRoot 'resources.apk'
& (Join-Path $buildTools 'aapt2.exe') link `
    -o $resourceApk `
    -I $androidJar `
    --manifest (Join-Path $projectRoot 'AndroidManifest.xml') `
    --min-sdk-version 26 `
    --target-sdk-version 33 `
    --version-code 4 `
    --version-name 1.1.0 `
    --auto-add-overlay
if ($LASTEXITCODE -ne 0) { throw 'aapt2 link failed' }

$unsignedApk = Join-Path $buildRoot 'sms-reliable-outbox-unsigned.apk'
Copy-Item -LiteralPath $resourceApk -Destination $unsignedApk
Push-Location $dex
try {
    & jar uf $unsignedApk classes.dex
    if ($LASTEXITCODE -ne 0) { throw 'Unable to add classes.dex' }
} finally {
    Pop-Location
}

$alignedApk = Join-Path $buildRoot 'sms-reliable-outbox-aligned.apk'
& (Join-Path $buildTools 'zipalign.exe') -f 4 $unsignedApk $alignedApk
if ($LASTEXITCODE -ne 0) { throw 'zipalign failed' }

Write-Output '=== sign and verify APK ==='
if (-not (Test-Path -LiteralPath $keystore)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $keystore) -Force | Out-Null
    & keytool -genkeypair -keystore $keystore -storetype JKS `
        -storepass $storePassword -keypass $storePassword -alias $keyAlias `
        -keyalg RSA -keysize 3072 -validity 10000 `
        -dname 'CN=Fortytwoo SMS Reliable Outbox,O=Fortytwoo,C=CN' -noprompt
    if ($LASTEXITCODE -ne 0) { throw 'Key generation failed' }
}

$signedApk = Join-Path $dist 'sms-reliable-outbox-v1.1.0.apk'
& (Join-Path $buildTools 'apksigner.bat') sign `
    --ks $keystore `
    --ks-key-alias $keyAlias `
    --ks-pass "pass:$storePassword" `
    --key-pass "pass:$storePassword" `
    --out $signedApk `
    $alignedApk
if ($LASTEXITCODE -ne 0) { throw 'APK signing failed' }
& (Join-Path $buildTools 'apksigner.bat') verify --verbose --print-certs $signedApk
if ($LASTEXITCODE -ne 0) { throw 'APK verification failed' }

$hash = (Get-FileHash -LiteralPath $signedApk -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "path=$signedApk"
Write-Output "sha256=$hash"

Write-Output '=== package Magisk systemizer payload ==='
$moduleStage = Join-Path $buildRoot 'magisk-module'
$moduleSystemApp = Join-Path $moduleStage 'system\priv-app\SmsReliableOutbox'
New-Item -ItemType Directory -Path $moduleSystemApp -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot 'magisk\module.prop') -Destination $moduleStage
Copy-Item -LiteralPath (Join-Path $projectRoot 'magisk\service.sh') -Destination $moduleStage
Copy-Item -LiteralPath $signedApk -Destination (Join-Path $moduleSystemApp 'SmsReliableOutbox.apk')
$moduleZip = Join-Path $dist 'sms-reliable-outbox-systemizer-v1.1.0.zip'
Compress-Archive -Path (Join-Path $moduleStage '*') -DestinationPath $moduleZip -Force
$moduleHash = (Get-FileHash -LiteralPath $moduleZip -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "module_path=$moduleZip"
Write-Output "module_sha256=$moduleHash"
