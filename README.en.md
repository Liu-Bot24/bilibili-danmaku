# Bilibili Danmaku Analysis Website Template

Languages: [简体中文](README.md) · English

A Flask template for building a Bilibili danmaku search, download, charting, and AI analysis website. Users can enter a BV ID or Bilibili video URL, fetch danmaku data, download CSV/TXT files, inspect interaction charts, and generate AI-powered content analysis or subtitle-based deep analysis with an OpenAI-compatible model.

It works well as a danmaku download tool, a video review workflow, a lightweight content research site, or a shareable analysis page for Bilibili videos.

## Features

- Parse BV IDs and Bilibili video URLs
- Fetch video metadata and cover images
- Download danmaku as CSV / TXT
- Show danmaku density, word cloud, send-date distribution, and send-hour distribution
- Generate AI danmaku content analysis
- Upload subtitles and generate deeper video analysis
- Save analysis results as shareable report pages
- Generate share links, QR codes, and report cards
- Let users configure their own OpenAI-compatible API in the browser
- Provide an ops dashboard for visits, downloads, analysis jobs, and shared reports

## Quick Start

Copy the templates, then fill in your server paths and model settings:

```bash
cp config.template.py config.py
cp .env.example .env
```

Run locally:

```bash
python3 app.py
```

For production, run the app with uWSGI, Gunicorn, or your server panel, then place Nginx in front of it. The repository includes uWSGI, Gunicorn, and start-script templates that you can copy and adapt to your environment.

## Configuration

Common configuration areas:

| Setting | What it controls | Recommendation |
| --- | --- | --- |
| Project root | Backend code, static assets, and templates | Use a stable server directory |
| Log paths | App, access, and worker logs | Store them in your server log directory |
| State database | Jobs, analytics, and rate-limit state | Use a writable SQLite path |
| Private secrets file | Operator token, model API keys, and search submission token | Keep it in a private server directory |
| Model config file | Built-in models, fallback order, timeouts, and sampling limits | Keep it in a private server directory |
| Model endpoint | OpenAI-compatible Chat Completions endpoint, model name, and token limits | Choose a stable model that returns JSON reliably |

Keep API keys, operator tokens, and submission tokens in server environment variables or a private JSON file. Do not expose them in the browser, public web directories, or the public repository.

## AI Analysis

The built-in analysis flow prepares danmaku samples for the model and asks for structured JSON. You can configure:

- Primary and fallback models
- Fallback order
- Sampling limits for danmaku analysis and subtitle deep analysis
- Request timeouts and maximum output tokens
- Analysis job concurrency

Users can also configure their own OpenAI-compatible API from the web page. That custom configuration is stored only in the user's browser and does not enter the server-side job queue.

## Runtime Data

The website creates danmaku files, subtitle uploads, shared reports, async job state, and analytics data at runtime. If you migrate servers and want to preserve historical downloads, shared pages, or ops metrics, migrate those runtime directories and the state database together.

## Deployment Notes

- Give download, report, state database, and log directories write permission.
- Enable HTTPS, suitable request body limits, and sensible timeouts at the reverse proxy layer.
- Store secrets, tokens, and private model configuration in server environment variables or private files.
- Run the background worker as a separate process so long AI jobs do not block web requests.
- Back up the state database and any shared reports you need to keep.

## License

Add a license that matches how you plan to distribute and operate the project.
