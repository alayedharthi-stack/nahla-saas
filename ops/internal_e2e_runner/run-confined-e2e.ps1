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
    [string]$PinnedRevision = "",
    [ValidateSet("preflight", "run")]
    [string]$Command = "preflight",
    [string]$ScenariosPath = "",
    [string]$DatabaseUrlFile = "",
    [string]$EvidenceKeyFile = "",
    [string]$AttestationKeyFile = "",
    [string]$AttestationJsonFile = "",
    [string]$AttestationSignatureFile = "",
    [string]$NetworkConfirmFile = "",
    [string]$LlmApiKeyFile = "",
    [string]$TenantAllowlistFile = "",
    [string]$TestPhoneFile = "",
    [string]$PhoneAllowlistFile = "",
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
if (-not $PinnedRevision) {
    $PinnedRevision = (& git -C $RepoRoot rev-parse HEAD).Trim()
}
$ImageLabel = "nahla-internal-e2e-confined:$PinnedRevision"
$SidecarImage = "nahla-internal-e2e-sidecars:$PinnedRevision"
$secretFiles = [ordered]@{
    database_url = $DatabaseUrlFile
    evidence_hmac_key = $EvidenceKeyFile
    attestation_hmac_key = $AttestationKeyFile
    attestation_json = $AttestationJsonFile
    attestation_signature = $AttestationSignatureFile
    network_confirm = $NetworkConfirmFile
    llm_api_key = $LlmApiKeyFile
    tenant_allowlist = $TenantAllowlistFile
    test_phone = $TestPhoneFile
    phone_allowlist = $PhoneAllowlistFile
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
$operatorArgs = @("preflight")
if ($Command -eq "run") {
    if (-not $ScenariosPath) { throw "run_requires_scenarios_path" }
    $operatorArgs = @("run", "--scenarios", $ScenariosPath)
}

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
        required_secret_files = @($secretFiles.Keys)
        resource_limits = @{
            cpus = "2.0"
            memory = "4g"
            pids = 256
        }
    }
    default_command = @("preflight")
    operator_command = $operatorArgs
    topology = @{
        runner_networks = @("unique_internal_only")
        connect_proxy_networks = @("unique_internal", "unique_egress")
        db_relay_networks = @("unique_internal", "unique_egress")
    }
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

if ($PinnedRevision -notmatch "^[0-9a-f]{40}$") { throw "full_pinned_revision_required" }
$headSha = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($headSha -ne $PinnedRevision) { throw "checkout_revision_mismatch" }
if (& git -C $RepoRoot status --porcelain) { throw "dirty_worktree_rejected" }
foreach ($name in $secretFiles.Keys) {
    if (-not $secretFiles[$name] -or -not (Test-Path -LiteralPath $secretFiles[$name] -PathType Leaf)) {
        throw "required_secret_file_missing:$name"
    }
}
if ($Command -eq "run" -and -not (Test-Path -LiteralPath $ScenariosPath -PathType Leaf)) {
    throw "scenarios_file_missing"
}

New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null

$config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
if ($config.pinned_revision -ne $PinnedRevision) { throw "config_revision_mismatch" }
if ($config.image_label -ne $ImageLabel) { throw "config_image_label_mismatch" }
$suffix = [Guid]::NewGuid().ToString("N").Substring(0, 10)
$internalNetwork = "nahla-e2e-internal-$suffix"
$egressNetwork = "nahla-e2e-egress-$suffix"
$proxyName = "nahla-e2e-proxy-$suffix"
$relayName = "nahla-e2e-relay-$suffix"
$runnerName = "nahla-e2e-runner-$suffix"
$createdContainers = [System.Collections.Generic.List[string]]::new()
$createdNetworks = [System.Collections.Generic.List[string]]::new()
$exitCode = 2

