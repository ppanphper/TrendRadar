/**
 * YAML 列表块的局部改写工具（无 DOM 依赖，浏览器与 Node 共用）
 *
 * 可视化编辑器修改 platforms.sources、rss.feeds 等列表时，只改动真正变化的条目：
 * 未变化的条目原样保留（含注释和未知字段），变化的条目逐个字段替换并保留行尾注释，
 * 新增条目输出全部字段。避免整段重写导致 expected_domain 等字段和注释丢失。
 */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    } else {
        root.YamlBlock = api;
    }
})(typeof self !== 'undefined' ? self : this, function () {
    const ITEM_START = /^(\s*)-(\s+|$)/;
    const FIELD_LINE = /^(\s*(?:-\s+)?)([A-Za-z0-9_.-]+):(.*)$/;

    function indentOf(line) {
        const match = line.match(/^\s*/);
        return match ? match[0].length : 0;
    }

    function isBlank(line) {
        return line.trim() === '';
    }

    function isComment(line) {
        return line.trim().startsWith('#');
    }

    /**
     * 拆分值与行尾注释，忽略引号内的 #。返回 [值部分, 注释部分（含前导空白）]
     */
    function splitInlineComment(text) {
        let quote = null;
        for (let i = 0; i < text.length; i++) {
            const ch = text[i];
            if (quote) {
                if (ch === '\\' && quote === '"') {
                    i++;
                } else if (ch === quote) {
                    quote = null;
                }
                continue;
            }
            if (ch === '"' || ch === "'") {
                quote = ch;
            } else if (ch === '#' && (i === 0 || /\s/.test(text[i - 1]))) {
                let start = i;
                while (start > 0 && /\s/.test(text[start - 1])) start--;
                return [text.slice(0, start), text.slice(start)];
            }
        }
        return [text, ''];
    }

    /**
     * 标量转 YAML 文本；字符串用 JSON 双引号形式（YAML 双引号字符串的子集）。非标量返回 undefined
     */
    function yamlScalar(value) {
        if (typeof value === 'string') return JSON.stringify(value);
        if (typeof value === 'number' && Number.isFinite(value)) return String(value);
        if (typeof value === 'boolean') return String(value);
        if (value === null) return 'null';
        return undefined;
    }

    function yamlValue(value) {
        const scalar = yamlScalar(value);
        // 数组和对象用 JSON 流式写法，也是合法 YAML
        return scalar !== undefined ? scalar : JSON.stringify(value);
    }

    function canonical(value) {
        if (Array.isArray(value)) return value.map(canonical);
        if (value && typeof value === 'object' && !(value instanceof Date)) {
            const out = {};
            Object.keys(value).sort().forEach(key => { out[key] = canonical(value[key]); });
            return out;
        }
        return value;
    }

    function deepEqual(a, b) {
        return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
    }

    function orderedKeys(item, keyField) {
        const keys = Object.keys(item || {});
        return keys.includes(keyField) ? [keyField, ...keys.filter(key => key !== keyField)] : keys;
    }

    /**
     * 生成一个新条目的全部字段行
     */
    function freshItem(item, itemIndent, keyField) {
        const fieldIndent = itemIndent + '  ';
        return orderedKeys(item, keyField).map((key, index) =>
            `${index === 0 ? itemIndent + '- ' : fieldIndent}${key}: ${yamlValue(item[key])}`
        );
    }

    /**
     * 把列表块拆成 header（第一个条目前的行）、条目块和 trailer。
     * 条目之间的空行和与条目同级的注释归入下一个条目；缩进更深的注释归入当前条目。
     */
    function parseChunks(lines) {
        const firstItem = lines.findIndex(line => !isComment(line) && ITEM_START.test(line));
        if (firstItem < 0) {
            return { header: lines.slice(), chunks: [], trailer: [], itemIndent: null };
        }
        const itemIndent = lines[firstItem].match(ITEM_START)[1];
        const chunks = [];
        let pending = [];
        let current = null;
        for (const line of lines.slice(firstItem)) {
            const startsItem = !isComment(line) && indentOf(line) === itemIndent.length && ITEM_START.test(line);
            if (startsItem) {
                current = { leading: pending, body: [line] };
                chunks.push(current);
                pending = [];
            } else if (isBlank(line) || (isComment(line) && indentOf(line) <= itemIndent.length)) {
                pending.push(line);
            } else {
                current.body.push(...pending, line);
                pending = [];
            }
        }
        return { header: lines.slice(0, firstItem), chunks, trailer: pending, itemIndent };
    }

    function fieldIndentOf(body) {
        const match = body[0].match(/^(\s*-\s+)/);
        return ' '.repeat(match ? match[1].length : 2);
    }

    /**
     * 在条目原文上逐字段应用修改；遇到无法安全局部修改的情况返回 null，由调用方整体重写该条目
     */
    function patchItem(body, oldItem, newItem, keyField) {
        const fieldIndent = fieldIndentOf(body);
        const lines = body.slice();
        const fieldLineIndex = key => lines.findIndex((line, index) => {
            const match = line.match(FIELD_LINE);
            if (!match || match[2] !== key || isComment(line)) return false;
            return index === 0 ? ITEM_START.test(line) : indentOf(line) === fieldIndent.length && !ITEM_START.test(line);
        });
        const keys = [...Object.keys(oldItem || {}), ...Object.keys(newItem || {}).filter(key => !(key in (oldItem || {})))];

        for (const key of keys) {
            const inOld = oldItem && key in oldItem;
            const inNew = newItem && key in newItem;
            if (inOld && inNew && deepEqual(oldItem[key], newItem[key])) continue;
            if (inOld && yamlScalar(oldItem[key]) === undefined) return null;
            const index = fieldLineIndex(key);
            if (inNew) {
                const scalar = yamlScalar(newItem[key]);
                if (scalar === undefined) return null;
                if (index >= 0) {
                    const match = lines[index].match(FIELD_LINE);
                    const comment = splitInlineComment(match[3])[1];
                    lines[index] = `${match[1]}${key}: ${scalar}${comment}`;
                } else if (inOld) {
                    return null;
                } else {
                    let insertAt = lines.length;
                    while (insertAt > 1 && (isBlank(lines[insertAt - 1]) || isComment(lines[insertAt - 1]))) insertAt--;
                    lines.splice(insertAt, 0, `${fieldIndent}${key}: ${scalar}`);
                }
            } else if (index > 0) {
                lines.splice(index, 1);
            } else {
                // 删除首行字段或找不到原字段行
                return null;
            }
        }
        if (keyField && newItem && !(keyField in newItem)) return null;
        return lines;
    }

    function collapseBlankRuns(lines) {
        return lines.filter((line, index) => !(isBlank(line) && index > 0 && isBlank(lines[index - 1])));
    }

    /**
     * 根据新旧条目数组改写列表块。
     * blockLines: 列表区域的原始行（含区域开头的注释）
     * oldItems:   blockLines 解析出的条目（与条目块一一对应）
     * newItems:   修改后的条目
     * options:    keyField 用于匹配条目（默认 id）；defaultIndent 为空列表时的条目缩进；
     *             blankLineBetween 新增条目前是否空一行
     */
    function rewriteListItems(blockLines, oldItems, newItems, options = {}) {
        const keyField = options.keyField || 'id';
        const parsed = parseChunks(blockLines);
        const itemIndent = parsed.itemIndent ?? (options.defaultIndent ?? '    ');
        const blankBetween = !!options.blankLineBetween;

        if (parsed.chunks.length !== oldItems.length) {
            // 文本与解析结果对不上（流式写法等），退回整体重写，但仍输出全部字段
            const header = parsed.chunks.length ? parsed.header : blockLines.filter(line => isBlank(line) || isComment(line));
            const body = [];
            newItems.forEach((item, index) => {
                if (blankBetween && index > 0) body.push('');
                body.push(...freshItem(item, itemIndent, keyField));
            });
            return [...header, ...body];
        }

        const used = new Set();
        const matches = newItems.map(item => {
            const key = item ? item[keyField] : undefined;
            const index = oldItems.findIndex((old, i) => !used.has(i) && old && key !== undefined && String(old[keyField]) === String(key));
            if (index >= 0) used.add(index);
            return index;
        });

        // 被删除条目前的注释（例如分组标题）移交给其后第一个保留的条目
        const carried = new Map();
        let pendingCarry = [];
        oldItems.forEach((_, index) => {
            if (used.has(index)) {
                if (pendingCarry.length) carried.set(index, pendingCarry);
                pendingCarry = [];
            } else if (parsed.chunks[index].leading.some(isComment)) {
                pendingCarry = pendingCarry.concat(parsed.chunks[index].leading);
            }
        });

        const output = parsed.header.slice();
        newItems.forEach((item, position) => {
            const index = matches[position];
            if (index >= 0) {
                const chunk = parsed.chunks[index];
                const leading = collapseBlankRuns([...(carried.get(index) || []), ...chunk.leading]);
                const body = deepEqual(oldItems[index], item)
                    ? chunk.body
                    : (patchItem(chunk.body, oldItems[index], item, keyField) || freshItem(item, itemIndent, keyField));
                output.push(...leading, ...body);
            } else {
                if (blankBetween && position > 0) output.push('');
                output.push(...freshItem(item, itemIndent, keyField));
            }
        });
        return [...output, ...pendingCarry.filter(line => !isBlank(line)), ...parsed.trailer];
    }

    /**
     * 改写 `key: [...]` 或块状 `key:\n  - a` 形式的字符串列表，保留键所在行的注释和原有写法
     * 返回新的行数组
     */
    function replaceSequenceValue(lines, keyIndex, list) {
        const match = lines[keyIndex].match(/^(\s*)([^:#\s][^:#]*):(.*)$/);
        if (!match) return lines;
        const indent = match[1];
        const key = match[2].trim();
        const [value, comment] = splitInlineComment(match[3]);

        let end = keyIndex + 1;
        let itemIndent = null;
        if (value.trim() === '') {
            for (let i = keyIndex + 1; i < lines.length; i++) {
                const line = lines[i];
                if (isBlank(line)) continue;
                const deeper = indentOf(line) > indent.length;
                const sameLevelItem = indentOf(line) === indent.length && ITEM_START.test(line);
                if (!deeper && !sameLevelItem) break;
                if (itemIndent === null && !isComment(line) && ITEM_START.test(line)) {
                    itemIndent = line.match(ITEM_START)[1];
                }
                end = i + 1;
            }
        }

        let replacement;
        if (list.length === 0) {
            replacement = [`${indent}${key}: []${comment}`];
        } else if (itemIndent !== null) {
            replacement = [`${indent}${key}:${comment}`, ...list.map(item => `${itemIndent}- ${yamlValue(item)}`)];
        } else {
            replacement = [`${indent}${key}: [${list.map(yamlValue).join(', ')}]${comment}`];
        }
        return [...lines.slice(0, keyIndex), ...replacement, ...lines.slice(end)];
    }

    return {
        splitInlineComment,
        yamlScalar,
        freshItem,
        parseChunks,
        patchItem,
        rewriteListItems,
        replaceSequenceValue,
    };
});
