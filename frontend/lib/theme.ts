
export type ThemePref = "auto" | "light" | "dark";

const KEY = "bidding.theme";

export function getThemePref(): ThemePref {
  if (typeof window === "undefined") return "auto";
  try {
    const value = window.localStorage.getItem(KEY);
    if (value === "light" || value === "dark") return value;
  } catch {
  }
  return "auto";
}

export function applyTheme(pref: ThemePref): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  if (pref === "auto") delete root.dataset.theme;
  else root.dataset.theme = pref;
}

export function setThemePref(pref: ThemePref): void {
  try {
    window.localStorage.setItem(KEY, pref);
  } catch {
  }
  applyTheme(pref);
}

export function cycleTheme(current: ThemePref): ThemePref {
  return current === "auto" ? "light" : current === "light" ? "dark" : "auto";
}

export const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem("bidding.theme");if(t==="light"||t==="dark"){document.documentElement.dataset.theme=t;}}catch(e){}})();`;