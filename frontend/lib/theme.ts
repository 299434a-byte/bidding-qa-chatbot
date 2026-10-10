/** 主题管理：跟随系统 + 手动切换（浅色 / 暗色），偏好写入 localStorage。 */

export type ThemePref = "auto" | "light" | "dark";

const KEY = "bidding.theme";

/** 读取当前偏好（默认 auto = 跟随系统）。 */
export function getThemePref(): ThemePref {
  if (typeof window === "undefined") return "auto";
  try {
    const value = window.localStorage.getItem(KEY);
    if (value === "light" || value === "dark") return value;
  } catch {
    /* 隐私模式读取失败，退回跟随系统 */
  }
  return "auto";
}

/** 把偏好落到 <html data-theme>（auto 时不写属性，由 CSS 跟随系统）。 */
export function applyTheme(pref: ThemePref): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  if (pref === "auto") delete root.dataset.theme;
  else root.dataset.theme = pref;
}

/** 写入偏好并立即生效。 */
export function setThemePref(pref: ThemePref): void {
  try {
    window.localStorage.setItem(KEY, pref);
  } catch {
    /* 隐私模式写入失败：本轮会话内仍生效，只是不持久化 */
  }
  applyTheme(pref);
}

/** 三态循环：自动 → 浅色 → 暗色 → 自动。 */
export function cycleTheme(current: ThemePref): ThemePref {
  return current === "auto" ? "light" : current === "light" ? "dark" : "auto";
}

/**
 * 首屏初始化脚本（inline 进 <head>，在 React 挂载前执行，避免闪白）。
 * 不依赖任何框架，字符串形式注入。
 */
export const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem("bidding.theme");if(t==="light"||t==="dark"){document.documentElement.dataset.theme=t;}}catch(e){}})();`;