"""出事时通知 Leo 的那个脚本。"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _copy_script(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    target = scripts / "alert.sh"
    target.write_text((ROOT / "scripts" / "alert.sh").read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o755)
    return target


def test_without_a_webhook_it_stays_quiet_and_never_fails(tmp_path: Path) -> None:
    """没配 webhook：只往 stderr 写一行，退出 0。它不能连累调它的备份脚本。"""
    script = _copy_script(tmp_path)
    result = subprocess.run([str(script), "备份失败：找不到 rclone"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "备份失败" in result.stderr


def test_it_posts_one_plain_message_that_mentions_nobody(tmp_path: Path) -> None:
    """配了 webhook：发一条 JSON，中文和引号、换行原样到达，不 @ 任何人。"""
    received: list[dict] = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server 的接口名
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Hook)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    script = _copy_script(tmp_path)
    (script.parent / "alert.env").write_text(
        f"ALERT_WEBHOOK_URL=http://127.0.0.1:{server.server_port}/hook\n", encoding="utf-8"
    )
    text = '每周体检发现问题：\n✗ 1 个任务重试到放弃了 "连不上接口" @everyone'
    result = subprocess.run(
        [str(script), text], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}
    )
    thread.join(timeout=10)
    server.server_close()
    assert result.returncode == 0, result.stderr
    assert received and received[0]["content"] == f"[chloe] {text}"
    assert received[0]["allowed_mentions"] == {"parse": []}


def test_the_service_raises_the_alarm_when_systemd_gives_up() -> None:
    """熔断之后 systemd 不再拉起她：chloe.service 要挂着报警的那个单元。"""
    unit = (ROOT / "scripts" / "chloe.service").read_text(encoding="utf-8")
    unit_section = unit.split("\n[Service]\n")[0]
    assert "OnFailure=chloe-alert.service" in unit_section
    alert = (ROOT / "scripts" / "chloe-alert.service").read_text(encoding="utf-8")
    assert "alert.sh" in alert
