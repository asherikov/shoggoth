# Code review tooling: qwen-code vs alibaba/open-code-review

Comparison of the two PR-review pipelines relevant to the shoggoth maintenance
workflow. Current selection: **qwen-code `--effort low`** invoked via the
`/review` slash command in `shoggoth/workflow/scripts/shoggoth_workflow.py`.
Candidate: **alibaba/[open-code-review](https://github.com/alibaba/open-code-review)**
(OCR), a standalone Go CLI purpose-built for diff-based review.

## Tool summaries

### qwen-code `/review`

A slash command invoked inside a qwen-code agent session. Targets: PR number,
PR URL (GitHub only), file path, or local working tree. Headless equivalent:
`qwen review run [target] [--json] [--comment] [--effort low|medium|high]`.

Three effort levels with different pipeline depth:

- **`low`** — 3–6 directed inline angles over the diff + gap sweep + under-floor
  re-pass. **0 subagent calls.** No verification, no reverse audit, no verdict,
  never writes the incremental cache, findings capped at 10. This is what
  shoggoth invokes today.
- **`medium`** — high-effort pipeline minus the most expensive passes:
  reduced-dimension parallel fan-out plus build/test and a single verification
  pass. Drops adversarial personas, language-pitfall and wrapper specialists
  (Agents 1d/1e), diff-specialist finders (Agent 8) and reverse audit.
- **`high`** — full pipeline: up to 16 parallel review agents → sharded
  verification → iterative reverse audit. Default for PR reviews.

### open-code-review (OCR)

Standalone Go CLI (`ocr`) installable via `npm install -g
@alibaba-group/open-code-review` (which downloads the right binary from
GitHub Releases per architecture). Architecture: **Deterministic Engineering ×
Agent Hybrid** — file selection, bundling, rule matching and positioning are
deterministic; an LLM agent reviews each group.

Review commands:

- `ocr review --from {base} --to {head} --format json` — diff-based review
- `ocr scan --path <path>` — full-file scan (no diff required)
- `ocr delegate preview` / `ocr delegate rule <paths>` — emit deterministic
  review spec for a host agent (zero LLM calls)

Three effort levels too (`--effort low|medium|high`), but the depth scaling is
different from qwen-code's. Default for diff review is `medium`.

## Pipeline depth — qwen-code at `--effort high` (full pipeline)

A 9-step pipeline with up to **~20–31 LLM calls** per PR, parallelized to
~16–19 concurrent subagents at peak. Verbatim from
`docs/users/features/code-review.md`:

```
Step 1:  Determine scope + effort level (local diff / PR worktree / file)
         Capture the diff to a file + partition it into chunks
Step 2:  Load project review rules (medium/high)
Step 3A: high, <=500 src AND <=3200 total: up to 16 agents  [16+ LLM calls]
            |-- Agent 0: Issue Fidelity & Root-Cause Ownership
            |-- Agent 1a: Correctness — line-by-line scan
            |-- Agent 1b: Correctness — removed-behavior audit
            |-- Agent 1c: Correctness — cross-file tracer
            |-- Agent 1d: Correctness — language-pitfall scan
            |-- Agent 1e: Correctness — wrapper/proxy routing
            |-- Agent 2: Security
            |-- Agent 3a: Reuse & duplication
            |-- Agent 3b: Altitude & abstraction fit
            |-- Agent 3c: Consistency & clarity
            |-- Agent 4: Performance & Efficiency
            |-- Agent 5: Test Coverage
            |-- Agent 6: Undirected Audit (3 personas: 6a/6b/6c)
            |-- Agent 8: Diff-specialized finders (0-2)
            '-- Agent 7: Build & Test (runs shell commands)
Step 3B: high, >500 src OR >3200 total: territory x dim.    [N+5..7+3H calls]
Step 3C: low effort: 3-6 inline angles + gap sweep + under-floor re-pass
                                                                [0 subagent calls]
Step 4:  Deduplicate --> Sharded verify (<=8 findings each)
            --> Aggregate                    [ceil(F/8) calls, F=findings]
Step 5:  Iterative reverse audit, fanned out per chunk;
            stop after 2 consecutive dry rounds (cap 10/5/3 by topology)
Step 6:  Present findings + verdict (high; low pass: findings only)
Step 6B: Apply findings + record per-finding outcomes  (--fix only)
Step 7:  Submit PR review (inline comments, if requested; high only)
Step 8:  Save report + incremental cache (cache: high only)
Step 9:  Clean up (remove worktree + temp files)
```

Key properties of the high-effort pipeline:

- **Adversarial decomposition.** 16+ specialized agents, each with a narrow
  mandate. Security, performance, tests, code quality, reuse, language
  pitfalls, removed-behavior invariants — each gets its own reviewer.
- **Failure-scenario requirement.** Every finding must state a concrete
  failure scenario — input, state, or timing that triggers the wrong outcome.
  A finding that cannot name its scenario is dropped at the source.
- **Sharded verification.** Findings are verified in shards of 8. A Critical
  can only be rejected by quoting contradicting code, proving the claimed
  state impossible from a type/constant/invariant, or matching an exclusion
  criterion. Anything less certain is downgraded, never deleted.
- **Iterative reverse audit.** One auditor per chunk per round, hunting for
  *missing* findings. Stops after two consecutive dry rounds; cap is 10/5/3
  rounds by topology (small / chunked / huge-under-deadline).
- **Counter-frame audit (Agent 6d).** Reads the PR description to *exclude*
  its nominated topics and replay the motivating incident the day after the
  merge.
- **Domain-specialized finders (Agent 8, 0–2 instances).** Pattern-matches
  reconnect logic, module loaders, schedulers, codecs when the diff
  concentrates in those domains.
- **Heavy-rewrite whole-file invariants.** Three extra agents for files that
  are ≥40 % rewritten (existing file ≥300 lines, ≥800 changed lines).
  Checklist items split three ways: mutable fields cleared on every exit,
  timers cancelled on every close, map inserts matched by deletes, retry
  counters incremented at every entry, status return values checked, error
  codes classified permanent vs transient, config fields honoured, early
  returns not skipping required side effects.
- **Cross-file tracer (Agent 1c, same-repo only).** Walks every changed
  symbol's callers (consumer direction) and every added field's read sites
  (producer direction).
- **Build & test (Agent 7, same-repo only).** Runs shell commands, reports
  failures.

## Pipeline depth — qwen-code at `--effort low` (what shoggoth invokes today)

> *"The review runs entirely inline — 0 subagent calls — walking the diff
> once per angle instead of once in total."*

3–6 directed angles scaled by diff size (3 + 1 per 60 source lines, capped at
6), gap sweep (off below 25 source lines), under-floor re-pass (a second look
at the largest changed source file when candidate count is below
`min(changed files, 4)`). **No verification, no reverse audit, no verdict,
never writes the incremental cache.**

Skipped relative to high effort: all 16 specialized agents, sharded
verification, iterative reverse audit, build & test, domain finders,
counter-frame audit, heavy-rewrite invariants, project review rules.

## Pipeline depth — open-code-review

Single LLM call **per file group**, partitioned deterministically before any
model sees the diff.

### Pre-dispatch (deterministic)

`internal/agent/selection.go::selectFiles` is a pure function. For each
changed file, in order:

1. Binary check
2. Built-in secret paths (`allowedext.IsSecretPath` — credentials never reach
   the LLM by construction)
3. User exclude/include globs
4. Extension allowlist
5. Default-path patterns
6. Deletion check
7. Per-file diff-token ceiling (`MaxTokens`)

`--preview` and the real run consume the same answer.

### Grouping

`internal/agent/grouping.go::groupDiffs`. Below
`GroupingMinFiles`/`GroupingBundleLineThreshold`, partitions locally with no
LLM. Above, calls the LLM with file metadata only (no diff content) to
produce semantically-related groups — `message_en.properties` +
`message_zh.properties` get bundled. Indices not paths are returned to keep
prompt tokens small. Enforced caps: ≤10 files per group, ≤token-limit per
group. Falls back to one-file-per-group on any error.

### Per-group review

`internal/llmloop/loop.go::RunMainTask`. One tool-use loop per group,
parallelized through a `CommentWorkerPool`. The LLM has five built-in tools:

- `file_read` — reads a file (or line range), pinned to the diff's `Ref` so the
  model cannot drift
- `file_read_diff` — returns the unified diff for a path
- `file_find` — find files by glob
- `code_search` — `git grep` with strict input validation (refuses traversal
  components and option-like refs)
- `code_comment` — the only way to file a finding; goes through
  `comment_args_repair` (recovers from JSON-with-trailing-garbage), then the
  `CommentCollector`

Loop exits on `task_done`, `StopMaxRounds` (template's `MaxToolRequestTimes`),
`StopEmptyRounds` (3 consecutive no-tool rounds), `StopCompression` (context
window exceeded), or `StopTokenBudget` (aggregate cap fired).

### Scan-mode passes (not present in diff review)

`ocr scan` adds three passes that diff review skips:

- `PLAN_TASK` per file (read whole file, list what to look at)
- `DEDUP_TASK` per batch (collapse duplicates)
- `PROJECT_SUMMARY_TASK` after the run

These are opt-out via `--no-plan`, `--no-dedup`, `--no-summary`.

### Positioning layer

`internal/suggestdiff/diff.go` is a Myers-style LCS used for *rendering*
suggestion blocks in CLI output. It is not a verification pass — comments
flow from `code_comment` → repair → collector → emit without a separate
agent re-reading each finding against the code.

### Delegation mode

`ocr delegate preview` and `ocr delegate rule <paths>` emit the deterministic
spec (file list, merge-base, resolved rule groups) with **zero LLM calls**.
A host agent can then do the actual review with its own loop. This is
explicitly the path for shoggoth to consider if it wants OCR's deterministic
pre-filter without taking on OCR's LLM runtime.

## Side-by-side comparison

| Dimension | qwen-code high | qwen-code low (current) | OCR |
|---|---|---|---|
| LLM calls per PR | ~20–31 typical | 0 subagents, inline walks | 1 per file group (≤10 files) |
| Parallel agents | 16–19 concurrent | 1 | groups run in worker pool |
| Verification | sharded, 8-per, code-quoted rejection | none | none |
| Reverse audit ("what's missing") | iterative, 2-dry-round stop, cap by topology | none | none |
| Cross-file analysis | Agent 1c (same-repo only) | partial (single agent) | implicit — files in same group share one LLM call |
| Build & test | Agent 7 runs shell commands | not run | not run |
| Domain-specialized finders | Agent 8 (reconnect/scheduler/codec/…) | not run | none |
| Failure-scenario requirement | enforced — drop if not constructible | partial | none |
| Counter-frame audit | Agent 6d replays incident after PR-claim exclusions | not run | none |
| Adversarial decomposition | security/test/perf/quality each own agent | none | rule config injects a checklist; otherwise generic |
| Heavy-rewrite invariant pass | 3 whole-file agents for ≥40 % rewrite | not run | none |
| Tool grounding | none at low; full at high | none | yes — file_read, file_read_diff, file_find, code_search |
| Deterministic pre-filter | none | none | path/binary/secret/ext gates + size ceiling |
| Token economy | high; 17-way fan-out reads same early hunks multiple times | low; one inline pass | low; per-group, total-budget-capped |
| Recall vs precision | higher recall, lower precision | mid | higher precision, lower recall (explicit design choice, AACR-Bench) |
| Output shape | findings JSON + verdict + inline comments | text only | structured JSON `LlmComment{path,start_line,end_line,content,severity,category,existing_code,suggestion_code,thinking}` |
| Incremental cache | yes (high only) | none | session-history JSON-Lines per LLM call |
| VCS coupling | GitHub URL parsing, worktree per PR | GitHub URL parsing | local repo only; `--from`/`--to`/`--commit` |
| Secret-path pre-exclusion | none | none | built-in `allowedext.IsSecretPath` |

## AACR-Bench reference

OCR's published benchmark (verbatim from their README):

> *"Compared to general-purpose agents (Claude Code), Open Code Review achieves
> significantly higher **Precision** and **F1** with the same underlying model,
> while consuming only **~1/9 of the tokens** and completing reviews faster.
> Note that its Recall is lower than general-purpose agents — a deliberate
> trade-off favoring precision over noise."*

Dataset: 50 popular open-source repositories, 200 real PRs, 10 programming
languages, cross-validated by 80+ senior engineers (1,505 annotated
ground-truth issues). Hosted on Hugging Face as `Alibaba-Aone/aacr-bench`.

## What shoggoth actually compares today

shoggoth's current call is **not** "high-effort qwen-code" vs OCR. It is
**"low-effort qwen-code (3–6 unverified inline angles)"** vs **"OCR per-group
review (1 verified-by-tool-use call per ≤10 files)"**. At this tier the
difference narrows substantially:

| At shoggoth's current effort | qwen-code low | OCR |
|---|---|---|
| Specialized angles | 3–6, generic | 1 per group, with file_read/code_search grounding |
| Tool grounding | none (inline prompt only) | yes (file_read, file_read_diff, file_find, code_search) |
| Deterministic pre-filter | none | yes (binary, secrets, ext, size) |
| Output structure | text | structured JSON with severity/category |
| Per-finding verification | no | no |
| Reverse audit | no | no |
| Token budget control | no explicit cap | per-group + total cap |
| Resume across interruption | yes (qwen session) | yes (session id) |

The decisive difference at this tier is **tool grounding vs no grounding**:
OCR's review loop can call `code_search "RedisAuth" path:internal/` or
`file_read src/auth.go 100-150` to ground a claim in the actual codebase,
while qwen-code's low-effort inline pass walks the diff prose-only.

## Where each wins

### qwen-code wins on completeness

- **Coverage breadth.** 16+ specialized agents means security gets its own
  take, performance gets its own, tests get theirs. A finding that crosses
  dimensions (e.g., a concurrency bug that also leaks memory) is caught twice.
- **Verification.** A separate agent re-reads every Critical against real
  code. The "must name a failure scenario" rule is the load-bearing
  constraint — it prevents the most common LLM review failure mode
  (plausible-sounding prose without a real trigger).
- **Reverse audit.** The single largest source of missed findings in LLM
  review is "didn't think to look there". qwen-code's iterative reverse audit
  addresses this directly.
- **Reproducibility.** The incremental cache means a re-review of the same
  SHA returns the same findings in seconds.
- **Heavily-rewritten files.** The three invariant agents for ≥40 % rewrites
  catch the *between-the-lines* bugs (timer armed at line 50, cancellation
  path at line 2000) that diff-reading misses.

### OCR wins on signal-to-noise and predictability

- **Determinism before any LLM call.** Binary, secret paths, extensions, user
  excludes, size ceiling — all applied before tokens are spent. This is
  structurally what shoggoth's reviewer would have to do by hand.
- **Group-level parallelism.** A 200-file PR reviews in
  `min(files/10, concurrency)` rounds instead of one PR-shaped serialization.
- **Token cost.** The benchmark numbers aren't aspirational — the
  deterministic pre-filter plus per-group prompt size cap is exactly what
  produces ~9× fewer tokens.
- **Output schema.** `LlmComment` carries path/line/severity/category/
  existing_code/suggestion_code/thinking — every field shoggoth's Gitea
  adapter needs is already there.
- **VCS-agnostic.** No GitHub-specific assumptions. Local repo + `--from
  {base_sha} --to {head_sha}` is all it needs.
- **Secret-path exclusion.** Credentials cannot reach the LLM by
  construction. shoggoth currently has no equivalent.

## Operational considerations

### LLM endpoint

OCR accepts OpenAI- and Anthropic-compatible endpoints (per its ROADMAP
also Google Gemini, Amazon Bedrock, Azure OpenAI). shoggoth already runs
LiteLLM at `http://api.{SHOGGOTH_DOMAIN}/litellm`; configure once:

```bash
ocr config provider   # pick "openai-compatible", point at litellm
ocr config model      # pick the model id litellm routes (qwen, claude, …)
```

Stored in `~/.config/opencodereview/`. No changes to LiteLLM config needed.

### MCP servers

OCR exposes MCP **client** integration via `Config.MCPServers`. The same MCP
servers qwen-code uses today (`codebase-memory-mcp`, `basic-memory`) can be
listed in OCR's config — they appear as agent tools during review. shoggoth's
`slave:noble` image already installs both.

Caveat: OCR's MCP integration is "extend the review agent with external
tools" — useful but not the same as qwen-code's "agent invokes MCP tools as a
core part of its loop". For the review path, this is fine: the bulk of the
work is the deterministic diff + line-accurate comment placement, and the LLM
just decides *what* to flag.

### Container image

Add OCR to `slave:noble`:

```dockerfile
RUN npm install -g @alibaba-group/open-code-review
```

The wrapper downloads the right Go binary from GitHub Releases per
`optionalDependencies` in `package.json`. One image, no per-architecture
split needed.

### Gitea comment posting

OCR ships a GitHub-only comment poster at
`scripts/github-actions/post-review-comments.js`. shoggoth already has its own
Gitea poster (`Gitea.post_pr_review_chunked` and inline-comment logic in
`shoggoth_workflow.py`). The OCR→Gitea adapter is small (~80 lines) — map
`LlmComment{path,start_line,end_line,content,severity,category}` to Gitea's
existing `POST /repos/{repo}/pulls/{n}/reviews` shape with `comments[]` for
inline comments.

### Telemetry

OCR ships native OTLP via `go.opentelemetry.io/otel` — points at the same
endpoint as qwen-code (`OTEL_EXPORTER_OTLP_ENDPOINT=http://otelcol:4318`).
Existing OTel collector, dashboards and alerts in shoggoth work unchanged.

### Resumability

Both expose `--resume`. qwen-code uses a session id; OCR uses an explicit
`<session-id>` argument after `ocr session list`. Same operational pattern.

## Recommended tier mapping

The transition is **not** "qwen-code (high effort) → OCR". qwen-code's
high-effort pipeline is more thorough on recall than anything OCR ships, and
shoggoth would lose the cross-file tracer, build-and-test execution, domain
finders, and reverse audit. That loss is real and material for
security-sensitive or pre-release reviews.

What shoggoth actually needs is a choice between three tiers, mapped to MR
risk:

| MR class | Recommended tool & effort | Why |
|---|---|---|
| Routine dependency bumps, doc edits, refactors with high test coverage | **OCR `--effort low`** | fast, deterministic filter, structured output; recall cost is low because the diff is low-risk |
| Feature work, multi-file changes | **OCR `--effort medium`** | full group partitioning, dedup, project summary; misses cross-file tracer but grouping captures adjacent-file context |
| Security-sensitive, pre-release, large surface-area changes | **qwen-code `qwen review run {repo_dir} --effort high --comment`** | verification + reverse audit + build & test + Agent 8 finders; cost is justified |
| Cross-repo PR review (Gitea URL today rejected by qwen-code) | **OCR only** | qwen-code's cross-repo lightweight mode skips Agents 1c/7/0/6d; you might as well use the deterministic OCR pipeline |

For shoggoth's current auto-review call, **OCR `--effort medium` is a strict
upgrade**: tool grounding + per-group concurrency + structured output, no
GitHub coupling, deterministic pre-filter, predictable cost, and the lost
capabilities (verification, reverse audit, Agent 8, build & test) are exactly
the ones `--effort low` already didn't have.

The high-effort qwen-code path remains useful — just gated behind an opt-in
for security-sensitive MRs, invoked via `qwen review run` (the headless form).
