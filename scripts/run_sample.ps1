$ErrorActionPreference = "Stop"

$python = "python"
$apiKeyEnv = & $python -c "from lowered_aigc.config.loader import load_config; print(load_config()['llm']['api_key_env'])"

if (-not [Environment]::GetEnvironmentVariable($apiKeyEnv)) {
  Write-Error "Missing LLM API key environment variable: $apiKeyEnv"
}

& $python -m lowered_aigc.cli `
  --docx "[!A]*.docx" `
  --report-file "AIGC*.docx" `
  --out "output\lowered.docx" `
  --report "output\lowered_aigc_report.json"

if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

