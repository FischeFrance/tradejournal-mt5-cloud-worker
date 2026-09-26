#Requires -Version 5.1

function Resolve-TradeJournalMt5Compiler {
  [CmdletBinding()]
  param(
    [string]$InstalledRoot = 'C:\Program Files\MetaTrader 5',
    [string]$TemplateRoot = 'C:\TradeJournal\mt5-template',
    [scriptblock]$SignatureReader = {
      param([string]$Path)
      Get-AuthenticodeSignature -LiteralPath $Path
    },
    [scriptblock]$VersionReader = {
      param([string]$Path)
      (Get-Item -LiteralPath $Path -ErrorAction Stop).VersionInfo.FileVersion
    }
  )

  $templateExists = Test-Path -LiteralPath $TemplateRoot -PathType Container
  $compilerRoot = if ($templateExists) { $TemplateRoot } else { $InstalledRoot }
  $compilerSource = if ($templateExists) { 'trusted_template' } else { 'installed_bootstrap' }

  if (-not (Test-Path -LiteralPath $compilerRoot -PathType Container)) {
    throw 'MT5 compiler root is missing.'
  }
  $rootItem = Get-Item -LiteralPath $compilerRoot -ErrorAction Stop
  if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'MT5 compiler root cannot be a reparse point.'
  }

  $editor = Join-Path $compilerRoot 'MetaEditor64.exe'
  $terminal = Join-Path $compilerRoot 'terminal64.exe'
  $missing = @(
    foreach ($path in @($editor, $terminal)) {
      if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $path }
    }
  )
  if ($missing.Count -ne 0) {
    if ($templateExists) {
      throw 'Trusted MT5 template compiler pair is incomplete.'
    }
    throw 'Bootstrap MT5 compiler pair is incomplete.'
  }

  foreach ($path in @($editor, $terminal)) {
    $item = Get-Item -LiteralPath $path -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw 'MT5 compiler binaries cannot be reparse points.'
    }
    $signature = & $SignatureReader $path
    $certificate = if ($null -eq $signature) { $null } else { $signature.SignerCertificate }
    $subject = if ($null -eq $certificate) { '' } else { [string]$certificate.Subject }
    if (
      $null -eq $signature -or
      [string]$signature.Status -ne 'Valid' -or
      $subject -notlike '*CN=MetaQuotes Ltd.*' -or
      $subject -notlike '*O=MetaQuotes Ltd.*'
    ) {
      throw 'MT5 compiler signature is invalid.'
    }
  }

  $editorVersion = [string](& $VersionReader $editor)
  $terminalVersion = [string](& $VersionReader $terminal)
  try {
    $null = [Version]::Parse($editorVersion)
    $null = [Version]::Parse($terminalVersion)
  } catch {
    throw 'MT5 compiler version is invalid.'
  }
  if (-not [string]::Equals($editorVersion, $terminalVersion, [StringComparison]::Ordinal)) {
    throw 'MetaEditor and terminal versions do not match.'
  }

  [pscustomobject]@{
    Source = $compilerSource
    Root = $rootItem.FullName
    Editor = (Get-Item -LiteralPath $editor -ErrorAction Stop).FullName
    Terminal = (Get-Item -LiteralPath $terminal -ErrorAction Stop).FullName
    Version = $editorVersion
    EditorSha256 = (Get-FileHash -LiteralPath $editor -Algorithm SHA256).Hash.ToLowerInvariant()
    TerminalSha256 = (Get-FileHash -LiteralPath $terminal -Algorithm SHA256).Hash.ToLowerInvariant()
  }
}
