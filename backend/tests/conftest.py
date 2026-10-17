"""pytest 全局配置（conftest.py）—— 测试开始前先跑两件准备工作。

【测试纪律】断言行为，不断言源码文本：锁事件流/返回值/异常/落库结果，
  不锁配置值、调用计数、内部标志位这类实现细节（防线失效时它们照样绿，只制造安全感）。

【这个文件是干什么的】
  pytest 在收集、导入任何 test_*.py 之前，会先自动加载本文件。
  所以我们把「准备工作」写在这里，就能保证它一定发生在所有测试之前。

【两件准备工作】
  1) 把 backend/ 目录塞进 Python 的模块搜索路径，让测试里能写 `import app.bm25`；
  2) 预置一批「假的环境变量」，把整场测试彻底关在屋里：
     · 不连真实 MySQL / PostgreSQL / Neo4j；
     · 不连 Qdrant 向量库；
     · 不调用真实大模型接口；
     · 不读写项目 data/ 目录下的任何文件（相关路径全部改指到系统临时目录）。
"""

from __future__ import annotations

# 【为什么需要 os】下面要往「环境变量」里塞假值。
import os
# 【为什么需要 sys】下面要手工往模块搜索路径里加 backend/ 目录。
import sys
# 【为什么需要 tempfile】拿一个系统临时目录，用来当测试专用的「沙箱路径」。
import tempfile
# 【为什么需要 Path】拼路径比手工拼字符串可靠，且自动适配 Windows / Linux。
from pathlib import Path

# ============================================================================
# 第 1 件事：让 `import app.xxx` 能找到目标
# ============================================================================
# 【路径怎么算】
#   本文件是 backend/tests/conftest.py
#   · Path(__file__).resolve()          —— 本文件的绝对路径
#   · .parents[0]                        —— 上一级：backend/tests
#   · .parents[1]                        —— 再上一级：backend  ← 我们要的就是它
# 【为什么要加 backend 而不是 backend/app】
#   因为项目里的导入写法是「包名.模块名」，例如 `from .config import Settings`
#   在 app 包内部用相对导入。要让 `import app.bm25` 成立，
#   搜索路径里必须能找到 `app` 这个包，也就是 backend/ 目录本身。
BACKEND_DIR = Path(__file__).resolve().parents[1]
# 【为什么先判断再插入】避免重复调用时把同一个路径塞进去多次（无害但不整洁）。
if str(BACKEND_DIR) not in sys.path:
    # 【为什么插到最前面】保证优先用项目自己的 app 包，而不是别处同名的包。
    sys.path.insert(0, str(BACKEND_DIR))

# ============================================================================
# 第 2 件事：预置假环境变量，保证测试完全离线
# ============================================================================
# 【关键原理，务必看懂】
#   app/config.py 在被导入时，会立刻执行一句 load_dotenv(...) 去读项目根目录的 .env。
#   而 load_dotenv 的默认行为是「不覆盖已经存在的环境变量」。
#   所以：只要我们在它之前（也就是现在）把假值先设好，
#   这些假值就会胜出，.env 里的真实密钥、数据库地址统统不会被用上。
#   这就是「测试完全离线」的实现方式。

# 【造一个测试专用沙箱目录】放在系统临时目录下，跟项目的 data/ 毫无关系。
_SANDBOX = Path(tempfile.gettempdir()) / "bidding_qa_pytest_sandbox"
# 【为什么要 mkdir】后面若真有代码往这些路径写文件，目录得先存在才不会报错。
_SANDBOX.mkdir(parents=True, exist_ok=True)

# 【逐条解释每一个假值为什么这么设】
_FAKE_ENV = {
    # ---- 大模型 ----
    # 端口 9 是 TCP 里的「保留端口」，正常没人监听，因此任何真实请求都会立即失败，
    # 从物理上杜绝「测试偷偷把流量发到线上模型」的可能。
    "LLM_BASE_URL": "http://127.0.0.1:9/v1",
    "LLM_API_KEY": "test",
    "LLM_MODEL": "test-model",
    # ---- MySQL 数据源（测试不连库，填假值即可）----
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_USER": "test",
    "MYSQL_PASSWORD": "test",
    "MYSQL_DATABASE": "test",
    # ---- PostgreSQL：清空 → 配置判定为「未配置」→ 相关工具不会被启用 ----
    "PG_HOST": "",
    "PG_USER": "",
    "PG_PASSWORD": "",
    # ---- Neo4j：同上，清空连接信息 ----
    "NEO4J_URI": "",
    "NEO4J_USER": "",
    "NEO4J_PASSWORD": "",
    # ---- 外部服务密钥：清空 → 联网搜索 / 图片解析工具不会被启用 ----
    "TAVILY_API_KEY": "",
    "ZHIPUAI_API_KEY": "",
    # ---- 会落盘的路径：全部指向临时沙箱，绝不碰项目的 data/ ----
    "QDRANT_PATH": str(_SANDBOX / "qdrant"),
    "BM25_INDEX_PATH": str(_SANDBOX / "bm25_index.json"),
    "HISTORY_DB_PATH": str(_SANDBOX / "conversations.db"),
    # ---- 长期记忆画像：一个用户一个 JSON，同样只落在沙箱 ----
    "MEMORY_DIR": str(_SANDBOX / "memory"),
    # ---- 数据能力边界口径文件：指到沙箱里一个**故意不存在**的路径 →
    #      load_capability() 恒返回 {} → 闸门恒「不拦截」。这样既不会因为
    #      测试进程读到仓库里那份真实口径（里面写着近五年/未来年份会拦）而误伤
    #      存量用例，又能保证「读失败降级为不拦截」这条铁律天天被走到。 ----
    "DATA_CAPABILITY_PATH": str(_SANDBOX / "data_capability.yaml"),
    # ---- 问答缓存 / 指标 / 日志：同样指向沙箱，避免测试污染真实文件 ----
    "CACHE_DB_PATH": str(_SANDBOX / "answer_cache.db"),
    "METRICS_DB_PATH": str(_SANDBOX / "metrics.db"),
    "LOG_FILE_PATH": str(_SANDBOX / "app.log"),
    # ---- 覆盖率闸门放行（阈值 0 = 恒过）：闸门是全局问答策略，
    #      与缓存/模型切换/降级通道等无关用例的「假检索道具」不该被它误伤。
    #      闸门自身的拦截行为由 tests/test_term_coverage.py 直接注入阈值来测。 ----
    "COVERAGE_MIN": "0",
}

# 【为什么用「强制赋值」而不是 setdefault】
#   setdefault 只在键不存在时才写；而这里我们要的是「无论外壳环境里有什么，一律用假值」，
#   所以直接赋值（覆盖），确保测试环境完全可控。
for _key, _value in _FAKE_ENV.items():
    os.environ[_key] = _value