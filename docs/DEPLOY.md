# Deployment & host operations

Host-level notes for running the bot publicly: the reverse proxy that fronts
the Docker stack, the **Tailscale ↔ nginx port-443 clash** (the one that bites
after every reboot), and how to reach a **LiteLLM gateway** on the tailnet.
The app itself is covered in [`README.md`](../README.md) → *Docker (Compose)*;
this file is only the layer **in front of and around** the container.

---

## Reverse proxy (nginx + TLS)

The container binds **`127.0.0.1:8000`** only (see `docker-compose.yml`,
`beta-web`). Public access goes through **nginx**, which terminates TLS on
**:443** and proxies the `/betabot/` prefix to the app:

```
https://<host>/betabot/   --nginx-->   http://127.0.0.1:8000/betabot/   (Docker)
```

- The app is mounted under that prefix via **`WEB_BASE_PATH=/betabot`** (compose
  env). nginx passes the prefix through unchanged — do **not** strip it.
- Server block lives at `/opt/homebrew/etc/nginx/servers/betabot.conf` (macOS
  Homebrew layout). It also redirects `:80 → :443` and 404s the raw app paths
  (`/api/`, `/static/`, `/docs/`) so only `/betabot/` is reachable from outside.
- Auth is two layers (`README.md` → *Web app*): first visit with
  `?access=<WEB_ACCESS_TOKEN>` sets the session cookie, then HTTP Basic from
  `data/web_users` (`student-bot-mkuser <name>`). A bare `/betabot/` returning
  **403** is the access-token gate working, not an outage.

### nginx must run as root, and survive reboot

Binding :80/:443 requires **root** — nginx started as your user (plain
`brew services start nginx`) can **never** bind 443. Start it as root so
Homebrew installs a **LaunchDaemon** (boots at startup, as root, no login
needed):

```bash
sudo brew services start nginx     # installs /Library/LaunchDaemons/homebrew.mxcl.nginx.plist
```

Brew prints *"must be run as non-root to start at user login"* — ignore it;
the LaunchDaemon is exactly what you want for an always-on public service.
Validate config before (re)starting: `nginx -t`.

---

## ⚠️ The Tailscale ↔ port-443 clash (recurring after reboot)

**Symptom:** the bot is unreachable from the public URL even though the Docker
containers are healthy. nginx fails to start with:

```
[emerg] bind() to 0.0.0.0:443 failed (48: Address already in use)
```

**Cause:** `tailscale serve` / `funnel` binds the host's **:443** (via
`tailscaled`, as **root**), and Tailscale re-applies that config on boot —
grabbing 443 before nginx. Because the socket is root-owned it's **invisible to
non-sudo `lsof`**; confirm with `netstat`:

```bash
netstat -an -p tcp | grep '\.443 '        # shows *.443 LISTEN even when lsof (no sudo) sees nothing
curl -sk https://127.0.0.1/               # HANGS when Tailscale holds 443 (it only serves tailnet traffic)
tailscale serve status                    # shows the conflicting :443 serve config
```

**Fix:**

```bash
tailscale serve reset            # frees 443; does NOT touch tailnet VPN / SSH / screen-sharing
sudo brew services start nginx   # rebind 443 as root
curl -sk -o /dev/null -w "%{http_code}\n" https://127.0.0.1/betabot/   # expect 403 (= chain works)
```

**The rule:** on any host that fronts the public site, **never configure
`tailscale serve --https=443`**. The host's 443 belongs to nginx. If you need a
tailnet-only HTTPS service on that machine, put it behind nginx or give it a
different port. `tailscale serve reset` only removes the HTTPS *proxy* config —
it does **not** disconnect Tailscale, so VPN, SSH and screen-sharing keep working.

---

## LiteLLM gateway on the tailnet (LLM provider)

The bot reaches any OpenAI-compatible endpoint via the generic provider in
`bot/llm.py` (`_stream_chat_openai`). A LiteLLM proxy is just another provider —
**config only, no code**. Current setup (`config.yaml` → `llm.providers.litellm`):

```yaml
litellm:
  kind: openai_compatible
  base_url: http://100.75.42.33:4000/v1   # 100.x tailnet IP of the gateway host
  display_name: LiteLLM (lokal Qwen via Spark)
  api_key_env: LITELLM_API_KEY
  timeout_seconds: 120
  discloses_external: false               # models run LOCALLY on the Spark — suppress the cloud notice
```

Key points:

- **Use the `100.x` IP, not the MagicDNS `.ts.net` name**, in `base_url`. A
  Docker container on a native Linux host won't resolve MagicDNS unless you
  point it at Tailscale's resolver (`--dns 100.100.100.100`); the IP avoids that.
- **Plain `http://` is fine** — the tailnet encrypts in transit (WireGuard). No
  need to put the gateway behind TLS.
- **`LITELLM_API_KEY`** in `.env` is the gateway **virtual key** (scoped/rotatable
  from the LiteLLM UI → Virtual Keys). The loader auto-reads any provider's
  `api_key_env` (`config.py`), sent as `Authorization: Bearer`.
- **`discloses_external: false`** marks this provider as local so the "external
  cloud model, don't share sensitive info" notice does **not** fire (it's keyed
  on this flag, not on `provider_kind`). Default (`None`) derives from kind:
  `ollama` → local, everything else → external. **Set `false` only for gateways
  whose models actually run locally** — keep the student-scoped key restricted to
  local models; do not route student content to third-party clouds (OpenRouter).
- **Model entries** are keyed `litellm/<alias>` where `<alias>` matches the name
  registered in LiteLLM. `num_ctx` is **informational on this path** (not sent;
  the real window is set in Ollama/LiteLLM on the gateway host).
- **Reasoning models** (e.g. `qwen3.6-think`): LiteLLM streams chain-of-thought
  as `delta.reasoning_content`, then the answer as `delta.content` — use
  `thinking_style: openai_reasoning_field` so the CoT is filtered out and only
  surfaced as a "thinking…" indicator.

Quick gateway checks (run from the host, with `LITELLM_API_KEY` exported):

```bash
curl -s  http://100.75.42.33:4000/v1/models -H "Authorization: Bearer $LITELLM_API_KEY" | jq '.data[].id'
LLM_ACTIVE=litellm/qwen3.6 uv run student-bot-cli "Vilka krav gäller för masterbehörighet?"
```

---

## Moving to a new host — checklist

When relocating (the new host also runs Tailscale):

1. **Reverse proxy:** install nginx, copy the `betabot.conf` server block,
   install TLS certs, and start it **as root** so it can bind 443 (LaunchDaemon
   on macOS, a systemd unit on Linux — order it **after** `tailscaled` if both
   run there).
2. **Port 443:** ensure **no `tailscale serve` on 443** (see the clash section).
3. **Container → tailnet reachability:** verify the bot container can reach the
   LiteLLM `100.x` address. Test from inside the container:
   ```bash
   docker compose exec beta-web python -c "import socket; socket.create_connection(('100.75.42.33',4000),5)"
   ```
   On Docker Desktop/macOS this works out of the box (the VM routes via the host
   Tailscale). On native Linux Docker it generally works too; if MagicDNS is
   needed, add `--dns 100.100.100.100` to the container.
4. **App env:** keep `WEB_SESSION_SECRET` stable (or sessions invalidate), set
   `WEB_ACCESS_TOKEN`, and create `data/web_users` **before** the server stays up.
5. **Reindex** the corpus on the new host (`scripts/reindex.py`) — the Chroma
   index is host-specific (see `README.md` *Image notes* re: chromadb mismatch).
