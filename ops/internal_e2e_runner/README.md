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
2. **No runner internet route** — runner attaches only to a unique Docker `--internal` network. Exact-host CONNECT and exact-target DB relay sidecars are dual-homed to internal + egress networks.
3. **Defense-in-depth OUTPUT drop** — runner allows only loopback, established/related, the exact CONNECT sidecar IP:3128, and DB relay IP:5432. No public LLM/DB IP is accepted by runner firewall.
4. **Reject** `*.railway.internal`, `postgres-staging`, private/reserved IPs, provider hosts (Meta/Salla/etc.), wildcards/CIDR.
5. **DB URL and identity** — the URL must use `sslmode=require`; the route keeps exact IPv4 DNS pins and requires an explicit SHA-256 SubjectPublicKeyInfo pin for the disposable database certificate.
6. **Probes** — exact LLM CONNECT plus hostname/SNI TLS, Meta/Salla CONNECT rejection, egress-side baseline then runner direct block, and PostgreSQL SSLRequest/TLS with certificate validity + SPKI identity proof. No LLM API request, DB authentication, query, or write.
7. **Evidence** — unsigned `network_evidence.json` for external signer review; container must not self-sign attestation.
8. **Operator** — entrypoint runs only `preflight` (default) or explicit `run`; requires pinned git revision + image label.
9. **Secrets** — ten required read-only files are mounted under `/run/secrets` (including tenant/phone allowlists); sidecars receive none. Other provider keys are explicitly unset.

## DNS TTL limitation

Each sidecar resolves its single public target before serving and verifies that live DNS
exactly equals the configured expected IP set. DNS/IP evidence is valid only for the
short confinement window. If upstream DNS changes, startup fails or the envelope must
be re-run. The launcher uses Docker `--add-host` to append the disposable DB hostname
to the relay IP while preserving base `/etc/hosts`; hostname/SNI semantics remain intact.

## Disposable DB TLS identity

Railway disposable PostgreSQL endpoints may present a private certificate whose public
PKI hostname cannot match the route hostname. The runner therefore requires
`db_tls_spki_sha256` in canonical `sha256:<64 lowercase hex>` form. The probe still
sends PostgreSQL SSLRequest, requires `S`, sends route-host SNI, checks certificate
validity, and accepts identity only when the peer certificate's SubjectPublicKeyInfo
hash exactly matches the configured pin.

Obtain the pin out of band from an operator-reviewed certificate diagnostic. Never
trust or import the presented root dynamically. Certificate key rotation requires a
new reviewed pin and config update before execution; an unplanned rotation fails
closed.

## Layout

```
ops/internal_e2e_runner/
├── Dockerfile
├── README.md
├── run-confined-e2e.ps1
├── contracts/
├── lib/
├── sidecars/
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
  -EvidenceDir path/to/evidence `
  -DatabaseUrlFile path/to/database_url `
  -EvidenceKeyFile path/to/evidence_hmac_key `
  -AttestationKeyFile path/to/attestation_hmac_key `
  -AttestationJsonFile path/to/attestation_json `
  -AttestationSignatureFile path/to/attestation_signature `
  -NetworkConfirmFile path/to/network_confirm `
  -LlmApiKeyFile path/to/anthropic_api_key `
  -TenantAllowlistFile path/to/tenant_allowlist `
  -TestPhoneFile path/to/test_phone `
  -PhoneAllowlistFile path/to/phone_allowlist
```

## External attestation flow

1. Operator runs confined container and copies `network_evidence.json` to host.
2. External signer reviews evidence (rules hash, probes, fingerprints, capability proof).
3. Signer creates PR #662 sandbox attestation with `network_policy=default_deny`.
4. Internal E2E operator consumes attestation via `NAHLA_INTERNAL_E2E_ATTESTATION_JSON`.

The runner reads the PR #662 attestation verification key because that operator requires
it, but network evidence remains unsigned and the runner has no separate external
attestation-signing action. External review/signing remains mandatory.

## Cleanup

The launcher writes evidence directly to the dedicated host evidence directory, then
removes all uniquely named containers and networks in `finally`. Operator failure is
recorded separately and does not convert network evidence into a pass. Disposable
database disposal remains a separate external operator step.

## Current verification limit

Docker Desktop is stopped. Sidecar builds, Docker topology, live DNS equality,
firewall installation, TLS probes, network inspect hashes, and cleanup behavior are
**runtime integration pending**. Static/unit, parser, and dry-plan checks do not prove
runtime confinement.
