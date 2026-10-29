# Benchmark dataset staging (Step 4.1)

`tasks_manifest.json` pins 12 tasks (8 Python SWE-bench Verified +
4 TypeScript SWE-bench Multilingual, all vitest/pytest runnable).
Staging is reproducible from `repo` + `base_ref` alone — no imports,
no hidden state.

## Reproduce staging (local or OCI)

```bash
# per task: shallow-fetch the pinned base commit, verify the tree hash
git init task && git -C task fetch --depth 1 https://github.com/<repo>.git <base_ref>
TREE=$(git -C task rev-parse 'FETCH_HEAD^{tree}')
python3 -c "import hashlib; print(hashlib.sha256(('git-tree:$TREE').encode()).hexdigest())"
# must equal the task's dataset_sha256 in tasks_manifest.json
```

Python tasks additionally need the repo's test deps at `base_ref`
(pytest; repo-specific requirements per SWE-bench `environment_setup_commit`).
TypeScript tasks need `pnpm install` at `base_ref` + vitest (vuejs/core
runs `pnpm run test --no-watch`).

## Freeze record (2026-10-29)

- All 12 `base_ref` commits verified to exist upstream; all 12
  `dataset_sha256` values computed from live `git` tree hashes.
- Sources pinned: Verified HF rev `78f471bf…`, Multilingual HF rev `846e647b…`.
- Host: local scratch (no `OCI_SSH_HOST` in `.env`). OCI staging is
  **deferred to 4.2**: re-run the procedure above on OCI and re-verify
  all 12 hashes before the first live run. No dataset mutation is allowed
  after staging — the manifest hashes are the freeze record.
