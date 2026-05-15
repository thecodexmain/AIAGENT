# AIAGENT

Production-ready autonomous Telegram AI coding agent in Python 3.11 with async architecture, OpenAI-compatible API integration, per-user project workspaces, persistent memory, admin controls, and Docker sandbox execution.

## Features

- Async Telegram bot using `python-telegram-bot`
- OpenAI-compatible API (`https://api.freemodel.dev/v1`, default model `gpt-5.5`)
- Multi-file generation via AI response file blocks
- Per-user project workspace at `./projects/{user_id}/default`
- Persistent sessions and conversation history in `./data`
- Docker-isolated `/run` execution with timeout and resource limits
- Admin control plane (`/setkey`, `/setmodel`, `/users`, `/maintenance`, `/ban`, etc.)
- Monitoring (`/status`: CPU, RAM, Disk, uptime, API/error counters)

## Project Structure

```text
.
├── bot.py
├── ai_engine.py
├── admin.py
├── memory.py
├── docker_runner.py
├── file_manager.py
├── config_manager.py
├── middleware.py
├── security.py
├── utils.py
├── requirements.txt
├── config.json
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── install.sh
├── start.sh
├── systemd/aiagent.service
├── projects/
├── data/
├── logs/
└── tmp/
```

## Quick Start (Linux)

```bash
chmod +x install.sh start.sh
./install.sh
cp .env.example .env
# edit .env and config.json
./start.sh
```

## Docker Start

```bash
cp .env.example .env
# edit .env and config.json
docker compose up -d --build
```

## Required Environment Variables

- `TELEGRAM_BOT_TOKEN` (required)
- `OPENAI_API_KEY` (optional if set with admin `/setkey`)
- `APP_ENCRYPTION_SECRET` (recommended)

## Commands

### User Commands

- `/start`
- `/help`
- `/build`
- `/fix`
- `/continue`
- `/run`
- `/zip`
- `/files`
- `/delete`
- `/history`
- `/reset`
- `/status`

### Admin Commands

- `/admin`
- `/setkey NEW_KEY`
- `/getkey`
- `/fullkey`
- `/setmodel MODEL`
- `/getmodel`
- `/setbaseurl URL`
- `/status`
- `/users`
- `/broadcast MESSAGE`
- `/logs`
- `/restart`
- `/shutdown`
- `/clearcache`
- `/projects`
- `/deleteproject USER_ID PROJECT_NAME`
- `/ban USER_ID`
- `/unban USER_ID`
- `/maintenance on|off`

## API Communication Flow

1. Telegram command arrives in `bot.py`
2. Guard checks in `middleware.py`
3. Prompt + memory sent to `ai_engine.py`
4. Streamed response updates progress message
5. File blocks are extracted and saved by `file_manager.py`
6. Optional code execution via `docker_runner.py`

## Security Model

- Secrets in `.env` or encrypted file `data/api_key.enc`
- Per-user filesystem isolation under `projects/{user_id}`
- Path traversal prevention in `security.py`
- Command allowlist and unsafe shell token blocking
- Docker execution with resource/time limits and disabled network
- Admin-only privileged commands

## Systemd Deployment

```bash
sudo useradd -m botuser || true
sudo mkdir -p /opt/aiagent
sudo rsync -a ./ /opt/aiagent/
cd /opt/aiagent
sudo -u botuser ./install.sh
sudo cp systemd/aiagent.service /etc/systemd/system/aiagent.service
sudo systemctl daemon-reload
sudo systemctl enable --now aiagent
sudo systemctl status aiagent
```

## Notes

- Ensure Docker daemon is available if using `/run`.
- Add admin Telegram IDs in `config.json`.
- For production, set a strong `APP_ENCRYPTION_SECRET`.
