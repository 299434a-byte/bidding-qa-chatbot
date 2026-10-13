
export type IconName =
  | "send"
  | "stop"
  | "copy"
  | "check"
  | "refresh"
  | "regenerate"
  | "locate"
  | "arrow-down"
  | "arrow-up"
  | "chevron-down"
  | "chevron-right"
  | "menu"
  | "x"
  | "plus"
  | "search"
  | "image"
  | "sun"
  | "moon"
  | "monitor"
  | "cpu"
  | "database"
  | "network"
  | "chart"
  | "alert"
  | "panel"
  | "external"
  | "file-text"
  | "message";

const PATHS: Record<IconName, string[]> = {
  send: ["M22 2L11 13", "M22 2l-7 20-4-9-9-4 20-7z"],
  stop: ["M6 6h12v12H6z"],
  copy: ['M9 9h11v11H9z', "M5 15H4V4h11v1"],
  check: ["M20 6L9 17l-5-5"],
  refresh: ["M23 4v6h-6", "M20.49 15a9 9 0 1 1-2.12-9.36L23 10"],
  regenerate: ["M1 4v6h6", "M3.51 15a9 9 0 1 0 2.13-9.36L1 10"],
  locate: ["M9 10l11-7-7 11-2 5-2-9z", "M21 3l-9 9"],
  "arrow-down": ["M12 5v14", "M19 12l-7 7-7-7"],
  "arrow-up": ["M12 19V5", "M5 12l7-7 7 7"],
  "chevron-down": ["M6 9l6 6 6-6"],
  "chevron-right": ["M9 6l6 6-6 6"],
  menu: ["M3 6h18", "M3 12h18", "M3 18h18"],
  x: ["M18 6L6 18", "M6 6l12 12"],
  plus: ["M12 5v14", "M5 12h14"],
  search: ["M21 21l-4.35-4.35", "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z"],
  image: ['M21 15l-5-5L5 21', "M21 19H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11z", "M17 8.5a1 1 0 1 1-2 0 1 1 0 0 1 2 0z"],
  sun: ["M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z", "M12 1v2", "M12 21v2", "M4.22 4.22l1.42 1.42", "M18.36 18.36l1.42 1.42", "M1 12h2", "M21 12h2", "M4.22 19.78l1.42-1.42", "M18.36 5.64l1.42-1.42"],
  moon: ["M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"],
  monitor: ['M2 4h20v12H2z', "M8 20h8", "M12 16v4"],
  cpu: ['M8 8h8v8H8z', "M4 9H2", "M22 9h-2", "M4 15H2", "M22 15h-2", "M9 22V20", "M15 22V20", "M9 4V2", "M15 4V2", 'M5 5h14v14H5z'],
  database: ["M12 3c4.42 0 8 1.34 8 3s-3.58 3-8 3-8-1.34-8-3 3.58-3 8-3z", "M4 6v6c0 1.66 3.58 3 8 3s8-1.34 8-3V6", "M4 12v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"],
  network: ["M6 3v7", "M18 14v7", "M3 6h6", "M15 17h6", "M12 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6z", "M12 20a3 3 0 1 0 0-6 3 3 0 0 0 0 6z", "M9 8l6 8"],
  chart: ["M3 3v18h18", "M7 15l4-5 3 3 5-7"],
  alert: ["M12 9v4", "M12 17h.01", "M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"],
  panel: ['M21 21V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v16', "M15 3v18"],
  external: ["M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6", "M15 3h6v6", "M10 14L21 3"],
  "file-text": ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z", "M14 2v6h6", "M16 13H8", "M16 17H8", "M10 9H8"],
  message: ["M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"],
};

export const ICON_LABELS: Partial<Record<IconName, string>> = {
  send: "发送",
  stop: "停止生成",
  copy: "复制",
  check: "已复制",
  refresh: "刷新",
  regenerate: "重新生成",
  locate: "定位引用来源",
  "arrow-down": "回到最新",
  "arrow-up": "回到顶部",
  "chevron-down": "展开",
  "chevron-right": "进入",
  menu: "打开菜单",
  x: "关闭",
  plus: "新建",
  search: "搜索",
  image: "上传图片",
  sun: "浅色模式",
  moon: "暗色模式",
  monitor: "跟随系统",
  cpu: "模型",
  database: "数据",
  network: "工具",
  chart: "评测",
  alert: "注意",
  panel: "证据面板",
  external: "新窗口打开",
  "file-text": "文档",
  message: "消息",
};

export default function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}