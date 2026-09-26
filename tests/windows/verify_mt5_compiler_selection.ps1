#Requires -Version 5.1
param(
  [Parameter(Mandatory = $true)][string]$RepositoryRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $RepositoryRoot 'scripts\windows\mt5-compiler-selection.ps1')

$root = Join-Path ([IO.Path]::GetTempPath()) ('tj-compiler-selector-' + [Guid]::NewGuid().ToString('N'))
$installed = Join-Path $root 'installed'
$template = Join-Path $root 'template'
$versions = @{}
$validSignature = {
  param([string]$Path)
  [pscustomobject]@{
    Status = 'Valid'
    SignerCertificate = [pscustomobject]@{
      Subject = 'CN=MetaQuotes Ltd., O=MetaQuotes Ltd., S=Lemesos, C=CY'
    }
  }
}
$versionReader = {
  param([string]$Path)
  $versions[(Get-Item -LiteralPath $Path -ErrorAction Stop).FullName]
}.GetNewClosure()

function Add-FakeCompilerPair {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Version
  )
  New-Item -ItemType Directory -Path $Path -Force | Out-Null
  foreach ($name in @('MetaEditor64.exe', 'terminal64.exe')) {
    $binary = Join-Path $Path $name
    [IO.File]::WriteAllText($binary, "$name-$Version", [Text.UTF8Encoding]::new($false))
    $versions[(Get-Item -LiteralPath $binary).FullName] = $Version
  }
}

try {
  Add-FakeCompilerPair -Path $installed -Version '5.0.0.6063'
  Add-FakeCompilerPair -Path $template -Version '5.0.0.6182'

  $selected = Resolve-TradeJournalMt5Compiler `
    -InstalledRoot $installed `
    -TemplateRoot $template `
    -SignatureReader $validSignature `
    -VersionReader $versionReader
  if ($selected.Source -ne 'trusted_template' -or $selected.Version -ne '5.0.0.6182') {
    throw 'The current trusted template was not selected.'
  }

  $templateTerminal = (Get-Item -LiteralPath (Join-Path $template 'terminal64.exe')).FullName
  $versions[$templateTerminal] = '5.0.0.6063'
  try {
    $null = Resolve-TradeJournalMt5Compiler `
      -InstalledRoot $installed `
      -TemplateRoot $template `
      -SignatureReader $validSignature `
      -VersionReader $versionReader
    throw 'A mismatched template compiler pair was accepted.'
  } catch {
    if ($_.Exception.Message -notlike '*versions do not match*') { throw }
  }

  $versions[$templateTerminal] = '5.0.0.6182'
  Remove-Item -LiteralPath (Join-Path $template 'MetaEditor64.exe') -Force
  try {
    $null = Resolve-TradeJournalMt5Compiler `
      -InstalledRoot $installed `
      -TemplateRoot $template `
      -SignatureReader $validSignature `
      -VersionReader $versionReader
    throw 'An incomplete trusted template compiler pair was accepted.'
  } catch {
    if ($_.Exception.Message -notlike '*template compiler pair is incomplete*') { throw }
  }

  Remove-Item -LiteralPath $template -Recurse -Force
  $bootstrap = Resolve-TradeJournalMt5Compiler `
    -InstalledRoot $installed `
    -TemplateRoot $template `
    -SignatureReader $validSignature `
    -VersionReader $versionReader
  if ($bootstrap.Source -ne 'installed_bootstrap' -or $bootstrap.Version -ne '5.0.0.6063') {
    throw 'The clean bootstrap compiler pair was not selected.'
  }

  Remove-Item -LiteralPath (Join-Path $installed 'MetaEditor64.exe') -Force
  try {
    $null = Resolve-TradeJournalMt5Compiler `
      -InstalledRoot $installed `
      -TemplateRoot $template `
      -SignatureReader $validSignature `
      -VersionReader $versionReader
    throw 'An incomplete bootstrap compiler pair was accepted.'
  } catch {
    if ($_.Exception.Message -notlike '*Bootstrap MT5 compiler pair is incomplete*') { throw }
  }
} finally {
  if (Test-Path -LiteralPath $root) {
    Remove-Item -LiteralPath $root -Recurse -Force
  }
}

Write-Output 'MT5 compiler selector tests passed.'
