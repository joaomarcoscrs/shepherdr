# Description

<directions>
Include a concise summary of the change including:

1. Context/Background/Prior Behavior (target branch)
2. New behavior (new code)
3. Why the change (motivation)

Include links to relevant context, such as:

1. Linear Tickets, Github tickets, Intercom Tickets
2. Slack Threads
3. Google Docs

List any dependencies that are required for this change. </directions>

## Type of change

Please delete options that are not relevant.

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Maintenance (non-breaking, non-user facing changing such as dependency update, adding test,
      modifying secrets, etc.)

## How has this change been tested, please provide a testcase or example of how you tested the change?

YOUR_ANSWER

## Light/dark UI review

For UI changes, verify both display modes; otherwise write N/A.

- [ ] Verified the changed UI in light and dark mode and provided screenshots or a reproducible
      Storybook example, including affected interaction states, menus/portals, editor Shadow DOM,
      canvas colors, and nested theme scopes.

## Will the change affect Universe? If so was this change tested in universe?

YOUR_ANSWER

## EU parity (data residency)

Roboflow runs a separate EU platform (`roboflow-eu-platform` / `roboflow-eu-staging`, europe-west1).
Both prod **and** staging deploys ship US **and** EU in parallel by default — assume your change
reaches EU in both tiers. Full model:
[docs/EU_DEPLOYMENT_MODEL.md](https://github.com/roboflow/roboflow/blob/master/docs/EU_DEPLOYMENT_MODEL.md)
· [Slab: EU Deployment Model](https://roboflow.slab.com/posts/eu-deployment-model-jjvvkxdd)

- [ ] Region-agnostic, OR EU-specific behavior is gated via env config
      (`packages/shared/environments/roboflow-eu-*.js` + `projectSelector`) — **never** by skipping
      the EU deploy.
- [ ] No new hardcoded US-only resources (project `roboflow-platform`, Crusoe/`ck8s-prd`,
      `us-central1`, US buckets, US-only secrets).
- [ ] Any new env-config key added for US has an explicit value in `roboflow-eu-platform.js`.
- [ ] New secrets/buckets/queues this change needs also exist in `roboflow-eu-platform` (ask in
      #team-engineering if unsure).

If your feature is out of scope for EU: briefly explain why, and describe the feature gate you
implemented for it:

> YOUR_ANSWER

## Github Actions Required to Deploy

List exactly which deploys this change needs. The Deploy PR workflow reads this section as the
deploy list, so an example left here is deployed to staging.

Firebase functions go on one line, comma-separated, each name prefixed with `functions:` (the value
typed into the functions deploy action). Put every other workflow on its own line.

<!--
Format example only. Deploy PR strips this comment. Do not copy it into the answer below.

    functions:your-function,functions:your-other-function

    Deploy Admin
    Deploy Hosting
    Deploy Rapid
    API
    Deploy Universe
-->

YOUR_ANSWER

### Infrastructure impact

- [ ] This change affects infrastructure needs (GPUs, Cloud Function sizing, storage etc.)
- [ ] This change requires updates to secrets
- [ ] This change materially impacts cloud costs (explain briefly)

If checked: link your `@infra-support-engineer` Slack thread here → YOUR_LINK

## Docs

- [ ] This change requires a documentation update
- [ ] Docs updated? What were the changes:
