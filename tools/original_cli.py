# -*- coding: utf-8 -*-
"""「原画质」回捞命令行工具：可复现的探测 / 下载 / 校验 / 回归。

仅用于**本人作品或已获授权内容**的归档与画质分析。不破解签名：detail 请求
由真实作品页的 JS 代发，play 端点在已登录的浏览器上下文内发出。

    python tools/original_cli.py login-status
    python tools/original_cli.py probe <作品链接或ID>
    python tools/original_cli.py download <作品链接或ID> [--out 目录]
    python tools/original_cli.py compare <作品链接或ID> [--out 目录]
    python tools/original_cli.py check <视频.mp4>
    python tools/original_cli.py regress          # 离线，可挂定时任务

`compare` 对应验收清单第 4 条：把原片与 `bit_rate` 里体积最大的转码档都落到本地，
对比编码、分辨率、码率；编码被更换（如 HEVC → H.264）是「转码档经过重编码」的硬证据。
已存在的原片文件会按大小复用，不重复下载大文件。

`regress` 不需要网络，失败返回非零退出码，可直接挂每日定时任务做静默失效巡检。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config          # noqa: E402
import links           # noqa: E402
import models          # noqa: E402
import original_stream as oq  # noqa: E402

# 规范第 6 节三个实测样本（体积单位 MB），用于校准判定实现。
SAMPLES = [
    ("A 4K 横屏", 78.46, 3.25, "original"),
    ("B 竖屏 4K", 2.68, 6.21, "transcoded"),
    ("C 竖屏 1080p", 21.36, 5.74, "original"),
]


def capture_detail(raw_input: str, session: oq.OriginalSession,
                   timeout_s: float = 20.0) -> dict:
    """规范 Step 1~3：解析 ID → 打开作品页监听 detail → SSR 递归回退。"""
    ctx, page = session.ensure()
    captured = {}

    def on_response(response):
        if oq.DETAIL_MARK not in response.url:
            return
        try:
            node = response.json().get("aweme_detail")
        except Exception:  # noqa: BLE001 - 非 JSON / 结构变化
            return
        if isinstance(node, dict) and node:
            captured.setdefault("detail", node)

    page.on("response", on_response)
    try:
        aweme_id = links.extract_aweme_id(raw_input)
        if not aweme_id:
            # 短链会先跳 H5 分享页，必须跟随跳转再从 page.url 里取 ID
            page.goto(links.extract_url(raw_input), wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            aweme_id = links.extract_aweme_id(page.url)
            if not aweme_id:
                raise oq.OriginalStreamError("没有找到有效的抖音作品 ID")
        page.goto("https://www.douyin.com/video/" + aweme_id,
                  wait_until="domcontentloaded")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not captured:
            page.wait_for_timeout(500)
        if oq.detect_challenge(page.url, page.title()):
            raise oq.OriginalStreamError("命中人机校验，请在「登录抖音」里人工处理后再试")
        if not captured:
            router = page.evaluate("() => window._ROUTER_DATA || null")
            detail = oq.find_nested_detail(router)
            if detail:
                captured["detail"] = detail
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:  # noqa: BLE001
            pass
    detail = captured.get("detail")
    if not detail:
        raise oq.OriginalStreamError(
            "20 秒内没有捕获到 aweme_detail，页面结构可能变了或命中风控兜底")
    return detail


def probe(raw_input: str):
    """只读探测：拿 detail + play 端点 Location + CDN HEAD，不下载视频正文。"""
    session = oq.get_session()
    try:
        detail = capture_detail(raw_input, session)
        oq.assert_detail(detail)
        gear = oq.best_gear(detail["video"])
        stream = session.resolve(oq.video_id_of(detail), gear["size"])
        payload = {
            "aweme_id": str(detail.get("aweme_id") or ""),
            "desc": str(detail.get("desc") or "")[:80],
            "author": (detail.get("author") or {}).get("nickname", ""),
            "video_id": stream.video_id if stream else oq.video_id_of(detail),
            "best_gear": gear["gear_name"],
            "best_gear_size": gear["size"],
            "probe": ({**stream.as_dict(), "best_gear": gear["gear_name"]}
                      if stream else None),
            "note": ("码流未经重编码，但容器与元数据被平台重封装过，"
                     "不等于上传文件的字节副本"),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        oq.close_session()


def download_cdn(url: str, target: str, timeout: int = 120) -> int:
    """规范 Step 7：带 Referer + 正常 UA，流式 1 MB 分块，直链有时效要立刻下。"""
    import requests
    if not oq._allowed_cdn(url):
        raise oq.OriginalStreamError("下载域名不在白名单内：" + url[:120])
    headers = {
        "User-Agent": config.DESKTOP_UA,
        "Referer": oq.REFERER,
        "Accept-Encoding": "identity",
    }
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = target + ".part"
    size = 0
    with requests.get(url, headers=headers, stream=True,
                      timeout=(10, timeout)) as response:
        response.raise_for_status()
        with open(part, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
                    size += len(chunk)
    if size == 0:
        os.remove(part)
        raise oq.OriginalStreamError("下载内容为空，直链可能已过期")
    os.replace(part, target)
    return size


def download(raw_input: str, out_dir: str = ""):
    session = oq.get_session()
    try:
        detail = capture_detail(raw_input, session)
        oq.assert_detail(detail)
        gear = oq.best_gear(detail["video"])
        stream = session.resolve(oq.video_id_of(detail), gear["size"])
        aweme_id = str(detail.get("aweme_id") or "unknown")
        target_dir = out_dir or os.path.join(
            config.DATA_DIR, "original_downloads", aweme_id)
        is_original = bool(stream and stream.is_original)
        url = stream.url if is_original else models.parse_aweme(detail).play_url
        if not url:
            raise oq.OriginalStreamError("既没有原片直链，也没有可用的最高转码档")
        print("判定：%s（factor=%.2f）" % (
            "原始码流" if is_original else "通道未开放，回退最高转码档",
            stream.factor if stream else 0))
        print("下载到：" + target_dir)
        target = os.path.join(target_dir, "视频.mp4")
        size = download_cdn(url, target)
        info = oq.inspect_mp4(target)
        result = {
            "aweme_id": aweme_id,
            "verdict": "original" if is_original else "transcoded",
            "factor": round(stream.factor, 3) if stream else 0,
            "downloaded_bytes": size,
            "file": info,
            "file_verdict": oq.file_verdict(info),
        }
        with open(os.path.join(target_dir, "original_probe.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(json.dumps({k: v for k, v in result.items() if k != "file"},
                         ensure_ascii=False, indent=2))
        print("MP4 结构：" + json.dumps(
            {k: info[k] for k in ("brand", "codecs", "width", "height",
                                  "duration_s", "avg_bitrate_kbps", "metadata")},
            ensure_ascii=False))
        return 0
    finally:
        oq.close_session()


def _brief(info: dict) -> dict:
    return {
        "bytes": info["bytes"], "brand": info["brand"], "codecs": info["codecs"],
        "width": info["width"], "height": info["height"],
        "duration_s": info["duration_s"],
        "avg_bitrate_kbps": info["avg_bitrate_kbps"],
        "capture_time": (info["metadata"] or {}).get("capture_time", ""),
    }


def compare(raw_input: str, out_dir: str = ""):
    """验收清单 4：原片 vs 最高转码档，编码被更换即为重编码硬证据。"""
    session = oq.get_session()
    try:
        detail = capture_detail(raw_input, session)
        oq.assert_detail(detail)
        gear = oq.best_gear(detail["video"])
        stream = session.resolve(oq.video_id_of(detail), gear["size"])
        aweme_id = str(detail.get("aweme_id") or "unknown")
        target_dir = out_dir or os.path.join(
            config.DATA_DIR, "original_downloads", aweme_id)
        os.makedirs(target_dir, exist_ok=True)
        original_path = os.path.join(target_dir, "视频.mp4")
        gear_path = os.path.join(target_dir, "视频-最高转码档.mp4")

        # 原片较大，本地已有且字节数吻合时直接复用，不重复采集。
        expected = int(stream.size) if stream and stream.is_original else 0
        reuse = (os.path.isfile(original_path)
                 and (not expected or os.path.getsize(original_path) == expected))
        if not reuse:
            if not (stream and stream.is_original and stream.url):
                raise oq.OriginalStreamError(
                    "本地没有原片，且该作品拿不到原片直链，无法做对比")
            download_cdn(stream.url, original_path)

        gear_url = models._clean_url(gear["url"])
        if not gear_url:
            raise oq.OriginalStreamError("最高转码档没有可用直链")
        if not os.path.isfile(gear_path):
            download_cdn(gear_url, gear_path)

        a, b = oq.inspect_mp4(original_path), oq.inspect_mp4(gear_path)
        changed = oq.codec_changed(a, b)
        result = {
            "aweme_id": aweme_id,
            "best_gear": gear["gear_name"],
            "declared_gear_size": gear["size"],
            "actual_gear_bytes": b["bytes"],
            "original_reused": reuse,
            "original": _brief(a),
            "transcoded": _brief(b),
            "codec_changed": changed,
            "size_ratio": round(a["bytes"] / b["bytes"], 3) if b["bytes"] else 0,
            "bitrate_ratio": round(a["avg_bitrate_kbps"] / b["avg_bitrate_kbps"], 3)
            if b["avg_bitrate_kbps"] else 0,
            "verdict": stream.verdict if stream else "transcoded",
            "factor": round(stream.factor, 3) if stream else 0,
            "conclusion": (
                "编码被更换，是「最高转码档经过重编码」的硬证据；"
                "原片码流疑似未经重编码（容器/元数据仍被平台重封装）。"
                if changed else
                "两档编码相同，仅凭编码无法证明重编码；请结合体积倍率与元数据判断。"),
        }
        with open(os.path.join(target_dir, "original_compare.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        oq.close_session()


def check(path: str):
    info = oq.inspect_mp4(path)
    info["verdict"] = oq.file_verdict(info)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def regress():
    """规范第 6 节复现基线 + 第 7 节定时回归：全过返回 0，否则返回 1。"""
    ok = True
    print("样本".ljust(16) + "原流体积".rjust(12) + "最高转码档".rjust(14)
          + "倍率".rjust(9) + "  期望 / 实际")
    for name, original_mb, best_mb, expected in SAMPLES:
        verdict, factor = oq.classify(int(original_mb * 1048576),
                                      int(best_mb * 1048576))
        passed = verdict == expected
        ok &= passed
        print("%s%s%s%8.2fx  %s / %s%s" % (
            name.ljust(16), ("%.2f MB" % original_mb).rjust(12),
            ("%.2f MB" % best_mb).rjust(14), factor, expected, verdict,
            "" if passed else "   <== FAIL"))
    # 540p 档比 720p 大的反例：必须按 data_size 而不是分辨率选档
    video = {"bit_rate": [
        {"gear_name": "normal_1080_0", "play_addr": {
            "data_size": 5740000, "url_list": ["https://x.douyinvod.com/1080"]}},
        {"gear_name": "normal_540_0", "play_addr": {
            "data_size": 4990000, "url_list": ["https://x.douyinvod.com/540"]}},
        {"gear_name": "normal_720_0", "play_addr": {
            "data_size": 4180000, "url_list": ["https://x.douyinvod.com/720"]}},
    ]}
    gear = oq.best_gear(video)
    passed = gear["gear_name"] == "normal_1080_0" and gear["size"] == 5740000
    ok &= passed
    print("按 data_size 选档：%s（%s）" % ("OK" if passed else "FAIL", gear["gear_name"]))
    print("回归结果：" + ("全部通过" if ok else "存在失败"))
    return 0 if ok else 1


def login_status():
    path = os.path.join(config.DATA_DIR, "cookies.json")
    try:
        with open(path, encoding="utf-8") as handle:
            cookies = json.load(handle)
        names = {c.get("name", "") for c in cookies if isinstance(c, dict)}
    except (OSError, ValueError) as exc:
        print("读取 %s 失败：%s" % (path, exc))
        return 1
    hit = sorted(names.intersection(oq._LOGIN_COOKIES))
    print("Cookie 文件：%s（%d 条）" % (path, len(cookies)))
    print("登录态标记：%s" % (", ".join(hit) or "无"))
    if not hit:
        print("未登录。请先在程序里点「登录抖音」完成扫码。")
        return 1
    return 0


def main(argv=None):
    cli = argparse.ArgumentParser(description="抖音原始上传码流回捞（本人/授权作品归档）")
    sub = cli.add_subparsers(dest="command", required=True)
    sub.add_parser("login-status", help="检查本地是否已有登录 Cookie")
    p = sub.add_parser("probe", help="只读探测：能否拿到原片（不下载视频）")
    p.add_argument("target", help="作品链接 / 分享文案 / 纯数字 ID")
    p = sub.add_parser("download", help="探测并下载（拿不到原片自动回退最高转码档）")
    p.add_argument("target", help="作品链接 / 分享文案 / 纯数字 ID")
    p.add_argument("--out", default="", help="输出目录")
    p = sub.add_parser("compare", help="原片 vs 最高转码档编码对比（验收清单 4）")
    p.add_argument("target", help="作品链接 / 分享文案 / 纯数字 ID")
    p.add_argument("--out", default="", help="输出目录")
    p = sub.add_parser("check", help="检查已下载 MP4 的编码/元数据/码率")
    p.add_argument("path", help="本地 mp4 路径")
    sub.add_parser("regress", help="离线回归：规范三个实测样本")
    args = cli.parse_args(argv)
    try:
        if args.command == "login-status":
            return login_status()
        if args.command == "probe":
            return probe(args.target)
        if args.command == "download":
            return download(args.target, args.out)
        if args.command == "compare":
            return compare(args.target, args.out)
        if args.command == "check":
            return check(args.path)
        if args.command == "regress":
            return regress()
    except (oq.OriginalStreamError, OSError, ValueError) as exc:
        print("失败：" + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 浏览器/网络异常给明确提示
        print("失败（%s）：%s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
