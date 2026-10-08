'use strict';

// A deliberately small Markdown renderer for model responses. It creates DOM
// nodes directly instead of assigning model output to innerHTML, so raw HTML is
// never executable and no network dependency is required.

function renderMarkdown(target, markdown) {
  const fragment = document.createDocumentFragment();
  const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^\s*```([^`]*)$/);
    if (fence) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      const language = fence[1].trim().split(/\s+/, 1)[0];
      if (language) code.dataset.language = language;
      code.textContent = codeLines.join('\n');
      pre.appendChild(code);
      fragment.appendChild(pre);
      continue;
    }

    const heading = line.match(/^\s*(#{1,4})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const element = document.createElement(`h${heading[1].length}`);
      appendInlineMarkdown(element, heading[2]);
      fragment.appendChild(element);
      index += 1;
      continue;
    }

    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      fragment.appendChild(document.createElement('hr'));
      index += 1;
      continue;
    }

    if (isTableHeader(lines, index)) {
      const table = document.createElement('table');
      const head = document.createElement('thead');
      const headRow = document.createElement('tr');
      splitTableRow(lines[index]).forEach(cell => {
        const th = document.createElement('th');
        appendInlineMarkdown(th, cell);
        headRow.appendChild(th);
      });
      head.appendChild(headRow);
      table.appendChild(head);
      index += 2;
      const body = document.createElement('tbody');
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const row = document.createElement('tr');
        splitTableRow(lines[index]).forEach(cell => {
          const td = document.createElement('td');
          appendInlineMarkdown(td, cell);
          row.appendChild(td);
        });
        body.appendChild(row);
        index += 1;
      }
      table.appendChild(body);
      const wrapper = document.createElement('div');
      wrapper.className = 'markdown-table-wrap';
      wrapper.appendChild(table);
      fragment.appendChild(wrapper);
      continue;
    }

    const listMatch = line.match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
    if (listMatch) {
      const ordered = Boolean(listMatch[2]);
      const list = document.createElement(ordered ? 'ol' : 'ul');
      while (index < lines.length) {
        const item = lines[index].match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
        if (!item || Boolean(item[2]) !== ordered) break;
        const li = document.createElement('li');
        appendInlineMarkdown(li, item[3]);
        list.appendChild(li);
        index += 1;
      }
      fragment.appendChild(list);
      continue;
    }

    if (/^\s*>/.test(line)) {
      const quote = document.createElement('blockquote');
      const quoteLines = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ''));
        index += 1;
      }
      appendInlineMarkdown(quote, quoteLines.join(' '));
      fragment.appendChild(quote);
      continue;
    }

    const paragraphLines = [];
    while (index < lines.length && lines[index].trim() && !startsMarkdownBlock(lines, index)) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    // A line can reach this branch while also starting a block only when a
    // streaming token has left incomplete Markdown. Render it as plain text.
    if (!paragraphLines.length) {
      paragraphLines.push(line.trim());
      index += 1;
    }
    const paragraph = document.createElement('p');
    appendInlineMarkdown(paragraph, paragraphLines.join(' '));
    fragment.appendChild(paragraph);
  }

  target.replaceChildren(fragment);
}

function startsMarkdownBlock(lines, index) {
  const line = lines[index];
  return /^\s*(?:```|#{1,4}\s+|>|(?:[-+*]|\d+[.)])\s+)/.test(line)
    || /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)
    || isTableHeader(lines, index);
}

function isTableHeader(lines, index) {
  if (index + 1 >= lines.length || !lines[index].includes('|')) return false;
  const separator = splitTableRow(lines[index + 1]);
  return separator.length > 0 && separator.every(cell => /^:?-{3,}:?$/.test(cell));
}

function splitTableRow(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
}

function appendInlineMarkdown(parent, text) {
  const tokenPattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\[[^\]\n]+\]\([^\s)]+\)|\*[^*\n]+\*|_[^_\n]+_)/g;
  let position = 0;

  for (const match of String(text).matchAll(tokenPattern)) {
    if (match.index > position) parent.appendChild(document.createTextNode(text.slice(position, match.index)));
    const token = match[0];
    if (token.startsWith('`')) {
      const code = document.createElement('code');
      code.textContent = token.slice(1, -1);
      parent.appendChild(code);
    } else if (token.startsWith('**') || token.startsWith('__')) {
      const strong = document.createElement('strong');
      appendInlineMarkdown(strong, token.slice(2, -2));
      parent.appendChild(strong);
    } else if (token.startsWith('[')) {
      const parts = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const href = parts && safeLink(parts[2]);
      if (parts && href) {
        const link = document.createElement('a');
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        appendInlineMarkdown(link, parts[1]);
        parent.appendChild(link);
      } else {
        parent.appendChild(document.createTextNode(parts ? parts[1] : token));
      }
    } else {
      const emphasis = document.createElement('em');
      appendInlineMarkdown(emphasis, token.slice(1, -1));
      parent.appendChild(emphasis);
    }
    position = match.index + token.length;
  }
  if (position < text.length) parent.appendChild(document.createTextNode(text.slice(position)));
}

function safeLink(value) {
  try {
    const url = new URL(value, window.location.origin);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : null;
  } catch (_) {
    return null;
  }
}

