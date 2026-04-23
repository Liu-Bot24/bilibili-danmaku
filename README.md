# Bilibili 弹幕分析网站模板

这是一个用于搭建 Bilibili 弹幕查询、下载与 AI 分析网站的项目模板。它提供了完整的网站后端、前端页面、弹幕处理流程、AI 分析流程、结果分享能力和部署配置模板，可以作为同类工具站的基础框架使用。

基于这个模板搭建的网站，可以让用户输入 BV 号或 Bilibili 视频链接，解析视频基础信息，获取弹幕数据，生成统计图表，并使用 AI 对弹幕内容或上传字幕进行分析。它适合用来构建弹幕下载工具、视频复盘工具、内容研究工具或带分享页的轻量级分析站点。

## 网站能做什么

- 查询 Bilibili 视频基础信息
- 获取弹幕并下载 CSV / TXT 文件
- 展示弹幕密度、词云、发送日期分布和发送时间分布
- 生成 AI 弹幕内容分析
- 上传字幕文件并生成字幕深度分析
- 保存当前分析结果，生成可分享的结果页链接
- 生成分享二维码和分享卡片
- 允许用户在浏览器本地配置自己的 OpenAI-compatible API

## 配置文件怎么用

这个项目把可恢复部署需要的配置都做成了模板文件。新环境部署时，先复制模板，再把复制出来的文件改成当前服务器可用的配置。

```bash
cp config.template.py config.py
cp uwsgi.template.ini uwsgi.ini
cp gunicorn_conf.template.py gunicorn_conf.py
cp start.template.sh start.sh
chmod +x start.sh
cp .env.example .env
```

复制完成后，主要检查三类内容：

1. 服务器路径：项目目录、虚拟环境目录、日志目录、状态数据库目录。
2. 模型接口：OpenAI-compatible API 的 Base URL、模型名称、上下文窗口、输出 token、fallback 顺序。
3. 站点功能 token：维护接口 token、搜索引擎提交 token、内置模型 API Key。

## 模板文件说明

### `config.template.py`

后端主配置模板。复制成 `config.py` 后使用。

它主要控制：

- 内置 AI 模型服务配置
- 模型 fallback 顺序
- 采样参数
- 请求超时参数
- 并发任务数量
- 报告、缓存和分析任务的默认行为

模板里只放了两个通用的 OpenAI-compatible 示例：

- `openai-compatible-primary`
- `openai-compatible-secondary`

如果要接入自己的模型服务，在 `config.py` 里替换示例的 `endpoint`、`model`、`key_env` 等字段即可。

### `.env.example`

环境变量示例。复制成 `.env` 后使用，也可以把这些变量配置到服务器面板、进程管理器或 systemd 里。

常用配置包括：

| 变量 | 作用 |
| --- | --- |
| `BILI_DANMAKU_PROJECT_ROOT` | 项目运行目录 |
| `BILI_DANMAKU_LOG_FILE` | 应用日志路径 |
| `BILI_DANMAKU_STATE_DB` | SQLite 状态数据库路径 |
| `BILI_DANMAKU_SECRET_FILE` | 私有密钥 JSON 文件路径 |
| `BILI_DANMAKU_MODEL_CONFIG_FILE` | 模型配置覆盖文件路径 |
| `BILI_DANMAKU_APP_TOKEN` | 维护接口访问 token |
| `BILI_DANMAKU_BAIDU_SUBMIT_TOKEN` | 百度链接提交 token |
| `BILI_DANMAKU_PRIMARY_ENDPOINT` | 主模型接口地址 |
| `BILI_DANMAKU_PRIMARY_API_KEY` | 主模型 API Key |
| `BILI_DANMAKU_SECONDARY_ENDPOINT` | 备用模型接口地址 |
| `BILI_DANMAKU_SECONDARY_API_KEY` | 备用模型 API Key |

API Key 可以放在环境变量里，也可以放在 `BILI_DANMAKU_SECRET_FILE` 指向的 JSON 文件里。

### `uwsgi.template.ini`

uWSGI 部署模板。复制成 `uwsgi.ini` 后使用。

需要替换：

- `chdir`
- `wsgi-file`
- `virtualenv`
- `pidfile`
- `daemonize`
- `BILI_DANMAKU_*` 环境变量路径

如果服务器使用宝塔 Python 项目或类似面板，通常会用到这个文件。

### `gunicorn_conf.template.py`

Gunicorn 部署模板。复制成 `gunicorn_conf.py` 后使用。

需要替换：

- 项目目录
- 监听地址和端口
- 进程数和线程数
- pid 文件路径
- 访问日志和错误日志路径

如果服务器使用 Gunicorn + Nginx，这个文件就是主要启动配置。

### `start.template.sh`

启动脚本模板。复制成 `start.sh` 后使用。

需要替换：

- 虚拟环境里的 `uwsgi` 路径
- 项目的 `uwsgi.ini` 路径

如果使用服务器面板直接管理进程，这个脚本可以不用；如果使用 shell 或 supervisor 启动，它可以作为入口脚本。

## 运行数据目录

网站运行后会生成一些数据目录：

- `downloads/`：弹幕下载文件
- `subtitles/`：用户上传的字幕文件
- `reports/`：用户分享的分析报告
- `.jobs/`：异步分析任务状态
- `.state/`：SQLite 状态数据库

这些目录是运行时数据。迁移服务器时，如果要保留历史报告或缓存，可以单独打包这些目录。

## 本地调试

安装依赖并准备好配置后，可以直接运行：

```bash
python3 app.py
```

生产环境建议使用 uWSGI 或 Gunicorn，并通过 Nginx、宝塔或其他 Web 服务把域名反向代理到后端服务。
