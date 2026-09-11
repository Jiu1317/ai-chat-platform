# Security

## Never commit

- Real domains, server IP addresses, SSH configuration or provider details
- Usernames, passwords, email addresses or password hashes
- API keys, cookies, sessions, OAuth tokens or Tunnel credentials
- SQLite databases, chat history, projects, uploads, logs or browser profiles
- Live `.env`, `web2api-config.json` or `cloudflared-config.yml` files

Only the `*.example` templates are intended for version control.

## Before every push

Run:

```bash
git status --short
git diff --cached
```

Check every new file. If a secret was committed, removing it in a later commit is not enough: rotate the secret and remove it from Git history before pushing.

## Network boundaries

- Website backend binds to `127.0.0.1:13002`.
- Image bridge binds to `127.0.0.1:13003`.
- Windows API gateway binds to `127.0.0.1:9181`.
- Public access should go through HTTPS reverse proxy or authenticated Tunnel only.
