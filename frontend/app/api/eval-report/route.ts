import { NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import path from "node:path";


export const dynamic = "force-dynamic";

export async function GET() {
  const candidates = [
    path.resolve(process.cwd(), "..", "backend", "eval", "report.md"),
    path.resolve(process.cwd(), "backend", "eval", "report.md"),
  ];

  for (const file of candidates) {
    try {
      // turbopackIgnore：目标是仓库里固定位置的评测报告，避免被静态分析判定为
      const markdown = await readFile(/* turbopackIgnore: true */ file, "utf8");
      return NextResponse.json({ available: true, markdown });
    } catch {
    }
  }

  return NextResponse.json({ available: false, markdown: "" });
}
