"use client";

import { useEffect, useState } from "react";
import Icon from "@/components/ui/icons";
import { cycleTheme, getThemePref, setThemePref, type ThemePref } from "@/lib/theme";

/**
 * 主题切换按钮：浅色 / 暗色 / 跟随系统 三态循环。
 * 偏好写入 localStorage；跟随系统时不写 <html data-theme>，交给 CSS 媒体查询决定。
 */
export default function ThemeToggle() {
  const [pref, setPref] = useState<ThemePref>("auto");

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR 水合前不能读 localStorage，主题偏好初始化必须放在 effect 里
    setPref(getThemePref());
  }, []);

  const toggle = () => {
    const next = cycleTheme(pref);
    setThemePref(next);
    setPref(next);
  };

  const icon = pref === "light" ? "sun" : pref === "dark" ? "moon" : "monitor";
  const label = pref === "light" ? "浅色模式" : pref === "dark" ? "暗色模式" : "跟随系统";

  return (
    <button
      className="icon-btn"
      type="button"
      onClick={toggle}
      title={`主题：${label}（点击切换）`}
      aria-label={`主题：${label}，点击切换`}
      data-testid="theme-toggle"
    >
      <Icon name={icon} size={16} />
      <span className="visually-hidden">{label}</span>
    </button>
  );
}