try {
    & docker build -f $Dockerfile -t $ImageLabel --build-arg "NAHLA_PINNED_REVISION=$PinnedRevision" $RepoRoot
    if ($LASTEXITCODE) { throw "runner_image_build_failed" }
    & docker build -f (Join-Path $RunnerRoot "sidecars\Dockerfile") -t $SidecarImage --build-arg "NAHLA_PINNED_REVISION=$PinnedRevision" $RepoRoot
    if ($LASTEXITCODE) { throw "sidecar_image_build_failed" }
    $runnerImageId = (& docker image inspect --format "{{.Id}}" $ImageLabel).Trim()
    $sidecarImageId = (& docker image inspect --format "{{.Id}}" $SidecarImage).Trim()
    if ($runnerImageId -notmatch "^sha256:[0-9a-f]{64}$" -or
        $sidecarImageId -notmatch "^sha256:[0-9a-f]{64}$") {
        throw "immutable_image_content_digest_missing"
    }

    & docker network create --internal --subnet "172.30.0.0/24" $internalNetwork
    if ($LASTEXITCODE) { throw "internal_network_create_failed" }
    $createdNetworks.Add($internalNetwork)
    & docker network create $egressNetwork
    if ($LASTEXITCODE) { throw "egress_network_create_failed" }
    $createdNetworks.Add($egressNetwork)

    $llmIpArgs = @($config.llm_host_ips | ForEach-Object { @("--expected-ip", $_) })
    $dbIpArgs = @($config.db_proxy_ips | ForEach-Object { @("--expected-ip", $_) })
    & docker create --name $proxyName --network $internalNetwork --ip $config.connect_proxy_ip `
        --read-only --tmpfs /tmp --security-opt no-new-privileges:true `
        -v "${EvidenceDir}:/evidence:rw" $SidecarImage python connect_proxy.py `
        --allowed-host $config.llm_host @llmIpArgs
    if ($LASTEXITCODE) { throw "connect_proxy_create_failed" }
    $createdContainers.Add($proxyName)
    & docker network connect $egressNetwork $proxyName
    if ($LASTEXITCODE) { throw "connect_proxy_egress_attach_failed" }
    & docker start $proxyName | Out-Null
    if ($LASTEXITCODE) { throw "connect_proxy_start_failed" }

    & docker create --name $relayName --network $internalNetwork --ip $config.db_relay_ip `
        --read-only --tmpfs /tmp --security-opt no-new-privileges:true `
        -v "${EvidenceDir}:/evidence:rw" $SidecarImage python db_relay.py `
        --target-host $config.db_proxy_host --target-port $config.db_proxy_port @dbIpArgs
    if ($LASTEXITCODE) { throw "db_relay_create_failed" }
    $createdContainers.Add($relayName)
    & docker network connect $egressNetwork $relayName
    if ($LASTEXITCODE) { throw "db_relay_egress_attach_failed" }
    & docker start $relayName | Out-Null
    if ($LASTEXITCODE) { throw "db_relay_start_failed" }
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $proxyRunning = (& docker inspect -f "{{.State.Running}}" $proxyName 2>$null) -eq "true"
        $relayRunning = (& docker inspect -f "{{.State.Running}}" $relayName 2>$null) -eq "true"
        $proxyEvidence = Join-Path $EvidenceDir "connect-proxy.jsonl"
        $relayEvidence = Join-Path $EvidenceDir "db-relay.jsonl"
        if ($proxyRunning -and $relayRunning -and
            (Test-Path $proxyEvidence) -and (Test-Path $relayEvidence) -and
            (Select-String -Quiet "startup_dns_verified" $proxyEvidence) -and
            (Select-String -Quiet "startup_dns_verified" $relayEvidence)) {
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "sidecar_readiness_or_dns_verification_failed" }

    # Establish controls are externally reachable from an egress namespace.
    $baseline = & docker exec $proxyName python -c "import json,socket; targets=[('1.1.1.1',443),('8.8.8.8',443)]; [(lambda s:s.close())(socket.create_connection(t,5)) for t in targets]; print(json.dumps({'namespace':'connect_proxy_egress','reachable':[f'{h}:{p}' for h,p in targets]}))"
    if ($LASTEXITCODE) { throw "egress_control_baseline_failed" }
    $baseline | Set-Content -Encoding utf8 -LiteralPath (Join-Path $EvidenceDir "egress-control-baseline.json")

    $runArgs = @("create", "--name", $runnerName, "--network", $internalNetwork,
        "--cap-add", "NET_ADMIN", "--security-opt", "no-new-privileges:true",
        "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/run",
        "--cpus", "2.0", "--memory", "4g", "--pids-limit", "256",
        "--add-host", "$($config.db_proxy_host):$($config.db_relay_ip)",
        "-v", "${EvidenceDir}:/evidence:rw",
        "-v", "$($ConfigPath):/run/config/runner_config.json:ro",
        "-e", "NAHLA_INTERNAL_E2E_IMAGE_DIGEST_INPUT=$runnerImageId",
        "-e", "NAHLA_INTERNAL_E2E_PINNED_REVISION=$PinnedRevision",
        "-e", "NAHLA_IMAGE_LABEL_REVISION=$PinnedRevision")
    foreach ($name in $secretFiles.Keys) {
        $runArgs += @("-v", "$($secretFiles[$name]):/run/secrets/${name}:ro")
    }
    if ($Command -eq "run") {
        $runArgs += @("-v", "${ScenariosPath}:/run/scenarios/scenarios.json:ro")
        $operatorArgs = @("run", "--scenarios", "/run/scenarios/scenarios.json")
    }
    $runArgs += @($ImageLabel) + $operatorArgs
    $createdContainers.Add($runnerName)
    & docker @runArgs
    if ($LASTEXITCODE) { throw "runner_container_create_failed" }
    $inspect = [ordered]@{
        runner_image = (& docker image inspect $ImageLabel | ConvertFrom-Json)
        sidecar_image = (& docker image inspect $SidecarImage | ConvertFrom-Json)
        runner = (& docker inspect $runnerName | ConvertFrom-Json)
        connect_proxy = (& docker inspect $proxyName | ConvertFrom-Json)
        db_relay = (& docker inspect $relayName | ConvertFrom-Json)
        internal_network = (& docker network inspect $internalNetwork | ConvertFrom-Json)
        egress_network = (& docker network inspect $egressNetwork | ConvertFrom-Json)
    }
    $inspect | ConvertTo-Json -Depth 30 | Set-Content -Encoding utf8 -LiteralPath (Join-Path $EvidenceDir "docker-inspect.json")
    & docker start -a $runnerName
    $exitCode = $LASTEXITCODE
}
finally {
    foreach ($container in $createdContainers) { & docker rm -f $container 2>$null | Out-Null }
    foreach ($network in $createdNetworks) { & docker network rm $network 2>$null | Out-Null }
}
Write-Host "Evidence preserved at: $EvidenceDir"
exit $exitCode
