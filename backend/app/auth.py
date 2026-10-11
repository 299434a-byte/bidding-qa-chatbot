# ============================================================================ #
# 本文件在干什么
# ---------------------------------------------------------------------------- #
# auth.py 是「账号体系」：注册、登录、注销，以及令牌（token）的签发与校验。
# 有了账号，用户的会话历史才能「按人分开存」，并在换设备/换浏览器后凭登录
# 恢复自己的历史记录（跨会话恢复）。
#
# 为什么自己实现、不引第三方库：本题所需功能用 Python 标准库就够（哈希、随机数、
# 线程锁），引库反而增加依赖和升级负担。
#
# ---------------------------------------------------------------------------- #
# 核心安全设计（这是本文件最该理解的部分）
# ---------------------------------------------------------------------------- #
# 【口令（密码）怎么存】绝不明文存！做法：
#   1. 用 PBKDF2-HMAC-SHA256 算法把口令「揉碎」成一串乱码（哈希值）；
#   2. 每个用户配一份「独立的随机盐」——同样的口令，不同用户得到不同哈希，
#      防止「撞库」和彩虹表攻击；
#   3. 迭代次数可配（默认 20 万次），迭代越多越难暴力破解，但登录越慢；
#   4. 比对哈希时用 hmac.compare_digest，它不因「第几位开始不同」而提前返回，
#      可防「时序侧信道」（攻击者靠测量响应时间逐位猜出答案）。
# 【令牌（token）怎么存】用户登录成功后拿到一串随机令牌（类似通行证）。
#   1. 用 secrets.token_urlsafe(32) 生成，随机且够长；
#   2. 数据库里「只存令牌的 SHA-256 哈希」，明文只在登录/注册的响应里返回一次；
#      这样即使数据库被读走，也无法直接拿哈希去冒用身份；
#   3. 令牌带过期时间，注销即删除。
# 【登录失败锁定】同一用户名连续失败太多次，就锁一段时间，防在线暴力破解。
#   注意：这是「进程内」计数（存在内存字典里），重启服务就清零；多进程部署时各自
#   计数、不共享——够用但不完美，属于已知取舍。
#
# ---------------------------------------------------------------------------- #
# 概念预习（大白话）
# ---------------------------------------------------------------------------- #
# 【哈希 / 摘要】= 把任意长度的内容压成固定长度的一串指纹，单向不可逆（不能从指纹
#   反推原文）。SHA-256、PBKDF2 都是这类算法。
# 【盐（salt）】= 掺进哈希过程的一小段随机数据。加了盐，同样的密码也得到不同结果。
# 【线程锁（Lock）】= 多线程同时改同一个字典会出错。锁的作用是「一次只让一个线程进」，
#   保证数据不乱。这里的 _attempts_lock 就是保护失败计数用的。
# 【Header 依赖】= FastAPI 的写法：函数参数声明成 Header(...)，框架会自动从 HTTP
#   请求头里取值传进来（这里是 Authorization 头）。
# 【Bearer 令牌】= HTTP 头里常见格式「Authorization: Bearer xxxxx」，Bearer 后面
#   那串就是令牌。
# 【401 / 503】= 401 = 未登录/登录过期；503 = 服务不可用（这里是「账号体系没开」）。
#
# ---------------------------------------------------------------------------- #
# 硬约束 & 注意事项
# ---------------------------------------------------------------------------- #
# - 这里没有硬编码任何密钥；开关和参数（如 AUTH_ENABLED、迭代次数）都来自 .env。
# - 【性能】口令哈希是 CPU 密集操作（约百毫秒量级）。调用方（main.py）必须用
#   asyncio.to_thread 包起来跑，否则会阻塞整个异步事件循环，拖慢所有请求。
# - 【不打扰匿名】optional_user 在未登录时返回 None 而不抛异常，保证「不登录也能
#   问答」的行为与改造前完全一致。
#
# ---------------------------------------------------------------------------- #
# 阅读顺序指南
# ---------------------------------------------------------------------------- #
#   1. 顶部常量 + 三个自定义异常；
#   2. 口令：hash_password → verify_password → validate_credentials；
#   3. 令牌：issue_token / resolve_token / revoke_token；
#   4. 锁定：_check_lockout / _record_failure / _clear_failures；
#   5. 业务：register / login / logout；
#   6. FastAPI 依赖：_bearer / optional_user / current_user / require_bearer。
# ============================================================================ #
"""账号体系：注册、登录、令牌签发与校验。

为什么要自己实现而不是引第三方：全部用标准库即可满足需求，不引入新依赖。

- 口令：PBKDF2-HMAC-SHA256 + **每用户独立随机盐**，迭代次数可配（默认 20 万），
  比对用 ``hmac.compare_digest`` 防时序侧信道。
- 令牌：``secrets.token_urlsafe(32)``，**库里只存 SHA-256**，明文只在登录/注册响应里返回一次；
  带过期时间，注销即删，因此库被读到也无法直接冒用身份。
- 登录失败锁定：进程内计数 + 锁定窗口，避免被在线暴力破解。

注意：口令哈希是 CPU 密集操作（约百毫秒量级），调用方（``main.py``）用
``asyncio.to_thread`` 包起来跑，别阻塞事件循环。
"""

