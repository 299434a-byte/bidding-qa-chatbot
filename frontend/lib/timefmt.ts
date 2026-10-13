
const DISPLAY_OFFSET_MIN = 8 * 60;

const _pad = (n: number): string => String(n).padStart(2, "0");

export function parseTs(value: string | number | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") {
    return Number.isFinite(value) ? new Date(value) : null;
  }
  const text = String(value).trim();
  if (/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) {
    const d = new Date(text);
    return Number.isNaN(d.getTime()) ? null : d;
  }
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
