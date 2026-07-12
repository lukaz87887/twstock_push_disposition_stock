# -*- coding: utf-8 -*-
"""
notify_line.py — LINE 推播 (文字訊息 + 圖片)

用 LINE Messaging API 的 push message 主動推播。

與 Telegram 的差異:
  • LINE 圖片訊息需要「公開 URL」, 不能直接傳位元組
    → K 線圖先 push 到 GitHub, 用 raw URL 當圖片來源
  • LINE 單次 push 最多 5 則訊息, 要分批
  • 免費方案每月 500 則

需要環境變數:
  LINE_CHANNEL_ACCESS_TOKEN — LINE Developers → Messaging API → Channel access token
  LINE_TO                   — 推播對象 userId (或群組 id)
                              (自己的 userId 可在 LINE Developers Console 看到,
                               或用 webhook 抓; 要先加官方帳號好友)

申請步驟見 DEPLOY_LINE.md
"""
import os
import time
import requests

PUSH_URL = "https://api.line.me/v2/bot/message/push"


def _cfg():
    return (os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
            os.environ.get("LINE_TO", ""))


def _push(messages: list) -> bool:
    """推一批訊息 (LINE 單次最多 5 則)"""
    token, to = _cfg()
    if not token or not to:
        print("[line] 缺 LINE_CHANNEL_ACCESS_TOKEN / LINE_TO")
        return False
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"to": to, "messages": messages[:5]}
    try:
        r = requests.post(PUSH_URL, headers=headers, json=payload, timeout=20)
        if r.status_code == 200:
            return True
        print(f"[line] 推播失敗 HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[line] 推播例外: {e}")
        return False


def send_text(text: str) -> bool:
    """送單則文字 (LINE 單則文字上限 5000 字)"""
    return _push([{"type": "text", "text": text[:4900]}])


def send_image(image_url: str) -> bool:
    """送圖片 (需公開 https URL)"""
    return _push([{
        "type": "image",
        "originalContentUrl": image_url,
        "previewImageUrl": image_url,
    }])


def send_batch(messages: list) -> bool:
    """一次送多則 (自動分批, 每批 5 則)"""
    ok = True
    for i in range(0, len(messages), 5):
        batch = messages[i:i + 5]
        if not _push(batch):
            ok = False
        time.sleep(0.3)
    return ok


# ==================================================================
#   組裝: 處置股推播 (LINE 版)
# ==================================================================
def push_disposal_line(result: dict, chart_urls: dict = None):
    """
    LINE 版處置股推播: 統整成「一則訊息 + 一張合併大圖」。
    chart_urls: {"__combined__": 公開圖片URL} — 合併大圖已 push 到 GitHub 的 raw URL
    """
    items = result.get("items", [])
    added = result.get("added_today", [])
    removed = result.get("removed_today", [])
    chart_urls = chart_urls or {}
    data_date = result.get("data_date", "")

    if not items:
        send_text(f"🚨 處置股提醒 ({data_date})\n目前無處置生效中的普通股")
        return

    # ---- 統整成單一則訊息 ----
    msg = [f"🚨 處置股提醒 ({data_date})",
           f"處置生效中共 {len(items)} 檔"]
    if added:
        msg.append(f"\n🆕 本日新增 {len(added)} 檔:")
        for it in added:
            mk = "櫃" if it.get("market") == "TWO" else "市"
            msg.append(f"  ➕ {it['code']}({mk}) {it['name']} 至{it['disposal_end']}")
    else:
        msg.append("\n🆕 本日新增: 無")
    if removed:
        _rm = []
        for r in removed:
            if isinstance(r, dict):
                nm = r.get("name", "")
                _rm.append(f"{r['code']}{nm}" if nm else r["code"])
            else:
                _rm.append(str(r))
        msg.append(f"\n✅ 本日出關 {len(removed)} 檔: " + "、".join(_rm))
    else:
        msg.append("\n✅ 本日出關: 無")

    msg.append(f"\n📈 距月線排序 (🔴≤2% 🟡≤5% ⚪>5%)")
    for r in items[:30]:
        mk = "櫃" if r.get("market") == "TWO" else "市"
        meas = r.get("measure", "")
        meas_s = f" ({meas})" if meas else ""
        msg.append(f"{r['color']} {r['code']}({mk}) {r['name']} "
                   f"距月線{r['diff_pct']:+.1f}%\n"
                   f"   處置 {r['disposal_start']}~{r['disposal_end']}{meas_s}")

    messages = [{"type": "text", "text": "\n".join(msg)[:4900]}]

    # ---- 一張合併大圖 ----
    combined_url = chart_urls.get("__combined__")
    if combined_url:
        messages.append({
            "type": "image",
            "originalContentUrl": combined_url,
            "previewImageUrl": combined_url,
        })

    send_batch(messages)