# 让类型标注延迟求值，兼容性更好。
from __future__ import annotations

# hashlib：提供 SHA-256、PBKDF2 等哈希算法。
import hashlib
# hmac：提供 compare_digest，用于「恒定时间」地比较两个哈希（防时序攻击）。
import hmac
# re：正则表达式，用来校验用户名格式。
import re
# secrets：生成「密码学安全」的随机数（盐、令牌），比 random 更适合安全场景。
import secrets
# threading：提供线程锁，保护进程内的失败计数字典。
import threading
# time：用来计算锁定截止时间、判断是否还在锁定期。
import time
# datetime/timedelta：计算令牌的过期时间点。
from datetime import datetime, timedelta
# 【UTC 改造】timeutil：统一的时间取值/解析工具。存储与比较一律 UTC，
# 历史 naive 文本按东八区解释（详见 app/timeutil.py 文件头说明）。
from . import timeutil
# 只用于类型标注的 Any。
from typing import Any

# FastAPI 的两个东西：
#   Header —— 声明「这个参数从 HTTP 请求头里取」；
#   HTTPException —— 抛出后 FastAPI 会自动返回对应状态码和错误信息。
from fastapi import Header, HTTPException

# history：真正跟数据库打交道（建用户、建会话、查会话等），本文件只做业务与安全逻辑。
from . import history
# Settings/get_settings：读取 .env 配置（开关、迭代次数、令牌有效期等）。
from .config import Settings, get_settings

# 用户名：中英文、数字、下划线，3~32 位
# ^ 行首、$ 行尾；[...] 是字符集；\u4e00-\u9fff 是汉字范围；{3,32} 表示重复 3~32 次。
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,32}$")
# 口令长度限制：下限 8 位（别学 Neo4j 那次「最短 8 位」的坑，这里显式约束并给出中文提示）
# 上限 128 位是防止有人提交超长口令把哈希计算拖慢（也算一种滥用防护）。
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128
# 盐长度（字节）：16 字节 = 128 位，足够随机。
_SALT_BYTES = 16


# 账号体系未开启时抛这个；上层捕获后应返回 503（服务不可用）。
class AuthUnavailable(RuntimeError):
    """账号体系未开启，上层应返回 503。"""


# 注册/登录失败时抛这个。消息写成人话，可直接展示给用户。
class AuthError(RuntimeError):
    """注册/登录失败（用户名已存在、口令错误、被锁定等），消息可直接展示给用户。"""


# 登录失败次数过多时抛这个；它是 AuthError 的子类（也是登录失败的一种）。
class RateLimited(AuthError):
    """登录失败次数过多，暂时锁定。"""


# _require_enabled：检查「账号体系」这个功能开关有没有打开。
# 没打开就抛 AuthUnavailable，让上层返回 503，而不是稀里糊涂继续跑。
def _require_enabled(settings: Settings) -> None:
    if not settings.auth_enabled:
        raise AuthUnavailable("账号体系未开启（AUTH_ENABLED=false）")


# --------------------------------------------------------------------------- #
# 口令
# --------------------------------------------------------------------------- #
# hash_password：把明文口令变成「哈希 + 盐」两串十六进制文本。
# 参数：
#   password —— 明文口令；
#   iterations —— 迭代次数（越大越安全、也越慢）。
# 返回：(哈希十六进制, 盐十六进制)。注意：这俩要一起存库，将来比对时都要用。
def hash_password(password: str, iterations: int) -> tuple[str, str]:
    """返回（哈希十六进制, 盐十六进制）。"""
    # 为这个用户生成一份独立的随机盐（16 字节）。
    # 为什么每题都要新盐：让相同口令产生不同哈希，防止撞库/彩虹表。
    salt = secrets.token_bytes(_SALT_BYTES)
    # PBKDF2-HMAC-SHA256：把「口令 + 盐 + 迭代次数」揉成一段摘要。
    #   password.encode("utf-8") —— 口令是文字，先转成字节；
    #   迭代 iterations 次，故意让计算变慢，增加暴力破解成本。
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    # .hex() 把字节转成十六进制字符串（方便存数据库、打印），两串一起返回。
    return digest.hex(), salt.hex()


