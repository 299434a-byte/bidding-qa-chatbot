import { NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import path from "node:path";

/**
 * 读取后端评测脚本产出的报告（backend/eval/report.md）给后台页展示。
 * 纯本地文件读取，不依赖后端进程；文件不存在时返回 available:false，由前端渲染空态。
 */

export const dynamic = "force-dynamic";

export async function GET() {
  // 开发时 cwd 是 frontend/，构建产物可能在上层，两个候选路径都试一遍
  const candidates = [
    path.resolve(process.cwd(), "..", "backend", "eval", "report.md"),
    path.resolve(process.cwd(), "backend", "eval", "report.md"),
  ];

  for (const file of candidates) {
    try {
      // turbopackIgnore：目标是仓库里固定位置的评测报告，避免被静态分析判定为
      // 「动态文件访问」而把整个工程打进产物（会导致部署体积异常）。
      const markdown = await readFile(/* turbopackIgnore: true */ file, "utf8");
      return NextResponse.json({ available: true, markdown });
    } catch {
      /* 换下一个候选路径 */
    }
  }

  return NextResponse.json({ available: false, markdown: "" });
}
