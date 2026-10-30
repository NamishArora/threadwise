$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$distPath = Join-Path $PSScriptRoot "server\dist"
New-Item -ItemType Directory -Force -Path $distPath | Out-Null

docker run --rm --platform linux/amd64 `
    -v "${repoRoot}:/workspace" `
    -w /workspace/plugins/threadwise `
    golang:1.26.10 `
    go build -mod=mod -trimpath -o server/dist/plugin-linux-amd64 .
if ($LASTEXITCODE -ne 0) {
    throw "Threadwise plugin build failed."
}

$archivePath = Join-Path $PSScriptRoot "threadwise.tar.gz"
docker run --rm --platform linux/amd64 `
    -v "${repoRoot}:/workspace" `
    -w /workspace/plugins/threadwise `
    golang:1.26.10 `
    sh -c "chmod 755 server/dist/plugin-linux-amd64 && tar --mode='u+rwX,go+rX' -czf threadwise.tar.gz -C /workspace/plugins/threadwise plugin.json server"
if ($LASTEXITCODE -ne 0) {
    throw "Threadwise plugin archive creation failed."
}

Write-Output "Plugin package created at $archivePath"
