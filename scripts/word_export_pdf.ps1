param(
  [Parameter(Mandatory = $true)]
  [string]$InputDocx,
  [Parameter(Mandatory = $true)]
  [string]$OutputPdf
)

$ErrorActionPreference = "Stop"

$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0

try {
  $doc = $word.Documents.Open((Resolve-Path $InputDocx).Path, $false, $true)
  try {
    $outPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPdf)
    $doc.ExportAsFixedFormat($outPath, 17)
  }
  finally {
    $doc.Close($false)
  }
}
finally {
  $word.Quit()
}

