# Comando main app deploy contract

The installed `herdr-task main-app-deploy` command operates on `roboflow/roboflow` and `master`.
The Comando worker invokes it on one dispatch machine and keeps that machine's task store stable.
Each selection is an explicit release action.

| Deploy menu option | `--environment` |
| --- | --- |
| Deploy to Staging | `staging` |
| Deploy to Prod | `prod` |
| Deploy to Staging + Prod | `both` |

```sh
herdr-task main-app-deploy plan --environment both --json
herdr-task main-app-deploy dispatch --environment both --head "$head" --plan-id "$plan_id" --json
herdr-task main-app-deploy status --request-id "$plan_id" --json
herdr-task main-app-deploy dispatch --resume "$plan_id" --json
```

JSON is the default. On the click, read the plan and use its exact `head`, `plan_id` and selection
for dispatch. Display unresolved changes or targets alongside any work that can proceed. A
successful command returns `schema: 1`. Preflight refusals return `{"schema":1,"error":"..."}`
and exit 1. Inspect the environment and target states even when the exit code is 0.

`plan` and `status` are read-only. The worker can poll status during the action the person chose.
Use `dispatch --resume` to advance a waiting wave of that same action once its prerequisites
complete. Each advance rechecks the original head, PR decisions, workflows and live coverage.
The same initial dispatch returns the existing receipt. An independent click can select the
other environment while the first is active. No scheduler calls these commands.

Planning and pre-dispatch revalidation have a 20-second read budget, a six-second timeout per
GitHub CLI call, and a maximum of 120 GitHub CLI reads per pass. A slow, rate-limited, or unauthorized
read stops that pass before a reservation or workflow dispatch. The JSON error names the stalled
phase and how to retry. When Actions returns a missing job or expired log, that target's coverage
remains unverified. The installed worker can display the existing `error` field as-is.

For a read-only diagnosis, run `herdr-task main-app-deploy plan --environment both --diagnostics`.
This adds an optional `diagnostics` object on success or failure. It includes the elapsed time in
milliseconds, wall time per phase, and per-category read counts, failures, and cumulative read time.
Concurrent reads can have more cumulative time than the phase's wall time. Counts refer to Git or
GitHub CLI invocations, including paginated reads as one invocation. Diagnostics stay out of
`plan_id` and the ledger. Existing clients can omit the flag or ignore the new field.

## Coverage and planning

- `scan` reports the first-parent range of master analyzed, through the exact head. Initial discovery
  walks up to 32 landings. Every landing in the range is inspected independently of the Herdr task
  list, including direct commits and possible rebase landings. The scan reports older history as
  unverified. The worker should present “no deploy needed” as a result for that scanned range.
- When the primary `~/roboflow/roboflow` clone has the exact commit reported by GitHub, the
  planner reads its first-parent objects and workflow definitions without fetching or changing
  the checkout. Missing objects fall back to GitHub. PR bodies and associations are read in
  batches through GitHub GraphQL on every pass, including dispatch. The same `gh` sign-in needs
  access to the private repository, its pull requests, and Actions runs, jobs, and logs.
- Each `sources` entry gives the PR number or landing commit, changed paths and their ownership,
  identified targets and unresolved issues. Only a uniquely associated merged master PR supplies
  deploy instructions. The changed files cross-check the PR's targets, including direct Firebase
  registrations, the functions owned by dedicated workflows, and shared Rapid embedding paths.
  For transitive backend paths the PR must declare the consumers. Missing ownership, missing or
  contradictory PR sections, and infra rollout notes requiring review block affected targets.
  Gaps with unknown scope block the environment. PR prose, paths and workflow text are read as data.
- Each target uses its own staging or prod baseline, evidenced by a successful direct full-tier
  master run, its exact input scope, successful publishing jobs and checkout logs. Hosting also
  requires the bundle preparation checkout. A candidate has to be on the scanned first-parent
  chain or at its boundary. The target's `pending` commits are the relevant landings after that
  baseline. The environment lists the oldest evidenced baseline among its targets for context.
  This is Actions evidence, with no live cloud verification.