# verify_password：校验「用户输入的口令」是否正确。
# 参数：
#   password —— 用户这次输入的明文口令；
#   salt_hex —— 当初存的盐（十六进制）；
#   iterations —— 当初的迭代次数（必须一致，否则算不出来同一个哈希）；
#   expected_hash —— 当初存的正确哈希。
# 返回：True 表示口令正确，False 表示不正确。
def verify_password(password: str, salt_hex: str, iterations: int, expected_hash: str) -> bool:
    try:
        # 把十六进制的盐还原成字节。
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        # 盐格式坏了（数据异常）→ 直接判定失败，不抛异常给上层。
        # 为什么：登录校验不该因脏数据把整个服务搞崩。
        return False
    # 用「同样的盐 + 同样的迭代次数」把用户输入的口令也揉一遍，得到候选哈希。
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    # 「恒定时间」比较：compare_digest 不会因为第几位不同而提前返回，
    # 从而不给攻击者「靠响应时间逐位猜」的机会。
    return hmac.compare_digest(digest.hex(), expected_hash)


# validate_credentials：校验用户名与口令的「格式」是否合规。
# 不合规直接抛 AuthError（附带中文提示，可直接给用户看）。
# 注意：这里只做格式检查，不查数据库（是否已存在由 history.create_user 处理）。
def validate_credentials(username: str, password: str) -> None:
    """校验用户名与口令格式，不合规直接抛 ``AuthError``（中文提示）。"""
    # username or "" 防止 None；match 从开头匹配（配合正则里的 ^...$ 相当于全串校验）。
    if not _USERNAME_RE.match(username or ""):
        raise AuthError("用户名需为 3~32 位的中英文、数字或下划线")
    # 口令太短 → 拒绝（下限 8 位）。
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AuthError(f"口令至少 {MIN_PASSWORD_LEN} 位")
    # 口令太长 → 拒绝（上限 128 位）。
    if len(password) > MAX_PASSWORD_LEN:
        raise AuthError(f"口令不能超过 {MAX_PASSWORD_LEN} 位")


# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #
# _hash_token：把明文令牌做一次 SHA-256，得到「存库用的哈希」。
# 为什么存哈希不存明文：数据库被读走也拿不到能直接用的令牌。
def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# issue_token：签发一个新令牌，并把它的哈希存进数据库，返回明文令牌。
# 参数：
#   settings —— 配置（提供令牌有效期 auth_token_ttl_days）；
#   user_id —— 令牌所属用户；
#   user_agent —— 浏览器信息（可选，便于用户看到「在哪台设备登录的」）。
# 返回：明文令牌（只在这一次返回，之后库里只剩哈希）。
def issue_token(settings: Settings, user_id: str, user_agent: str | None) -> str:
    """签发令牌并落库（只存哈希），返回明文令牌。"""
    # 生成 32 字节的随机令牌，urlsafe 表示用「URL 安全字符」，可安全放进请求头/链接。
    raw = secrets.token_urlsafe(32)
    # 计算过期时间点：现在 + 有效期天数，格式化成 "年-月-日 时:分:秒" 的字符串。
    # 【UTC 改造】过期时间改为「UTC 的 ISO 文本」（带 +00:00 偏移）：
    #   服务器 TZ=UTC 时本地 now() 会与东八差 8 小时，过期判断随之偏移；
    #   统一用 now_utc() 写、用 timeutil.parse_ts 读（老的 naive 文本按东八区解释）。
    expires_at = (timeutil.now_utc() + timedelta(days=settings.auth_token_ttl_days)).isoformat()
    # 落库：存哈希（不是明文）、所属用户、过期时间、设备信息。
    history.create_session(settings, _hash_token(raw), user_id, expires_at, user_agent)
    # 明文只在这返回一次。
    return raw


