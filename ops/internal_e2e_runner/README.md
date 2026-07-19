# Off-Railway confined internal E2E runner

Artifacts-only network-confinement envelope for PR #662 internal conversational E2E.
Railway cannot enforce outbound destination ACLs and containers lack `NET_ADMIN`, so
honest `network_policy=default_deny` evidence must be produced off-Railway in a
Linux container with kernel firewall rules.

## Status

| Layer | Status |
|-------|--------|
| Static config validation | Implemented + unit tested |
| Evidence schema + assembler | Implemented + unit tested |
| PowerShell launcher dry-run | Implemented + parse checked |
| Container firewall + probes | Artifacts ready |
| Runtime iptables proof | **Pending** — Docker daemon stopped; no container run performed |

## Contract summary

1. **Fail closed** without `CAP_NET_ADMIN` or if iptables/nft is unavailable.
2. **Resolve before drop** — explicit hostname→IP pinning in config; `/etc/hosts` pinning after resolution.
3. **OUTPUT default-drop** — allow only loopback, established/related, initial DNS resolver, one LLM host `:443`, one disposable DB proxy host/port.
4. **Reject** `*.railway.internal`, `postgres-staging`, private/reserved IPs, provider hosts (Meta/Salla/etc.), wildcards/CIDR.
5. **DB URL** must use `sslmode=require`; evidence stores fingerprint only.
6. **Probes** — TCP/TLS reachability only; no LLM requests or DB writes.
7. **Evidence** — unsigned `network_evidence.json` for external signer review; container must not self-sign attestation.
8. **Operator** — entrypoint runs only `preflight` (default) or explicit `run`; requires pinned git revision + image label.
9. **Secrets** — Docker secrets / mounted files; never CLI args or image layers.

## DNS TTL limitation

`/etc/hosts` pinning does not track upstream TTL changes. Evidence is valid only for the
confinement window between `started_at_utc` and `completed_at_utc`. If pinned IPs drift
before external attestation, the envelope must be re-run.

## Layout

```
ops/internal_e2e_runner/
├── Dockerfile
├── README.md
├── run-confined-e2e.ps1
├── contracts/
├── lib/
└── scripts/
```

## Host launcher (dry-run default)

```powershell
./ops/internal_e2e_runner/run-confined-e2e.ps1 -PrintPlan
```

Actual execution requires an explicit confirm token and a running Docker daemon:

```powershell
./ops/internal_e2e_runner/run-confined-e2e.ps1 `
  -ConfirmToken CONFIRM_INTERNAL_E2E_CONFINED_RUN `
  -ConfigPath path/to/runner_config.json `
  -EvidenceDir path/to/evidence
```

## External attestation flow

1. Operator runs confined container and copies `network_evidence.json` to host.
2. External signer reviews evidence (rules hash, probes, fingerprints, capability proof).
3. Signer creates PR #662 sandbox attestation with `network_policy=default_deny`.
4. Internal E2E operator consumes attestation via `NAHLA_INTERNAL_E2E_ATTESTATION_JSON`.

The confined runner never holds or uses the attestation signing key.

## Cleanup

Launcher uses `docker run --rm`. Host must copy evidence before container exit.
Disposable database disposal remains a separate external operator step.
