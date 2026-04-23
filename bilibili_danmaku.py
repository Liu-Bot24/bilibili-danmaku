from __future__ import annotations

from danmaku_backend.services.bilibili import extract_bvid
from danmaku_backend.services.downloads import get_danmaku


def main() -> None:
    print("=== bilibili弹幕下载器 ===")
    while True:
        raw = input("\n请输入视频BV号或链接（输入 q 退出）\n>>> ").strip()
        if raw.lower() == "q":
            break
        bvid = extract_bvid(raw)
        if not bvid:
            print("BV号格式不正确，请重新输入")
            continue
        result = get_danmaku(bvid)
        if isinstance(result, tuple):
            print(result[1])
            continue
        print(f"下载完成，共下载 {result['count']} 条弹幕")
        print(f"保存至文件:\n{result['csv_filename']}\n{result['txt_filename']}")


if __name__ == "__main__":
    main()