# resolve_token：根据「明文令牌」找出对应的用户信息。
# 令牌为空/无效/已过期，都返回 None（不抛异常，让调用方自行决定怎么处理）。
def resolve_token(settings: Settings, raw: str | None) -> dict[str, Any] | None:
    """按令牌取用户；令牌为空/无效/过期都返回 None。"""
    # 空令牌 → 直接 None（不用去查库）。
    if not raw:
        return None
    # 先用同样的方式算哈希，再去库里查（库里存的也是哈希，能对上）。
    return history.get_session_user(settings, _hash_token(raw))


# revoke_token：注销令牌（把它从数据库删掉，未来再用就失效了）。
def revoke_token(settings: Settings, raw: str | None) -> None:
    # 有令牌才需要删；空令牌什么都不做。
    if raw:
        history.delete_session(settings, _hash_token(raw))


# --------------------------------------------------------------------------- #
# 登录失败锁定（进程内）
# --------------------------------------------------------------------------- #
# 失败记录表：键=用户名，值=(失败次数, 锁定截止时间戳)。
# 注意这是「进程内」的（存在内存里），重启服务就清零；多进程部署时各自计数。
_attempts: dict[str, tuple[int, float]] = {}
# 线程锁：保护上面这个字典，避免多线程同时读写导致数据错乱。
_attempts_lock = threading.Lock()


# _check_lockout：登录前先检查该用户名是否处于「锁定期」，是就拒绝。
def _check_lockout(settings: Settings, username: str) -> None:
    # 加锁读一下当前记录（get 的默认值 (0, 0.0) 表示「从没失败过」）。
    with _attempts_lock:
        count, locked_until = _attempts.get(username, (0, 0.0))
    # 条件同时满足才算「还在锁定中」：
    #   1) 累计失败次数已达上限；2) 锁定截止时间还没到。
    # 为什么两条都要：次数够但时间已过，说明锁定期满，可以再试。
    if count >= settings.auth_max_login_attempts and locked_until > time.time():
        # 剩余秒数（+1 是为了向上取整，避免显示 0 秒却还没解）。
        remaining = int(locked_until - time.time()) + 1
        raise RateLimited(f"登录失败次数过多，请 {remaining} 秒后再试")


# _record_failure：记一次登录失败；若达到上限，就设置锁定期。
def _record_failure(settings: Settings, username: str) -> None:
    with _attempts_lock:
        # 取当前次数（没有则从 0 开始）。
        count, _ = _attempts.get(username, (0, 0.0))
        # 次数 +1。
        count += 1
        # 三元表达式：达到上限 → 设置锁定截止时间（现在 + 锁定时长）；否则置 0（不锁）。
        locked_until = time.time() + settings.auth_lockout_seconds if count >= settings.auth_max_login_attempts else 0.0
        # 写回。
        _attempts[username] = (count, locked_until)


# _clear_failures：登录成功后清掉该用户的失败记录（重新给他机会）。
def _clear_failures(username: str) -> None:
    with _attempts_lock:
        # pop(键, None)：有就删掉，没有也不报错。
        _attempts.pop(username, None)


# --------------------------------------------------------------------------- #
# 注册 / 登录 / 注销
# --------------------------------------------------------------------------- #
# register：注册新账号。
# 参数：settings 配置；username/password 用户提交的账号口令；user_agent 设备信息。
# 返回：{"token": 明文令牌, "user": {"id":..., "username":...}}。
def register(
    settings: Settings,
    username: str,
    password: str,
    user_agent: str | None = None,
) -> dict[str, Any]:
    # 先确认账号体系已开启。
    _require_enabled(settings)
    # 用户名去首尾空白（避免「 张三 」和「张三」被当成两个）。
    username = (username or "").strip()
    # 校验格式（不合规会抛 AuthError）。
    validate_credentials(username, password)
    # 计算口令哈希与盐（迭代次数来自配置）。
    password_hash, salt = hash_password(password, settings.auth_pbkdf2_iterations)
    # 落库建用户（用户名已存在时 history 会报错，由上层转成提示）。
    user = history.create_user(settings, username, password_hash, salt, settings.auth_pbkdf2_iterations)
    # 顺手签发一个令牌，让用户注册完就是「已登录」状态。
    token = issue_token(settings, user["id"], user_agent)
    return {"token": token, "user": {"id": user["id"], "username": user["username"]}}


