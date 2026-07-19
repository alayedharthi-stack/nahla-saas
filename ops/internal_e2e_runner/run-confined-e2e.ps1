#Requires -Version 5.1
<#
.SYNOPSIS
  Host-side launcher for the off-Railway confined internal E2E runner.

.DESCRIPTION
  Default mode is dry-run / print-plan. Actual execution requires -ConfirmToken.
  Detects Docker daemon unavailability cleanly and never starts Docker itself.
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [string]$EvidenceDir = "",
    [string]$ImageLabel = "",
    [string]$PinnedRevision = "",
    [string]$ConfirmToken = "",
    [switch]$PrintPlan
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptRoot "..\..")).Path
$RunnerRoot = Join-Path $RepoRoot "ops\internal_e2e_runner"
$Dockerfile = Join-Path $RunnerRoot "Dockerfile"
$DefaultConfig = Join-Path $RunnerRoot "contracts\runner_config.example.json"

if (-not $ConfigPath) { $ConfigPath = $DefaultConfig }
if (-not $EvidenceDir) { $EvidenceDir = Join-Path $RepoRoot ".internal-e2e-evidence" }
if (-not $ImageLabel) { $ImageLabel = "nahla-internal-e2e-confined:local" }
if (-not $PinnedRevision) {
    $PinnedRevision = (& git -C $RepoRoot rev-parse --short=12 HEAD).Trim()
}

function Test-DockerDaemonAvailable {
    try {
        $null = & docker info 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Get-DockerUnavailableMessage {
    return @{
        ok = $false
        mode = "dry_run"
        blocker = "docker_daemon_unavailable"
        message = "Docker daemon is not running. Start Docker Desktop before an actual run."
    } | ConvertTo-Json -Compress
}

$dockerAvailable = Test-DockerDaemonAvailable
$dryRun = -not $ConfirmToken -or $PrintPlan

$plan = [ordered]@{
    mode = $(if ($dryRun) { "dry_run" } else { "execute" })
    docker_daemon_available = $dockerAvailable
    image_label = $ImageLabel
    pinned_revision = $PinnedRevision
    config_path = (Resolve-Path $ConfigPath).Path
    evidence_dir = $EvidenceDir
    dockerfile = $Dockerfile
    security = @{
        cap_add = @("NET_ADMIN")
        security_opt = @("no-new-privileges:true")
        read_only = $true
        tmpfs = @("/tmp", "/run")
        mounts = @("${EvidenceDir}:/evidence:rw")
        secrets = @(
            "database_url",
            "attestation_hmac_key",
            "evidence_hmac_key",
            "attestation_json",
            "attestation_signature"
        )
        resource_limits = @{
            cpus = "2.0"
            memory = "4g"
            pids = 256
        }
    }
    default_command = @("preflight")
    runtime_verification = "pending_docker_daemon_stopped_or_not_executed"
    cleanup = "remove_container_and_network_after_host_copies_evidence"
    db_disposal = "external_operator_responsibility"
}

if ($dryRun) {
    $plan | ConvertTo-Json -Depth 8
    if (-not $dockerAvailable) {
        Write-Host (Get-DockerUnavailableMessage)
    }
    exit 0
}

if (-not $dockerAvailable) {
    Write-Error (Get-DockerUnavailableMessage)
    exit 2
}

if ($ConfirmToken -ne "CONFIRM_INTERNAL_E2E_CONFINED_RUN") {
    Write-Error "confirm_token_invalid"
    exit 2
}

New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null

$buildArgs = @(
    "build",
    "-f", $Dockerfile,
    "-t", $ImageLabel,
    "--label", "nahla.pinned_revision=$PinnedRevision",
    $RepoRoot
)

Write-Host "Building confined runner image..."
& docker @buildArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$runArgs = @(
    "run",
    "--rm",
    "--name", "nahla-internal-e2e-confined",
    "--cap-add", "NET_ADMIN",
    "--security-opt", "no-new-privileges:true",
    "--read-only",
    "--tmpfs", "/tmp",
    "--tmpfs", "/run",
    "--cpus", "2.0",
    "--memory", "4g",
    "--pids-limit", "256",
    "-v", "${EvidenceDir}:/evidence:rw",
    "-v", "${ConfigPath}:/run/config/runner_config.json:ro",
    "-e", "NAHLA_INTERNAL_E2E_IMAGE_DIGEST_INPUT=$ImageLabel@$PinnedRevision",
    "-e", "NAHLA_INTERNAL_E2E_PINNED_REVISION=$PinnedRevision",
    $ImageLabel,
    "preflight"
)

Write-Host "Running confined preflight envelope..."
& docker @runArgs
$exitCode = $LASTEXITCODE

Write-Host "Evidence directory: $EvidenceDir"
Write-Host "Cleanup: container removed via --rm after evidence copy on host."
exit $exitCode
