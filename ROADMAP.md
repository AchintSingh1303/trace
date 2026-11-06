# Roadmap

TRACE is a small, runnable slice of a larger idea: take requirements to reviewed, traceable test cases with AI-style agents and a human in the loop, then keep going all the way to executed, reported results. This page is what already works and what I am considering next.

## Built in this demo

### Requirements to reviewed test cases
*Generation*

Projects, cycles and requirements; five specialised agents (functional, API, security, performance, integration) run in parallel and stream each test case live.

**Why:** Specialised agents give broader, higher quality coverage than one big prompt, and streaming makes long runs feel observable instead of stuck.

### Human review gate with server-side roles
*Governance*

Approve, reject or bulk-review cases. Roles (viewer, tester, qa lead, admin) are enforced by the API, not only hidden in the UI.

**Why:** An AI output should never reach a downstream step unchecked, and a button that is only disabled in the browser is not a gate.

### Requirement to test case traceability matrix
*Governance*

Every case links to the requirement version it was written against; the matrix shows covered, pending review and uncovered requirements.

**Why:** Coverage claims need evidence you can show to an auditor or a release manager.

### Requirement versioning, stale detection and incremental regeneration
*Change management*

Editing a requirement bumps its version and flags linked cases as stale. Incremental mode regenerates only what changed and leaves every other approval alone.

**Why:** Specs change constantly. Regenerating everything throws away reviewed work and trains people to stop reviewing.

### Choose the generator: built-in rules, Claude or ChatGPT
*Generation*

Use the offline rule engine, or paste your own Claude (Anthropic) or ChatGPT (OpenAI) API key to have a model write the cases. Output is validated before it is stored, and the key is never saved.

**Why:** Anyone can compare a model against deterministic rules on the same requirements, with nothing to install and no data leaving the machine unless they choose it.

## Considering next

### Cross-cycle coverage baseline
*Generation · effort: small*

Compare a new cycle against previously approved cases so gap detection says 'this requirement was covered last release, now it is not'.

**Why:** Today gaps only look inside one cycle. A baseline is what turns gap detection from a list into a regression signal.

### End-to-end journey agent as its own type
*Generation · effort: small*

A dedicated agent for multi-screen journeys, one case per journey, with the UI only offering types that really produce output.

**Why:** Offering an option that silently produces nothing is worse than not offering it.

### Locator-verified script generation from approved cases
*Automation · effort: large*

Turn approved cases into Playwright page-object scripts, then check every generated locator against the live app before the script is stored.

**Why:** Checking before storing is cheaper than repairing after a run fails, and it removes most flaky-locator failures up front.

### Script review gate and Git check-in on a branch
*Governance · effort: medium*

Approved scripts go through a second human gate, then land on a per-cycle branch with a pull request. The API refuses check-in unless the gate is approved.

**Why:** Generated code deserves the same review path as written code.

### Dry run in a disposable workspace
*Automation · effort: large*

Run generated scripts in an isolated per-run workspace, capture before/after evidence, and produce a single self-contained HTML report that still opens offline.

**Why:** A proving ground between 'generated' and 'trusted', without touching shared systems.

## Later

### Execution with a browser matrix and schedules
*Execution · effort: large*

Run approved suites across browsers, resume after a closed tab, and schedule runs that skip a tick instead of stacking up when one is already active.

**Why:** Runs belong on the server, not in a tab. Wait-on-active scheduling avoids pile-ups.

### Insights, trends and one-click bug creation
*Reporting · effort: medium*

Rule-based recommendations (deterministic, no model cost on page load), an executive rollup, a per-run detail view, and raising a bug from a failed result.

**Why:** Same input should always give the same advice. A narrative layer from a model can sit on top later.

### Reverse-engineer requirements from a running app
*Requirements · effort: large*

Crawl a live app (with a human-assisted login step) into a knowledge graph, then draft requirements and flag ambiguities for a person to resolve.

**Why:** Most real applications have stale or missing specs, so start from what the app really does.

### Push to a test management tool with per-user credentials
*Integrations · effort: medium*

Create or update work items in a test management tool using each user's own token, held server-side and never sent to the browser.

**Why:** Actions should be attributable to the person who took them.

### Run load tests and judge them against targets
*Execution · effort: large*

Execute the generated performance scenarios and mark each non-functional target met or breached.

**Why:** Generating performance cases is only half the job; the verdict is what people need.

### Multi-worker deployment
*Platform · effort: medium*

Swap the in-process event bus for Redis pub/sub and move artifacts from local disk to object storage.

**Why:** Evidence stored on one machine's disk ties a run to that machine and blocks horizontal scaling.

### PDF and Excel exports
*Reporting · effort: small*

Exports for reports and the traceability matrix, next to the CSV that already works.

**Why:** Stakeholders want a file they can attach to an email.

### Self-healing for broken scripts
*Automation · effort: large*

When an executed script fails, classify the failure, inspect the live page and propose a patch that a person accepts, all inside a strict token, time and tool-call budget.

**Why:** Worth building only for failures that cannot be prevented by verifying locators when the script is generated.

## Principles behind this list

- Checking before you ship is cheaper than repairing after it breaks.
- Do not offer a feature in the UI until it is wired up.
- A permission that is not enforced on the API route is not a permission.
- Keep an audit trail: which input version produced which output, and who approved it.
- Prefer deterministic rules; use a model for what rules cannot do.
- Regenerate narrowly. Throwing away reviewed work is how a review process dies.
