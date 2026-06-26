from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from config import get_baidu_submit_token
from danmaku_backend.services.bilibili import BV_RE
from danmaku_backend.services.reports import default_report_store
from danmaku_backend.settings import REPORT_DIR


class BaiduSubmitter:
    def __init__(self, state_path: Path = REPORT_DIR / "baidu_submissions.json"):
        self.state_path = Path(state_path)

    def submit_bvid_once(self, bvid: str) -> dict[str, Any]:
        bvid = self._validate_bvid(bvid)
        config = self._config()
        if not config["enabled"]:
            return {"status": "skipped", "reason": "disabled"}

        token = get_baidu_submit_token()
        if not token:
            return {"status": "skipped", "reason": "missing_token"}

        url = f"{config['site'].rstrip('/')}/result?bvid={quote(bvid, safe='')}"
        state = self._read_state()
        record = state.get("submitted", {}).get(bvid)
        if record and record.get("ok") and self._within_dedupe_window(record.get("submitted_at"), config["dedupe_days"]):
            return {"status": "skipped", "reason": "deduped", "url": url}

        endpoint = f"http://data.zz.baidu.com/urls?site={config['site'].rstrip('/')}&token={token}"
        try:
            response = requests.post(
                endpoint,
                data=url.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
                timeout=(5, 12),
            )
            payload = self._parse_response(response)
            ok = response.ok and "error" not in payload
            now = self._now()
            state.setdefault("submitted", {})[bvid] = {
                "url": url,
                "submitted_at": now,
                "ok": ok,
                "response": self._public_response(payload),
            }
            self._write_state(state)
            return {
                "status": "submitted" if ok else "failed",
                "url": url,
                "response": self._public_response(payload),
            }
        except Exception as exc:
            return {"status": "failed", "url": url, "error": str(exc)}

    def _config(self) -> dict[str, Any]:
        config = default_report_store.get_config().get("baidu_submit", {})
        if not isinstance(config, dict):
            config = {}
        site = str(config.get("site") or "https://danmu.liu-qi.cn").strip().rstrip("/")
        if not site:
            site = "https://danmu.liu-qi.cn"
        return {
            "enabled": bool(config.get("enabled", False)),
            "site": site,
            "dedupe_days": self._positive_int(config.get("dedupe_days"), 30),
        }

    def _validate_bvid(self, bvid: str) -> str:
        bvid = str(bvid or "").strip()
        if not BV_RE.fullmatch(bvid):
            raise ValueError("invalid bvid")
        return bvid

    def _within_dedupe_window(self, submitted_at: str | None, days: int) -> bool:
        if not submitted_at:
            return False
        try:
            submitted_time = datetime.fromisoformat(submitted_at)
        except ValueError:
            return False
        return submitted_time > datetime.now().astimezone() - timedelta(days=days)

    def _parse_response(self, response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {"raw": payload}
        except ValueError:
            return {"status_code": response.status_code, "text": response.text[:200]}

    def _public_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in payload.items()
            if key not in {"token"}
        }

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"submitted": {}}
        if not isinstance(data, dict):
            return {"submitted": {}}
        if not isinstance(data.get("submitted"), dict):
            data["submitted"] = {}
        return data

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.state_path.parent),
            delete=False,
        ) as tmp_file:
            json.dump(payload, tmp_file, ensure_ascii=False, indent=2, sort_keys=True)
            tmp_name = tmp_file.name
        os.replace(tmp_name, self.state_path)
        try:
            os.chmod(self.state_path, 0o644)
        except Exception:
            pass

    def _positive_int(self, value: Any, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback

    def _now(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")


default_baidu_submitter = BaiduSubmitter()