# login：登录。
# 返回格式与 register 一致（拿到令牌 + 用户信息）。
def login(
    settings: Settings,
    username: str,
    password: str,
    user_agent: str | None = None,
) -> dict[str, Any]:
    # 确认账号体系已开启。
    _require_enabled(settings)
    # 用户名去空白。
    username = (username or "").strip()
    # 先查是否被锁定（锁定中会抛 RateLimited）。
    _check_lockout(settings, username)

    # 按用户名查用户。
    user = history.get_user_by_username(settings, username)
    # 用户不存在时也走一次哈希计算，避免「响应快慢」泄露账号是否存在
    # 原理：存在与不存在的分支都花差不多的时间，攻击者就无法靠计时判断「这个账号存在吗」。
    if user is None:
        hash_password(password or "", settings.auth_pbkdf2_iterations)
        _record_failure(settings, username)
        raise AuthError("用户名或口令错误")

    # 用户存在：校验口令。注意错误提示统一为「用户名或口令错误」，
    # 不区分「用户不存在」和「口令错」，避免泄露账号是否存在。
    # int(user["iterations"])：数据库取出的值可能不是整数，转一下再用。
    if not verify_password(password or "", user["salt"], int(user["iterations"]), user["password_hash"]):
        _record_failure(settings, username)
        raise AuthError("用户名或口令错误")

    # 登录成功：清失败记录、更新最后登录时间、签发新令牌。
    _clear_failures(username)
    history.touch_login(settings, user["id"])
    token = issue_token(settings, user["id"], user_agent)
    return {"token": token, "user": {"id": user["id"], "username": user["username"]}}


# logout：注销（把当前令牌删掉）。
def logout(settings: Settings, raw: str | None) -> None:
    _require_enabled(settings)
    # 删除令牌对应的会话记录。
    revoke_token(settings, raw)


# --------------------------------------------------------------------------- #
# FastAPI 依赖
# --------------------------------------------------------------------------- #
# 下面这些函数会被 FastAPI 当作「依赖」使用（在接口参数里声明，框架自动调用）。
# _bearer：从 Authorization 请求头里把 Bearer 后面的令牌抠出来。
# 返回：令牌字符串，或 None（头缺失/格式不对时）。
def _bearer(authorization: str | None) -> str | None:
    # 没有这个头 → None。
    if not authorization:
        return None
    # 按空白切一次（最多切成 2 段）：期望得到 ["Bearer", "令牌"]。
    # split(None, 1) 里的 1 表示「只切一刀」，防止令牌里含空格被切碎。
    parts = authorization.split(None, 1)
    # 段数不是 2，或第一段不是 bearer（大小写不敏感）→ 视为无效。
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    # 去掉令牌首尾空白；空串也当没有。
    return parts[1].strip() or None


# optional_user：可选登录的依赖。
# 特点：令牌为空或无效都返回 None，绝不抛异常——保证「不登录也能问答」与改造前一致。
# 参数 authorization 由 FastAPI 从请求头自动注入（不传就是 None）。
def optional_user(
    authorization: str | None = Header(None, description="Bearer 令牌；不传则以匿名身份问答（不落库）"),
) -> dict[str, Any] | None:
    """可选登录：令牌为空或无效都返回 None，**不抛异常** —— 保证匿名问答与改动前一致。"""
    s = get_settings()
    # 账号体系没开 → 直接当匿名（None）。
    if not s.auth_enabled:
        return None
    try:
        # 解析令牌 → 用户信息（无效/过期会返回 None）。
        return resolve_token(s, _bearer(authorization))
    # 兜底：数据库异常也不该让匿名问答失败（吞掉，返回 None）。
    except Exception:  # noqa: BLE001 - 数据库异常不应让匿名问答失败
        return None


# current_user：必须登录的依赖。
# 未登录 → 抛 401；账号体系未开启 → 抛 503。
# 返回：用户信息字典（保证非空）。
def current_user(
    authorization: str | None = Header(None, description="Bearer 令牌"),
) -> dict[str, Any]:
    """必须登录；未登录返回 401，账号体系未开启返回 503。"""
    s = get_settings()
    # 功能没开：抛 503（服务不可用）。注意这里不是 401——是「这个能力不存在」。
    if not s.auth_enabled:
        raise HTTPException(status_code=503, detail="账号体系未开启（AUTH_ENABLED=false）")
    user = resolve_token(s, _bearer(authorization))
    # 没解析出用户 → 401（未登录或过期）。
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录")
    return user


# require_bearer：从请求头里取出「原始令牌」，注销接口用它来删对应会话。
def require_bearer(authorization: str | None) -> str | None:
    """从 Header 里取出原始令牌（注销时用）。"""
    return _bearer(authorization)