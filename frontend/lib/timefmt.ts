/**
 * lib/timefmt.ts —— 时间戳的「解析 + 展示本地化」
 *
 * 约定（与后端 app/timeutil.py 对齐）：
 *   · 库里只存 UTC（ISO 文本，带 +00:00 偏移）；本文件绝不把本地时间写回库。
 *   · 历史数据里的 naive 文本（"2026-09-29 10:00:00"，不带时区）按东八区解释，
 *     与后端解析老数据的语义一致，避免老记录在前端突然偏移 8 小时。
 *   · 展示时区默认东八区（Asia/Shanghai，固定 +08:00、无夏令时）。
 *     部署时也可以用 TZ=Asia/Shanghai 统一整个进程的本地时区。
 */

/** 展示时区的固定偏移（分钟）：东八区 = +480。 */
const DISPLAY_OFFSET_MIN = 8 * 60;

const _pad = (n: number): string => String(n).padStart(2, "0");

/**
 * 把后端时间戳解析成 Date；解析不了返回 null。
 *
 * 带时区偏移（"Z" 或 "+08:00"）→ 按自带偏移；
 * 不带偏移的 naive 文本 → 视为东八区本地时间（历史数据兼容）。
 */
export function parseTs(value: string | number | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") {
    return Number.isFinite(value) ? new Date(value) : null;
  }
  const text = String(value).trim();
  // 带时区标记（Z / ±HH:MM / ±HHMM）→ 浏览器原生解析即可。
  if (/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) {
    const d = new Date(text);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  // naive 文本：补上 +08:00 再解析（历史数据的语义时区是东八区）。
  const iso = text.replace(" ", "T");
  const d = new Date(`${iso}+08:00`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 按展示时区（东八）取出年月日时分秒各字段。 */
function displayParts(d: Date) {
  const t = new Date(d.getTime() + DISPLAY_OFFSET_MIN * 60_000);
  return {
    y: t.getUTCFullYear(),
    mo: _pad(t.getUTCMonth() + 1),
    dd: _pad(t.getUTCDate()),
    hh: _pad(t.getUTCHours()),
    mi: t.getUTCMinutes(),
  };
}

/**
 * 展示成「YYYY-MM-DD HH:mm」（东八区）。解析不了返回占位符 "—"。
 * 用于历史列表、搜索命中的时间列。
 */
export function shortTime(value: string | number | null | undefined): string {
  const d = parseTs(value);
  if (!d) return "—";
  const p = displayParts(d);
  return `${p.y}-${p.mo}-${p.dd} ${p.hh}:${_pad(p.mi)}`;
}

/**
 * 展示成「HH:mm」（东八区）。解析不了返回空串（消息气泡不显示时间）。
 * 用于消息头像旁的时刻。
 */
export function formatHm(ts: string | number | null | undefined): string {
  const d = parseTs(ts);
  if (!d) return "";
  const p = displayParts(d);
  return `${p.hh}:${_pad(p.mi)}`;
}
