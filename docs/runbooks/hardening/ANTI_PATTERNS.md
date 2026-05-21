# Build / Deploy Anti-Patterns

> Concrete things we've stepped on. New ones go at the bottom; nothing
> in this file is theoretical — each entry comes from an actual incident.

---

## A1. NEVER commit `dashboard/node_modules`

**Discovered**: 2026-05-21, ~01:30 KSA.
**Symptom**:

```
Failed to build an image. Please check the build logs for more details.
resolve image config for docker-image://ghcr.io/railwayapp/railpack-frontend:v0.23.0
write /var/lib/buildkit/runc-overlayfs/containerdmeta.db: no space left on device
```

(observed on `creative-intuition` Railway service deployment after the
2026-05-20 DB cutover.)

**Root cause**: `dashboard/node_modules/` was tracked in git via
commit `02e7f0e8 AI Sales Agent progress`. **9,519 files / 116.06 MB**
inside it, including:

| File | Size | Why it hurts |
|---|---:|---|
| `@esbuild/win32-x64/esbuild.exe` | 9.6 MB | **Windows-only PE binary** — 100 % dead weight on Linux Railway runners |
| `typescript/lib/typescript.js` | 8.9 MB | bundled twice (cjs + umd) elsewhere too |
| `lucide-react/dist/**/*.js.map` | 10+ MB across 3 source maps | source maps that shouldn't ship at all |
| `@rollup/rollup-win32-x64-*/rollup.win32-x64-*.node` | 4.4 MB | another Windows-only native binary |
| `vite/dist/node/chunks/dep-*.js` | 2 MB | committed, then re-installed at build time anyway |

Even though `.dockerignore` excluded `dashboard/node_modules` so they
**did not reach the final image**, Railway's build sandbox `git clone`
brings them in **before** `.dockerignore` is consulted. With buildkit
overlayfs metadata + the npm install step on top, the sandbox disk
filled up and the build failed with `no space left on device`.

**Fix**: commit `42d054ff infra(build): stop tracking dashboard node_modules`.

```pwsh
git rm --cached -r dashboard/node_modules/
git commit -m "infra(build): stop tracking dashboard node_modules"
git push origin main
```

Tracked content went from **133.79 MB / 10,565 files** to
**17.73 MB / 1,046 files** (–86.8 %). Subsequent
`creative-intuition` build succeeded; image timestamp
`2026-05-20T22:53:31Z`.

**Prevention** (now in place):

1. `dashboard/.gitignore` already has `node_modules` — that prevents
   *new* tracking.
2. Root `.dockerignore` excludes `dashboard/` entirely (defence in
   depth — even if someone re-tracks it, it never reaches a backend
   image).
3. This document — read by every operator before they run
   `git add dashboard/`.

**Detection**: a one-liner that flags re-introduction:

```pwsh
$count = (git ls-files dashboard/node_modules | Measure-Object -Line).Lines
if ($count -gt 0) { Write-Error "dashboard/node_modules is tracked again ($count files). See ANTI_PATTERNS.md A1." }
```

Suggested CI hook (future):

```yaml
# .github/workflows/anti-patterns.yml
name: anti-patterns
on: [pull_request]
jobs:
  no-tracked-node-modules:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          if [ "$(git ls-files dashboard/node_modules | wc -l)" -gt 0 ]; then
            echo "::error::dashboard/node_modules is tracked. See docs/runbooks/hardening/ANTI_PATTERNS.md#a1"
            exit 1
          fi
```

---

## A2. NEVER commit `dashboard/dist`

**Discovered**: same audit as A1.
**Severity**: low (only 5 files / ~0.5 MB), but same class of mistake.

**Symptom**: working-tree `dashboard/dist/index.html` is constantly
"modified" because Vite rewrites it on every local `npm run build`,
producing noisy `git status` output and accidental commits of stale
build artifacts.

**Fix** (deferred until pressure exists; A3 in the build-pipeline
plan):

```pwsh
git rm --cached -r dashboard/dist/
git commit -m "infra(build): stop tracking dashboard dist artifacts"
```

**Prevention**: same as A1 — `dashboard/.gitignore` already lists
`dist`, and root `.dockerignore` now excludes the whole `dashboard/`.

---

## A3. NEVER hard-code DSNs into Railway env vars

**Discovered**: 2026-05-20 cutover review.

**Symptom**: if `nahla-saas` were configured with a literal
`DATABASE_URL=postgresql://postgres:<pwd>@…/railway`, rotating the
Postgres password would break the app and require a manual env edit.

**Fix**: always reference the Postgres service's variable, never copy
the resolved string:

```
DATABASE_URL=${{nahla-postgres-prod.DATABASE_URL}}    # GOOD
DATABASE_URL=postgresql://postgres:abc123@…/railway    # BAD
```

This is enforced by the procedure in
[`02_NEW_PASSWORD_ROTATION.md`](./02_NEW_PASSWORD_ROTATION.md) §2.

---

## A4. NEVER print DSNs in chat / logs / commits

**Discovered**: 2026-05-20 (during cutover scripts).

**Symptom**: scripts originally accepted DSNs as positional CLI
arguments, which meant any future `--help` accidentally exposed the
password if someone ever pasted it into chat.

**Fix**: every cutover script reads DSNs from environment variables
exclusively (`NEW_DSN`, `OLD_DSN`, `BACKUP_DSN`, etc.). Temp JSON
files that briefly hold a DSN are deleted in a `finally` clause and
listed in `.gitignore` (`_*.json/.txt/.log/.err`).

---

## How to add an entry

1. New incident? Add a section at the bottom: A**N**. NEVER ___.
2. Include: discovery date, symptom (literal log line if you have it),
   root cause, fix command, prevention, detection one-liner.
3. If a pattern can be machine-checked, add a CI hook example.
