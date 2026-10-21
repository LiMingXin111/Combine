# Team spaces

Local spaces run independently without an account. They have separate workflow, conversation, knowledge, file, run and connection storage on this computer. Use the top-left workspace card to create, rename or switch spaces. Unapplied edits and unsent drafts need confirmation before being discarded on a switch.

## Start an optional local service

Create a root `.venv`, install `requirements-lock.txt`, then run `Start-Combine-Team-Server.cmd`. It listens only on `http://127.0.0.1:8765`, uses `data/team-service/`, and does not load the model `.env`.

In the desktop selector choose Create team or Join team. Sign in/register on the selected server. Create team can use a fresh local working copy or connect the current space without uploading its existing data. Configure AI connections in a new space yourself.

For other computers, use the same reachable public HTTPS self-hosted server origin; see [deployment](../deploy/README.md). HTTP is only supported on loopback for development.

## Invitations and membership

The owner opens Members & invites, generates a code and shares it personally. Members choose Join team, paste the code, preview the team name and explicitly join. Codes expire after seven days, allow ten joins by default and can be revoked. The server stores code hashes. Repeat joins do not create duplicate membership or spend another use. Owners can remove members; members can leave. Existing memberships can be opened on another computer.

## Share a workflow

1. Open a saved workflow and apply its changes.
2. Choose Share to team, select a team and provide a public title/description.
3. Choose whether to include reusable instructions/public configuration, then Prepare upload preview.
4. Review the actual JSON and excluded fields. Publish workflow to team uploads only this definition, never a run.
5. Members choose Team workflows → Use a local copy. They review nodes, supply their own input and select a connection before running.

Private inputs, table rows, file/knowledge references, provider IDs, keys, paths, annotations, conversations, results and run history are excluded. Private-source downstream titles/configuration reset to defaults. Known protected fragments in a public prompt/title block sharing. Previews expire in 30 minutes and become invalid if the saved graph changes.

Public bundled methods use catalog IDs and SHA-256 references. The recipient restores matching public content locally; a missing source version fails explicitly. Custom source documents are not automatically included. Composite port mappings are preserved. Downloads create independent IDs/version 1 and never auto-run. Authors/owners can remove shared templates. The backend retains immutable versions and conflict checks; the current interface publishes new templates rather than editing team version history.

Cached lists are marked offline and scoped to server/account. Download/publish requires reconnection. Permission rejection clears the relevant cache; sign-out clears device caches/session. Previously downloaded local copies remain independent after leaving a team. Local execution is unaffected by a service outage.

The registry/DPAPI account cookie remain outside worker workspace directories. Space separation is enforced by application routing/storage; the existing AppContainer profile is shared, so spaces are not separate OS user identities. A workspace backup does not include the device registry or account credentials. There is no real-time shared-canvas editing or private-knowledge synchronization.
