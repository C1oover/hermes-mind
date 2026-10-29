# mind-hermes — repo mapping

| deliverable                                  | repo path                                              |
|----------------------------------------------|--------------------------------------------------------|
| `plugins-mind-hermes.nix`                    | `modules/services/hermes/plugins/mind-hermes.nix`      |
| include diff (below)                         | `modules/services/hermes/plugins/hermes-plugins.nix`   |
| `plugin/`                                    | push as `github:C1oover/mind-hermes` (repo root)       |

Enable diff for `hermes-plugins.nix`:

```nix
    includes = [
      <idios/hermes-agent/obsidian>
      <idios/hermes-agent/memory-ui>
      <idios/hermes-agent/mind>
    ];
```

Push the plugin tree:

```bash
cd /data/workspace/mind-hermes-module/plugin   # or copy into your own checkout
git init && git add -A && git commit -m "mind-hermes v4 plugin: JS engine via node bridge"
git remote add origin git@github.com:C1oover/mind-hermes.git && git push -u origin master
```

Post-rebuild decisive check (inside the container):

```bash
ls /data/.hermes/plugins/nix-managed-mind-hermes/plugin.yaml \
  && grep -A5 "enabled:" /data/.hermes/config.yaml | grep mind-hermes \
  && hermes -z "Quote the first 6 words of the <inner_state> block."
```

Notes:
- `plugins.enabled` is derived from `extraPlugins` by every plugin module (same line, replace-safe); the module overwrites the hand-edited config.yaml line — no manual enable needed after rebuild.
- State lives in `/var/lib/hermes/.hermes/plugin-data/mind-hermes/` (persisted with the existing `/var/lib/hermes` persist entry).
- node is already in the container image (engine bridge ran here); no PATH plumbing.
- fastapi for the dashboard API is in the sealed venv; no extraPythonPackages.
- Proactive wake needs the gateway API server (127.0.0.1:8642) + `API_SERVER_KEY` in the agenix hermes-env file (currently absent — wakes just backlog until then).
- HA token: put `HA_TOKEN=...` in hermes-env (secrets), not in settings.
