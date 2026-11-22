param([switch]$SkipSubtitles)

$ErrorActionPreference = 'Stop'
$revision = '5ceeeb388eef5423640b7db157706274bf1b8165'
$integration = Join-Path $PSScriptRoot 'tools/freecut'
$source = Join-Path $integration 'upstream'
New-Item -Path $integration -ItemType Directory -Force | Out-Null
if (!(Test-Path -LiteralPath (Join-Path $source '.git'))) {
    git init --quiet $source
    if ($LASTEXITCODE) { throw 'Freecut checkout could not be created.' }
}
git -C $source fetch --quiet --depth=1 https://github.com/walterlow/freecut.git $revision
if ($LASTEXITCODE) { throw 'Freecut could not be downloaded.' }
git -C $source checkout --quiet --detach $revision
if ($LASTEXITCODE) { throw 'The pinned Freecut revision could not be checked out.' }
Push-Location -LiteralPath $source
try {
    npm.cmd ci --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE) { throw 'Freecut dependencies could not be installed.' }
    npm.cmd run build
    if ($LASTEXITCODE) { throw 'Freecut could not be built.' }
} finally {
    Pop-Location
}
$dist = Join-Path $integration 'dist'
New-Item -Path $dist -ItemType Directory -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $source 'dist') | Copy-Item -Destination $dist -Recurse -Force
Copy-Item -LiteralPath (Join-Path $source 'LICENSE') -Destination $integration -Force
if (!$SkipSubtitles) {
    $alignment = Join-Path $integration 'align'
    py -3.12 -m venv --system-site-packages $alignment
    if ($LASTEXITCODE) { throw 'Install Python 3.12 for stable-ts.' }
    $python = Join-Path $alignment 'Scripts/python.exe'
    & $python -m pip install 'setuptools<81' wheel
    if ($LASTEXITCODE) { throw 'Subtitle build dependencies could not be installed.' }
    & $python -m pip install --no-build-isolation 'stable-ts==2.19.1' 'openai-whisper==20240930' 'torch==2.4.1' 'torchaudio==2.4.1'
    if ($LASTEXITCODE) { throw 'stable-ts could not be installed.' }
    & $python -c "import stable_whisper"
    if ($LASTEXITCODE) { throw 'stable-ts could not load the local audio runtime.' }
}
