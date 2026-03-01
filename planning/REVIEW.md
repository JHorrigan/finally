# Change Review — Commit 7a876f7 ("Doc Review Command & Updates")

**Reviewed by:** Claude Code (claude-sonnet-4-6)
**Date:** 2026-03-01
**Scope:** All changes since commit 14550e1 ("Ready for Teams"), plus untracked new files in `.claude/agents/`

---

## Summary of Changes

Three files were modified or added in the last commit:

1. `.claude/commands/doc-review.md` — new slash command added
2. `.claude/skills/cerebras/SKILL.md` — deleted entirely
3. `planning/PLAN.md` — updated to replace LiteLLM/OpenRouter/Cerebras with OpenAI SDK

One directory is untracked and not yet committed:

4. `.claude/agents/` — four new agent definition files

---

## Assessment by Change

### 1. `.claude/commands/doc-review.md` (New)

The new command reads:

> "Review the documentation file in the planning folder called $ARGUMENTS and add questions, clarifications or feedback to a new section at the end, along with any opportunities to simplify"

**Assessment: Acceptable.**

The command is clear and useful. One minor issue: the file lacks a newline at the end of the file (the diff shows `\ No newline at end of file`). This is cosmetically untidy but functionally harmless.

---

### 2. `.claude/skills/cerebras/SKILL.md` (Deleted)

The Cerebras inference skill was removed entirely. This aligns directly with the decision in `planning/PLAN.md` to drop LiteLLM/OpenRouter/Cerebras in favour of the OpenAI SDK.

**Assessment: Correct and clean.** The skill had no other consumers in the codebase. Removing it eliminates a source of confusion for downstream agents.

---

### 3. `planning/PLAN.md` (Modified)

The changes fall into several categories:

#### LLM provider switch (Cerebras/OpenRouter -> OpenAI)

The following were updated consistently throughout the document:
- Architecture Overview: changed `LiteLLM → OpenRouter (Cerebras)` to `OpenAI Python SDK → gpt-4o-mini`
- Section 5 env vars: `OPENROUTER_API_KEY` replaced with `OPENAI_API_KEY`
- Section 9 LLM Integration: removed Cerebras-specific instructions, replaced with direct OpenAI SDK guidance
- LLM Mock Mode: reference to OpenRouter replaced with OpenAI

**Assessment: The switch is internally consistent within PLAN.md.** However, `README.md` has NOT been updated. It still references `LiteLLM → OpenRouter (Cerebras inference)` and `OPENROUTER_API_KEY` in both the architecture bullet and the environment variables table. This is a direct contradiction between the two documents and will mislead users or agents reading the README.

**Action required:** `README.md` must be updated to match `PLAN.md`.

#### Model name inconsistency

Section 3 (Architecture Overview) now reads:
> `gpt-4o-mini` (`gpt-5-nano` when available)

Section 9 (LLM Integration) reads:
> Use the OpenAI Python SDK to call `gpt-5-nano` directly.

These two sections describe different models. `gpt-4o-mini` is real and widely available. `gpt-5-nano` does not exist as of the knowledge cutoff (August 2025). The plan appears to be using `gpt-5-nano` as a forward-looking placeholder, but the inconsistency between sections creates ambiguity: should agents implement with `gpt-4o-mini` now, or wait for `gpt-5-nano`?

**Action required:** Align the model name across both sections. Given `gpt-5-nano` is not available, the safer and clearer guidance is to specify `gpt-4o-mini` as the implementation target with a note that it can be swapped later.

#### SSE stream description improvement

The plan now reads:
> "Server pushes price updates for all tickers currently on the watchlist at a regular cadence (~500ms); adding or removing a ticker takes effect immediately in the stream"

and:

> "the frontend must preserve accumulated sparkline buffers across reconnections so that sparkline history is not lost on reconnect"

**Assessment: Both additions are good clarifications.** They resolve potential ambiguity about watchlist dynamism and sparkline resilience that existed in the prior wording.

#### Portfolio heatmap placeholder

Added: "shows a placeholder graphic when the user has no positions"

