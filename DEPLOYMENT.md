# Deployment Restore Notes

This repository keeps machine-specific files out of Git. To restore a deployment:

1. Copy templates to real config files:

```bash
cp config.template.py config.py
cp uwsgi.template.ini uwsgi.ini
cp gunicorn_conf.template.py gunicorn_conf.py
cp start.template.sh start.sh
chmod +x start.sh
```

2. Replace example paths in `uwsgi.ini`, `gunicorn_conf.py`, and `start.sh`.

3. Copy `.env.example` to `.env` or export the same variables in the process manager.

4. Put real API keys and tokens only in environment variables or in the JSON file pointed to by `BILI_DANMAKU_SECRET_FILE`.

5. Keep runtime data out of Git: `.state/`, `.jobs/`, `downloads/`, `reports/`, `subtitles/`, logs, SQLite files, and backups.
