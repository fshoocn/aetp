/**
 * 极简 Markdown 渲染（插件描述文件展示用）。
 *
 * 设计取向：
 * - **转义优先**：先把全文 HTML 转义，再做块级 / 行内转换，输出里不可能
 *   混入原始 HTML —— 描述文件来自插件包，不能成为 XSS 入口；
 * - **零依赖**：只覆盖文档常用子集（标题 / 段落 / 列表 / 围栏代码 / 行内
 *   代码 / 粗斜体 / 链接 / 引用 / 分隔线），不引入 markdown 库；
 * - 链接协议白名单（http / https / mailto / 锚点 / 站内相对路径），
 *   其余一律降级为 `#`。
 */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}

/** 链接协议白名单：放行 http(s) / mailto / 锚点 / 站内相对路径。 */
function safeUrl(url) {
    const trimmed = String(url).trim();
    return /^(https?:|mailto:|#|\/|\.{0,2}\/)/i.test(trimmed) ? trimmed : '#';
}

/** 行内元素：代码 / 链接 / 粗体 / 斜体（入参已转义）。 */
function renderInline(escaped) {
    const codes = [];
    let text = escaped.replace(/`([^`]+)`/g, (_match, code) => {
        codes.push(`<code>${code}</code>`);
        return `\uE000${codes.length - 1}\uE000`;
    });
    text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_match, label, url) => `<a href="${safeUrl(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
    return text.replace(/\uE000(\d+)\uE000/g, (_match, index) => codes[Number(index)]);
}

function isBlockStart(line) {
    return /^\s*```/.test(line) || /^(#{1,6})\s+/.test(line) || /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line) || /^\s*>\s?/.test(line) || /^\s*([-*+]|\d+\.)\s+/.test(line);
}

/**
 * 渲染 Markdown 子集为 HTML 字符串（配合 `v-html` 使用，输出已转义）。
 *
 * @param {string} source Markdown 原文
 * @returns {string} HTML 片段
 */
export function renderMarkdown(source) {
    const lines = String(source ?? '')
        .replace(/\r\n?/g, '\n')
        .split('\n');
    const html = [];
    let index = 0;
    while (index < lines.length) {
        const line = lines[index];

        if (/^\s*```/.test(line)) {
            const code = [];
            index += 1;
            while (index < lines.length && !/^\s*```/.test(lines[index])) {
                code.push(lines[index]);
                index += 1;
            }
            index += 1; // 收尾围栏
            html.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
            continue;
        }

        if (!line.trim()) {
            index += 1;
            continue;
        }

        const heading = line.match(/^(#{1,6})\s+(.*)$/);
        if (heading) {
            const level = heading[1].length;
            html.push(`<h${level}>${renderInline(escapeHtml(heading[2]))}</h${level}>`);
            index += 1;
            continue;
        }

        if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
            html.push('<hr>');
            index += 1;
            continue;
        }

        if (/^\s*>\s?/.test(line)) {
            const quoted = [];
            while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
                quoted.push(lines[index].replace(/^\s*>\s?/, ''));
                index += 1;
            }
            html.push(`<blockquote>${renderMarkdown(quoted.join('\n'))}</blockquote>`);
            continue;
        }

        if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
            const tag = /^\s*\d+\./.test(line) ? 'ol' : 'ul';
            const items = [];
            while (index < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[index])) {
                const text = lines[index].replace(/^\s*([-*+]|\d+\.)\s+/, '');
                items.push(`<li>${renderInline(escapeHtml(text))}</li>`);
                index += 1;
            }
            html.push(`<${tag}>${items.join('')}</${tag}>`);
            continue;
        }

        const paragraph = [];
        while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
            paragraph.push(lines[index]);
            index += 1;
        }
        html.push(`<p>${renderInline(escapeHtml(paragraph.join(' ')))}</p>`);
    }
    return html.join('\n');
}
