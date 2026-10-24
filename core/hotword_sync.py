"""两端共用的热词格式、三方合并与原子存储；须兼容客户端 Python 3.8。"""

import difflib
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

CLIENT_FILES = ('hot.txt', 'hot-rule.txt')
FILES = CLIENT_FILES + ('hot-server.txt',)
SYNC_PATH = '/hotword-sync'
VERSION = 1
CONFLICT_HEADER = (
    '# === CapsWriter 冲突处理说明（不会写入正式词库） ===\n'
    '# 处理正文的全部 <<<<<<<、=======、>>>>>>> 标记。\n'
    '# 可以保留任意一侧，或自行编辑为最终内容；空的一侧表示删除。\n'
    '# 删除所有标记后保存，程序会检查格式并自动提交。\n'
    '# 服务端确认保存且本机正式词库更新后，本文件会永久删除。\n'
    '# 未处理完或离线时请保留本文件；正式词库仍可使用。\n'
    '# === 正式内容开始 ===\n'
)


def read_text(path):
    # 保留换行风格，utf-8-sig 只在读取时兼容手工编辑器产生的 BOM。
    with open(str(path), encoding='utf-8-sig', newline='') as stream:
        return stream.read()


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def revision(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def conflict_text(local, remote):
    def terminated(text):
        return text + ('\n' if text and not text.endswith('\n') else '')
    return ('<<<<<<< 本机修改\n' + terminated(local) + '=======\n' +
            terminated(remote) + '>>>>>>> 服务端修改\n')


def resolved_text(text):
    for header in (CONFLICT_HEADER, CONFLICT_HEADER.replace('\n', '\r\n')):
        if text.startswith(header):
            text = text[len(header):]
            break
    if any(line.startswith(('<<<<<<<', '=======', '>>>>>>>')) for line in text.splitlines()):
        raise ValueError('仍有冲突标记')
    return text


def _entry(name, line):
    stripped = line.strip()
    if not stripped or stripped.startswith('#'):
        return None
    if name == 'hot-rule.txt':
        # 与 RuleCorrector 的分隔规则一致，包括空替换式。
        body = line.rstrip('\r\n')
        if body.endswith(' ='):
            body += ' '
        parts = body.split(' = ')
        if len(parts) != 2 or not parts[0].strip():
            raise ValueError('规则须使用带空格的 = 分隔模式和替换式')
        pattern, replacement = (part.strip() for part in parts)
        compiled = re.compile(pattern)
        compiled.sub(replacement.replace(r'\s', ' '), '')
        return pattern, replacement
    parts = [part.strip() for part in stripped.split('|')]
    if not parts[0] or any(not part for part in parts):
        raise ValueError('热词及别名不能为空')
    if name == 'hot.txt':
        return parts[0], tuple(dict.fromkeys(parts[1:]))
    if len(parts) > 2:
        raise ValueError('服务端热词格式为 词 | 加分')
    bonus = float(parts[1]) if len(parts) == 2 else None
    if bonus is not None and not math.isfinite(bonus):
        raise ValueError('热词加分必须是有限数字')
    return parts[0], bonus


def _document(name, text):
    values, lines, layout = {}, {}, []
    for number, line in enumerate(text.splitlines(True), 1):
        try:
            entry = _entry(name, line)
        except (ValueError, re.error, IndexError) as error:
            raise ValueError('{} 第 {} 行：{}'.format(name, number, error))
        if entry is None:
            layout.append(('text', line))
            continue
        key, value = entry
        if key not in values:
            layout.append(('entry', key))
            lines[key] = line
            values[key] = value
        elif name == 'hot.txt':
            values[key] = tuple(dict.fromkeys(values[key] + value))
        elif name == 'hot-rule.txt':
            # 原解析器同一模式的最后一个替换式生效，去重保持这个行为。
            values[key] = value
            lines[key] = line
        # 服务端解析器对重复词使用首次出现的加分。
    return values, lines, layout


def validate(name, text):
    if name not in FILES or not isinstance(text, str):
        raise ValueError('不支持的热词文件')
    resolved_text(text)
    _document(name, text)


def _render(name, key, value, candidates):
    for line in candidates:
        if line and _entry(name, line) == (key, value):
            return line
    template = next((line for line in candidates if line), '')
    indent = template[:len(template) - len(template.lstrip())]
    newline = '\r\n' if template.endswith('\r\n') else '\n'
    if name == 'hot.txt':
        body = ' | '.join((key,) + value)
    elif name == 'hot-rule.txt':
        body = key + ' = ' + value
    else:
        body = key if value is None else key + ' | ' + format(value, 'g')
    return indent + body + newline


def _layout_merge(base, local, remote):
    """对排版做三方合并；同一位置的新增保序合并，重叠改写交给调用者。"""
    if local == remote or remote == base:
        return local, False
    if local == base:
        return remote, False
    edits = []
    for side, target in enumerate((local, remote)):
        for tag, i, j, x, y in difflib.SequenceMatcher(a=base, b=target, autojunk=False).get_opcodes():
            if tag != 'equal':
                edits.append((i, j, side, target[x:y]))
    edits.sort(key=lambda edit: (edit[0], edit[1]))
    output, cursor = [], 0
    while edits:
        group = [edits.pop(0)]
        start, end = group[0][:2]
        while edits and (edits[0][0] < end or edits[0][0] == start):
            item = edits.pop(0)
            group.append(item)
            end = max(end, item[1])
        output.extend(base[cursor:start])
        variants = []
        for side in (0, 1):
            part, pos = [], start
            for i, j, which, replacement in group:
                if which == side:
                    part.extend(base[pos:i])
                    part.extend(replacement)
                    pos = j
            part.extend(base[pos:end])
            variants.append(part)
        left, right = variants
        original = base[start:end]
        if left == right or right == original:
            output.extend(left)
        elif left == original:
            output.extend(right)
        elif start == end:
            output.extend(left)
            output.extend(item for item in right if item not in left)
        elif all(item[0] == 'text' for item in left + right + original):
            output.extend(left)
            output.extend(item for item in right if item not in left)
        else:
            return [], True
        cursor = end
    output.extend(base[cursor:])
    return output, False


def merge(name, base, local, remote, default_bonus=4.0):
    """返回 (内容, 有冲突)。base=None 表示首次同步，双方现有内容都保留。"""
    validate(name, local)
    validate(name, remote)
    b, bl, layout_b = _document(name, base or '')
    l, ll, layout_l = _document(name, local)
    r, rl, layout_r = _document(name, remote)
    missing = object()
    result, conflicted = {}, False
    for key in dict.fromkeys(list(l) + list(r) + list(b)):
        bv, lv, rv = b.get(key, missing), l.get(key, missing), r.get(key, missing)
        if lv == rv or rv == bv:
            value = lv
        elif lv == bv:
            value = rv
        elif name == 'hot.txt' and lv is not missing and rv is not missing:
            aliases = set(bv) if bv is not missing else set()
            # 一侧删除、另一侧不变的别名不复活；新增别名按原顺序并入。
            keep = (set(lv) & set(rv)) | (set(lv) - aliases) | (set(rv) - aliases)
            value = tuple(alias for alias in dict.fromkeys(lv + rv) if alias in keep)
        elif name == 'hot-server.txt':
            if lv is missing or rv is missing:
                value = rv if lv is missing else lv
            else:
                value = min((lv, rv), key=lambda v: default_bonus if v is None else v)
        else:
            result[key] = conflict_text(ll.get(key, ''), rl.get(key, ''))
            conflicted = True
            continue
        result[key] = '' if value is missing else _render(name, key, value, (ll.get(key), rl.get(key), bl.get(key)))

    layout, layout_conflict = _layout_merge(layout_b, layout_l, layout_r)
    if layout_conflict:
        if name == 'hot-server.txt':
            layout = layout_l + [item for item in layout_r if item not in layout_l]
        else:
            return conflict_text(local, remote), True
    # 已被一侧删除的行可能不在合并后的排版里，但删除/修改冲突仍必须出现。
    present = {key for kind, key in layout if kind == 'entry'}
    layout.extend(('entry', key) for key in result if key not in present and result[key])
    output, seen = [], set()
    for kind, value in layout:
        if kind == 'text':
            output.append(value)
        elif value not in seen:
            seen.add(value)
            output.append(result[value])
    # 无末尾换行的原文件追加条目时，不能把两个词粘成同一行。
    text = ''
    for part in output:
        if part and text and not text.endswith(('\n', '\r')):
            text += '\n'
        text += part
    return text, conflicted
