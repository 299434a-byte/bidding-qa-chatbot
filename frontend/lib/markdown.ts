
export function escapeHtml(input: string): string {
  return input
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export type MdOptions = {
  citeMax?: number;
};

const CITE = "\u0001";

function inline(text: string, citeMax: number): string {
  let out = escapeHtml(text);

  out = out.replace(/`([^`]+)`/g, '<code class="inline">$1</code>');
  // 加粗 / 斜体 / 链接
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');

  // 行内引用 [1]：链接转换完之后剩下的纯数字方括号才是引用。
  // 编号超出来源总数的（模型幻觉编号）保持纯文本，不造假指向。
  if (citeMax > 0) {
    out = out.replace(/\[参考资料(\d+)\]|\[参考(\d+)\]/g, (match: string, full?: string, short?: string) => {
      const num = full ?? short ?? "";
      const n = Number(num);
      if (n < 1 || n > citeMax) return match;
      return `${CITE}${num}${CITE}`;
    });
  }
  return out;
}

/** 把哨兵替换成引用胶囊。 */
function wrapCites(html: string): string {
  return html.replace(
    new RegExp(`${CITE}(\\d+)${CITE}`, "g"),
    '<button type="button" class="cite" data-cite="$1">[$1]</button>',
  );
}

/** 管道表格 → HTML 表格（外层 .md-table 横向滚动）。 */
function tableToHtml(rows: string[][], citeMax: number): string {
  const [header, ...body] = rows;
  const thead = `<thead><tr>${header
    .map((cell) => `<th>${inline(cell.trim(), citeMax)}</th>`)
    .join("")}</tr></thead>`;
  const tbody = body.length
    ? `<tbody>${body
        .map((row) => `<tr>${row.map((cell) => `<td>${inline(cell.trim(), citeMax)}</td>`).join("")}</tr>`)
        .join("")}</tbody>`
    : "";
  return `<div class="md-table"><table>${thead}${tbody}</table></div>`;
}

function isTableSeparator(line: string): boolean {
  return /^\s*\|?[\s:|-]+\|?\s*$/.test(line) && line.includes("-");
}

function splitTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|");
}

export function mdToHtml(markdown: string, options: MdOptions = {}): string {
  const citeMax = options.citeMax ?? 0;
  const blocks: string[] = [];
  const marker = "\u0000";

  // 先抽出代码块，避免内部内容被后续规则误伤
  const text = markdown.replace(/```[a-zA-Z0-9]*\n?([\s\S]*?)```/g, (_m, code: string) => {
    const escaped = escapeHtml(code.replace(/\n$/, ""));
    blocks.push(
      `<div class="code-block"><div class="code-block-head"><span>代码</span>` +
        `<button type="button" class="code-copy" aria-label="复制代码">复制</button></div>` +
        `<pre class="code"><code>${escaped}</code></pre></div>`,
    );
    return `${marker}${blocks.length - 1}${marker}`;
  });

  const lines = text.split("\n");
  const out: string[] = [];
  let listType: "ul" | "ol" | null = null;

  const closeList = () => {
    if (listType) {
      out.push(`</${listType}>`);
      listType = null;
    }
  };

  for (let i = 0; i < lines.length; i += 1) {
    const rawLine = lines[i];
    const line = rawLine.trimEnd();

    // 代码块占位
    const ph = line.match(new RegExp(`^${marker}(\\d+)${marker}$`));
    if (ph) {
      closeList();
      out.push(blocks[Number(ph[1])]);
      continue;
    }
    if (!line.trim()) {
      closeList();
      continue;
    }

    // 表格：以 | 开头且下一行是分隔行
    if (line.trim().startsWith("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      closeList();
      const tableRows: string[][] = [splitTableRow(line.trim())];
      i += 1; // 跳过分隔行
      while (i + 1 < lines.length && lines[i + 1].trim().startsWith("|") && lines[i + 1].trim()) {
        i += 1;
        tableRows.push(splitTableRow(lines[i].trim()));
      }
      out.push(tableToHtml(tableRows, citeMax));
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = Math.min(heading[1].length, 4);
      out.push(`<h${level}>${inline(heading[2], citeMax)}</h${level}>`);
      continue;
    }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      closeList();
      out.push(`<blockquote>${inline(quote[1], citeMax)}</blockquote>`);
      continue;
    }

    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    if (bullet) {
      if (listType !== "ul") {
        closeList();
        out.push("<ul>");
        listType = "ul";
      }
      out.push(`<li>${inline(bullet[1], citeMax)}</li>`);
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ordered) {
      if (listType !== "ol") {
        closeList();
        out.push("<ol>");
        listType = "ol";
      }
      out.push(`<li>${inline(ordered[1], citeMax)}</li>`);
      continue;
    }

    closeList();
    out.push(`<p>${inline(line, citeMax)}</p>`);
  }
  closeList();

  return wrapCites(out.join("\n"));
}