- A fresh target without a prior verified run, an expired log, an older run beyond the scan,
  a newer overlapping PR or regional deploy, and a partial or failed release can leave coverage
  unverified. Deploy PR is a staging overlap signal. The planner never invokes it or counts an
  open PR deploy as a master baseline. Unknown prerequisites and manual jobs stay visible.
- Numbered deploy lines, the loginless example rollout and API/Rapid functions-before-hosting
  ordering are enforced. A flat PR list with manual steps requires verified manual coverage
  before its automated peers run. TTL and index runs can supply manual coverage when exact
  evidence exists. Other manual workflows require operator review. The planner sends only the
  supported inputs and lets the workflow select US/EU legs. Universe publishes to the US tier.
- For `checkout: moving_master`, the configured checkout reads the branch when the job starts.
  `event_sha` denotes workflows that explicitly check out the event commit. The CLI dispatches
  the supported ref `master` for both tiers. The observed event SHA, job checkout SHA and expected
  head stay distinct. A run is unverified when these disagree or publishing evidence is missing.

## Plan and request states

Both responses include environments keyed by `staging` and/or `prod`. Each target includes
`target`, `environment`, `regions`, `ref`, `expected_head`, `file`, `workflow_url`, `inputs`,
`state`, `reason`, `sources`, `pending`, `depends_on`, `baseline` and `observed_run`.
Inputs are null for manual workflows. Baseline and observed run objects include the exact run
ID, URL, GitHub status, event SHA and any verified job checkout evidence.

| Target state | Meaning |
| --- | --- |
| `ready` | Evidence and dependencies allow an explicit dispatch |
| `covered` | Verified prior deployment covers the target's changes |
| `waiting` | One or more prerequisite targets still need verified coverage |
| `blocked` | Unresolved merged changes prevent safe dispatch |
| `manual` | Requires an operator to select and run the appropriate workflow |
| `unverified` | Coverage, workflow compatibility or actual checkout cannot be established |
| `submitting` | A durable reservation exists and awaits an exact receipt |
| `submitted` | GitHub returned a run ID and the run is queued, waiting or not visible yet |
| `running` | A matching workflow is in progress |
| `passed` | The exact run succeeded and verified publishing jobs checked out the expected SHA |
| `failed` | The exact run completed unsuccessfully |
| `rejected` | GitHub refused the dispatch |
| `ambiguous` | The transport timed out or the response omitted the exact run ID |
| `held` | Another request has a conflicting receipt or reservation |
| `not_sent` | Pre-POST master verification stopped the submission |

A plan environment is `ready`, `blocked` or `no_deploy_needed`. A request environment can also
be `waiting`, `running`, `passed`, `failed` or `partial_failure`. Use target states for detail.
For submitted runs, use `run.id` and `run.url` for tracking. `head_drift` at request level compares
current master with the approved head. At target level it compares the GitHub event with the
approved head. `checkout_drift` flags a verified checkout that differs from the approved head.
Status refreshes known runs and head. Resume refreshes the release plan for
waiting or deferred targets.

A local SQLite ledger reserves each environment and target before the POST and stores its exact
receipt. Both selections share the same reservations on the dispatch machine. The worker can
poll the same request after an interruption. A missing receipt is held indefinitely for operator
inspection since there is no reliable way to attribute a matching external run to that POST.
Failed submissions and runs are not retried automatically. An operator can use the reported
workflow or exact run link to investigate and recover. Once recovery establishes a verified
baseline covering the earlier request, a later click can deploy newly merged changes. The
guarantees cover requests using this ledger. Actions started elsewhere are observed through
GitHub and can race the CLI.

GitHub's dispatch API version supplies the run ID in the response.
[GitHub workflow dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