**Assessment: Good.** Specifying the empty state prevents agents from leaving an awkward blank space.

#### Trade bar ticker pre-population

Added: "ticker field (pre-populated when a ticker is clicked in the watchlist)"

**Assessment: Good.** This is a small but meaningful UX detail that was previously implied but not stated.

#### LLM chat flow improvements

The LLM execution steps were expanded from 8 to 9 steps, with two notable additions:

- Step 6 now clarifies that trade prices are taken from the live price cache at execution time, not from the portfolio context snapshot. This is a meaningful correctness detail — it prevents stale-price trades.
- Step 9 adds graceful error handling on LLM failure, returning a user-facing error rather than a 500.

**Assessment: Both additions are valuable and improve correctness and robustness of the specification.**

---

### 4. `.claude/agents/` (Untracked, not committed)

Four agent definition files exist but are not tracked by git:

| File | Name | Description |
|------|------|-------------|
| `change-reviewer.md` | change-reviewer | Reviews PLAN.md and writes feedback to REVIEW.md |
| `plan-reviewer.md` | plan-reviewer | Same as above — duplicate intent |
| `codex-change-reviewer.md` | codex-change-reviewer | Delegates change review to `codex exec` shell command |
| `codex-plan-reviewer.md` | codex-plan-reviewer | Delegates plan review to `codex exec` shell command |

**Issues identified:**

1. **Duplicate agents.** `change-reviewer.md` and `plan-reviewer.md` have essentially the same description and instructions. One should be removed or they should be differentiated — a "plan reviewer" reviews the plan on demand, a "change reviewer" reviews git diffs. The current files blur this distinction.

2. **Codex agents issue shell commands as their primary action.** `codex-change-reviewer.md` instructs the agent to run `codex exec "..."` rather than performing the review itself. This creates an agent that defers to an external tool. If `codex` is not available in the environment this silently fails. The agent should either do the review itself or document the `codex` dependency clearly.

3. **None of these files are committed.** As untracked files they do not exist from the perspective of any other agent or team member who clones the repo. If they are intended for team use they must be committed.

**Action required:** Decide which agents are canonical, resolve duplicates, and commit the directory.

---

## Issues Requiring Action (Prioritised)

### High Priority

1. **README.md is out of sync with PLAN.md.** It still references `OPENROUTER_API_KEY` and LiteLLM/OpenRouter/Cerebras. Any user following the README will fail. This must be fixed before the next build phase begins.

### Medium Priority

2. **Model name inconsistency between Section 3 and Section 9 of PLAN.md.** Section 3 says `gpt-4o-mini`, Section 9 says `gpt-5-nano`. Agents will not know which to implement. Standardise on `gpt-4o-mini` as the concrete implementation target.

3. **`.claude/agents/` not committed.** These files are invisible to the team until committed. Resolve duplicates and commit.

### Low Priority

4. **`change-reviewer.md` and `plan-reviewer.md` are duplicates.** Consolidate or differentiate their responsibilities.

5. **`doc-review.md` command is missing a trailing newline.** Cosmetic only.

---

## What Is Working Well

- The LLM provider migration is well-executed within `PLAN.md`. All internal references are consistent.
- The SSE clarifications (dynamic watchlist, sparkline persistence across reconnect) remove real ambiguity that could have caused implementation divergence.
- The clarification that trade prices use the live cache at execution time (not the portfolio snapshot) is an important correctness detail that was previously missing.
- The graceful LLM error handling requirement (step 9) is a good addition — it prevents silent 500s reaching the user.
- The market data backend (already complete per `MARKET_DATA_SUMMARY.md`) is solid: 73 tests, 84% coverage, correct use of the strategy pattern, thread-safe price cache, and a working Rich terminal demo.

---

## Overall Verdict

The commit achieves its goal: migrating the LLM provider specification from Cerebras/OpenRouter to OpenAI and cleaning up the associated skill file. The quality of the PLAN.md changes is good. The primary risk is the README.md inconsistency, which will cause immediate confusion for anyone reading the project documentation. The model name inconsistency is a secondary risk that will cause implementation uncertainty when the LLM integration phase begins.
