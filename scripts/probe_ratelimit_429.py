# ============================================================================
# probe_ratelimit_429.py —— 端到端冒烟：超过限流额度时返回 429
#
# 【原理】问答类接口都挂了令牌桶限流（RATELIMIT_CAPACITY 默认 10、每秒补 0.5）。
#         本探针用零成本的 /api/retrieve（只检索、不调大模型）连发 15 次，
#         前 10 次应 200，之后必须出现 429 且 detail 是可读中文。
# 【前提】后端已在 127.0.0.1:8000 运行。
# 【注意】跑完后本机 IP 的限流桶是空的，真实用户约 20 秒后自动恢复（10 ÷ 0.5）。
# 【用法】python scripts/probe_ratelimit_429.py   （在项目根执行）
# ============================================================================
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _probe_common import API_BASE, http_status, out  # noqa: E402

BURSTS = 15  # > RATELIMIT_CAPACITY(10)，保证把桶打空


def main() -> int:
    url = f"{API_BASE}/api/retrieve?q={quote('限流探针')}"

    codes: list[int] = []
    details_429: list[str] = []
    for i in range(BURSTS):
        code, text = http_status(url, timeout=30)
        codes.append(code)
        if code == 429:
            try:
                details_429.append(json.loads(text).get("detail") or "")
            except json.JSONDecodeError:
                details_429.append(text)
        # 桶打空前不需要等；打空后 429 来得很快。整体不加 sleep（就是要压满）。
        time.sleep(0.05)

    ok_count = sum(1 for c in codes if c == 200)
    out(f"连发 {BURSTS} 次：200×{ok_count}，429×{len(details_429)}，其他：{[c for c in codes if c not in (200, 429)]}")

    if not details_429:
        out("[FAIL] 一次 429 都没有——限流可能没生效（检查 RATELIMIT_CAPACITY / 接口是否挂了 rate_limit）。")
        return 1
    detail = details_429[0]
    if "频繁" not in detail and "限流" not in detail and "稍后" not in detail:
        out(f"[FAIL] 429 的 detail 不是可读的限流提示：{detail!r}")
        return 1
    out(f"[PASS] 超额请求被限流：429 detail={detail[:60]}…（约 20 秒后本机额度自动恢复）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
