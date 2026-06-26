from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def prepare_env(root: Path) -> None:
    os.environ["BILI_DANMAKU_PROJECT_ROOT"] = str(root)
    os.environ["BILI_DANMAKU_DOWNLOAD_DIR"] = str(root / "downloads")
    os.environ["BILI_DANMAKU_SUBTITLE_DIR"] = str(root / "subtitles")
    os.environ["BILI_DANMAKU_REPORT_DIR"] = str(root / "reports")
    os.environ["BILI_DANMAKU_STATE_DIR"] = str(root / ".state")
    os.environ["BILI_DANMAKU_STATE_DB"] = str(root / ".state" / "state.sqlite3")
    os.environ["BILI_DANMAKU_JOB_DIR"] = str(root / ".jobs")
    os.environ["BILI_DANMAKU_LOG_FILE"] = str(root / "app.log")
    os.environ["BILI_DANMAKU_ACCESS_LOG_FILE"] = str(root / "access.log")
    os.environ["BILI_DANMAKU_SECRET_FILE"] = str(root / "secrets.json")
    os.environ["BILI_DANMAKU_MODEL_CONFIG_FILE"] = str(root / "model_config.json")
    (root / "secrets.json").write_text("{}", encoding="utf-8")
    (root / "model_config.json").write_text(
        json.dumps({"analysis": {"max_concurrent_jobs": 1}}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_template_config() -> None:
    if "config" in sys.modules:
        return
    config_path = PROJECT_ROOT / "config.template.py"
    spec = importlib.util.spec_from_file_location("config", config_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load config template: {config_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["config"] = module
    spec.loader.exec_module(module)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="english-site-smoke-") as tmp:
        prepare_env(Path(tmp))
        load_template_config()

        from app import app

        client = app.test_client()
        en_home = client.get("/", headers={"Host": "en.danmu.liu-qi.cn"})
        en_home_html = en_home.get_data(as_text=True)
        assert_true(en_home.status_code == 200, "English home should render")
        assert_true('<html lang="en"' in en_home_html, "English home should set English html lang")
        assert_true("https://en.danmu.liu-qi.cn/" in en_home_html, "English home should use English canonical/base URLs")
        assert_true("Bilibili Danmaku" in en_home_html, "English home should expose English product copy")
        assert_true("Start Search" in en_home_html, "English home should translate the main tool CTA")
        assert_true("B站弹幕查询、" not in en_home_html, "English home should not show the Chinese hero headline")
        assert_true(
            'href="https://danmu.liu-qi.cn/"' in en_home_html and 'aria-current="page">EN</a>' in en_home_html,
            "English home should offer a Chinese language switch while marking EN active",
        )

        en_faq = client.get("/faq", headers={"Host": "en.danmu.liu-qi.cn"})
        en_faq_html = en_faq.get_data(as_text=True)
        assert_true(en_faq.status_code == 200, "English FAQ should render")
        assert_true("Frequently Asked Questions" in en_faq_html, "English FAQ should translate the page heading")
        assert_true("Is this site free?" in en_faq_html, "English FAQ should use English FAQ content")
        assert_true("https://en.danmu.liu-qi.cn/faq" in en_faq_html, "English FAQ canonical should use English host")

        en_plugin = client.get("/plugin", headers={"Host": "en.danmu.liu-qi.cn"})
        en_plugin_html = en_plugin.get_data(as_text=True)
        assert_true(en_plugin.status_code == 200, "English plugin page should render")
        assert_true("Download and unzip the package" in en_plugin_html, "English plugin page should translate install step titles")
        assert_true(
            "Click “Download Package” above, unzip the archive to a fixed folder" in en_plugin_html,
            "English plugin page should translate manual install body copy",
        )
        assert_true(
            "Turn on Developer mode, click “Load the unpacked extension”" in en_plugin_html,
            "English plugin page should translate Chrome extension loading instructions",
        )
        assert_true(
            all(
                phrase not in en_plugin_html
                for phrase in (
                    "点击上方",
                    "打开开发者模式",
                    "加载成功后",
                    "打开任意 B 站",
                    "点击“下载 TXT”",
                    "向下滑动到字幕深度分析",
                    "Chrome 扩展程序页面",
                    "AI 弹幕分析页面",
                )
            ),
            "English plugin page should not leak Chinese manual install instructions",
        )

        en_plugin_query = client.get("/plugin?foo=bar&lq_lang=en", headers={"Host": "en.danmu.liu-qi.cn"})
        en_plugin_query_html = en_plugin_query.get_data(as_text=True)
        assert_true(
            'href="https://danmu.liu-qi.cn/plugin?foo=bar"' in en_plugin_query_html,
            "English plugin language switch should preserve path/query and remove lq_lang",
        )
        assert_true("lq_lang" not in en_plugin_query_html, "Language switch URLs should not keep the test language override")

        en_sitemap = client.get("/sitemap.xml", headers={"Host": "en.danmu.liu-qi.cn"})
        en_sitemap_xml = en_sitemap.get_data(as_text=True)
        assert_true(en_sitemap.status_code == 200, "English sitemap should render")
        assert_true("https://en.danmu.liu-qi.cn/" in en_sitemap_xml, "English sitemap should use English host")
        assert_true("https://danmu.liu-qi.cn/" not in en_sitemap_xml, "English sitemap should not point to Chinese host")

        en_robots = client.get("/robots.txt", headers={"Host": "en.danmu.liu-qi.cn"})
        en_robots_txt = en_robots.get_data(as_text=True)
        assert_true(
            "Sitemap: https://en.danmu.liu-qi.cn/sitemap.xml" in en_robots_txt,
            "English robots.txt should advertise English sitemap",
        )

        zh_home = client.get("/", headers={"Host": "danmu.liu-qi.cn"})
        zh_home_html = zh_home.get_data(as_text=True)
        assert_true(zh_home.status_code == 200, "Chinese home should still render")
        assert_true('<html lang="zh-CN">' in zh_home_html, "Chinese home should keep Chinese html lang")
        assert_true("B站弹幕查询、" in zh_home_html, "Chinese home should keep Chinese hero headline")
        assert_true(
            '<link rel="canonical" href="https://danmu.liu-qi.cn/">' in zh_home_html,
            "Chinese home canonical should stay on the Chinese host",
        )
        assert_true(
            'href="https://en.danmu.liu-qi.cn/"' in zh_home_html and 'aria-current="page">中文</a>' in zh_home_html,
            "Chinese home should offer an English language switch while marking Chinese active",
        )

    print("OK: English host overlay, FAQ, sitemap, robots, and Chinese fallback verified.")


if __name__ == "__main__":
    main()
