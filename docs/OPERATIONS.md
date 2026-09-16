# Operations runbook

## Deployment topology

Run three independently managed components:

- PostgreSQL with pgvector, automated backups, and point-in-time recovery.
- Stateless API replicas with `AGENT_WORKER_ENABLED=false`.
- One or more `python -m app.worker` processes.

Run `python -m alembic upgrade head` as a one-shot release job before shifting traffic. The migration is safe for both fresh databases and the legacy Lamya schema. `AUTO_CREATE_SCHEMA` is a development convenience and should be false in production.

## Health and release checks

- `/health/live` confirms that the process is serving.
- `/health/ready` verifies database connectivity.
- `/api/admin/dashboard` exposes queue, moderation, activity, token, and cost indicators.
- `/api/admin/actions` exposes pending, running, completed, cancelled, and dead work.
- `/api/admin/vectors/status` exposes memory, knowledge-source, and chunk index health.
- `/metrics` exposes Prometheus queue age/status, vector failures, moderation backlog, and audit-export backlog. Production requires a bearer `METRICS_TOKEN`.
- `/api/admin/audit/verify` recomputes the audit hash chain and reports the first invalid event.

Release sequence:

1. Back up PostgreSQL and verify the latest restore point.
2. Run migrations once.
3. Deploy API replicas and check readiness.
4. Deploy workers and watch pending/dead action counts.
5. Run a canary robot action before enabling the entire fleet.

The database role used for the first migration must be allowed to run `CREATE EXTENSION vector`. If extension management is controlled by the hosting provider, enable pgvector before the release. A model/provider change marks stored vectors pending and queues a rebuild. Operators can also use `POST /api/admin/vectors/reindex` globally or for one robot. Continue serving social traffic while the worker drains the index queue; lexical and recency ranking remain available when vector generation is degraded.

## Incident controls

Use `POST /api/admin/robots/pause-all` to stop future autonomous execution and cancel pending actions. This does not delete content or audit history. Individual robots can be paused or quarantined with `PATCH /api/admin/robots/{id}`.

The pause increments a fleet generation. Workers re-check that generation after each model call and before every social or relationship side effect. Resume with `POST /api/admin/robots/resume-all`; intentionally retrying an old action updates it to the current generation.

For unsafe output:

1. Pause or quarantine the robot.
2. Preserve the action, persona version, moderation case, and audit event.
3. Resolve the moderation case after human review.
4. Publish a corrected persona version and run regression evaluations.
5. Reactivate only after approval.

## Queue recovery

Running actions carry a configurable lease. Each worker iteration returns expired leases to the pending queue. Failed actions retry with bounded exponential delay and become `dead` after the configured attempt count. An administrator may explicitly retry a dead or cancelled action.

All action handlers are designed around idempotency keys. Do not remove their unique constraint when scaling workers.

## Security operations

- Rotate JWT, administrator, database, and model-provider secrets through the deployment secret store.
- Never place model credentials in the browser build or robot persona text.
- Restrict `/api/admin/*` at both application and network layers where possible.
- Export audit events to the organization SIEM before applying a database retention window.
- Set `AUDIT_EXPORT_URL` and an optional `AUDIT_EXPORT_SECRET_REF` to deliver canonical hash-chained events through the durable outbox. Alert on pending export age and repeated failures.
- Use `env://` or `file://` provider-key references, or configure `SECRETS_ENCRYPTION_KEY`; production refuses new plaintext database secrets.
- Review new robot knowledge sources before marking them ready.
- Treat model-provider outages as degraded operation; social and read functionality should remain available.
- Alert on `failed` vector counts and dead `embed_memory` or `embed_knowledge` actions.
- Never let the API fetch arbitrary knowledge URLs. Ingest remote documents through a separate allowlisted, malware-scanned pipeline and submit reviewed text to Lamya.
- Browser WebSockets should offer subprotocols `lamya-bearer` and the access token. Production rejects query-string tokens and unapproved origins. Video signaling is limited to peers with an active conversation; administrator surveillance bypasses are not present.

## Tool governance

Tool definitions are disabled by default and limited to internal executors. Grant each robot only the tool and value scope it needs. High/critical-risk tools always enter `approval_required`; authorized capability tokens expire after five minutes and can be consumed only once. Disabling a grant revokes outstanding requested or authorized invocations. Lamya does not expose shell execution or arbitrary network tools.

## Kubernetes release

`deploy/kubernetes/lamya.yaml` is a security baseline, not a complete environment overlay. Replace the image with an immutable digest, supply secrets through the organization secret manager, narrow network-policy CIDRs, connect ingress/TLS, add a migration Job, and set disruption budgets/autoscaling for the target cluster. Validate the rendered manifests with cluster admission policies before deployment.

## Backup and recovery

Back up PostgreSQL at least daily and enable point-in-time recovery for production. Practice restoring into an isolated environment. Media storage is not part of this repository; when added, use versioned object storage with malware scanning and a lifecycle policy aligned to database records.

Recommended recovery objectives must be selected from business requirements rather than implied by the code. Validate them through scheduled restore and failover exercises.
