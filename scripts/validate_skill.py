#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""亲子沟通军师 · skill 自检脚本

一条命令跑完发布前该核的硬约束，不依赖任何第三方库。

用法::

    python scripts/validate_skill.py           # 跑全部检查
    python scripts/validate_skill.py --fix     # 先清掉富文本污染痕迹，再跑检查
    python scripts/validate_skill.py --quiet   # 只打结论和失败项

退出码：0 = 全部通过（可有 WARN）；1 = 存在 FAIL。

为什么需要这个脚本：SKILL.md 曾被富文本编辑器（语雀类）粘回，把开头的
``---`` 转义成 ``\\---``、把 tags 的 ``-`` 转义成 ``\\-``，导致 YAML
解析失败、description 读不出来。这种伤肉眼几乎看不出，必须靠机器查。
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "SKILL.md")
README = os.path.join(ROOT, "README.md")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
REFS = os.path.join(ROOT, "references")

DOMAINS = [
    "00-frame", "01-child", "02-parent", "03-bond", "04-family",
    "05-outside", "06-scenes", "07-safety", "08-archive",
]

REQUIRED_SECTIONS = ["机制", "识别", "做法", "边界", "依据说明", "参考文献"]
REQUIRED_META = ["适用对象", "生态层", "适用时机", "一句话范围", "相关文件"]
DISCLAIMER = "这不是诊断，也不替代专业帮助。"
REQUIRED_FM = [
    "name", "slug", "displayName", "display_name", "summary", "description",
    "description_zh", "description_en", "tags", "category", "version",
    "author", "license",
]
# 全库唯一不列文献条目的文件（元规则文件，已在构建规范 §3.4 备案）
REF_EXEMPT = {"00-frame/术语与使用方法.md"}

# 标题疑问特征（与构建规范 §3.1 同判据：读起来要先回答一遍才懂的标题）
QUESTION_START = ("为什么", "怎么", "如何", "什么算", "什么", "是否", "要不要", "能不能", "该不该")

results = []   # (level, name, detail)  level: PASS / FAIL / WARN / SKIP


def record(level, name, detail=""):
    results.append((level, name, detail))


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def norm(text):
    """检查一律按 LF 做；写回时再还原原文件的换行符，避免把 CRLF 静默改成 LF。"""
    return text.replace("\r\n", "\n")


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# --------------------------------------------------------------------------
# frontmatter
# --------------------------------------------------------------------------

def split_frontmatter(text):
    """返回 (frontmatter 文本 或 None, 正文)。只认第一行就是 --- 的情况。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def parse_frontmatter(fm):
    """够用的极简 YAML 子集解析：key: value 与 key: 下的 - item 列表。"""
    data = {}
    key = None
    for raw in fm.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if re.match(r"^\s*-\s+", raw) and key:
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(re.sub(r"^\s*-\s+", "", raw).strip())
            continue
        m = re.match(r"^([A-Za-z_][\w\-]*)\s*:\s*(.*)$", raw)
        if m:
            key, val = m.group(1), m.group(2).strip()
            data[key] = val if val else []
    return data


# --------------------------------------------------------------------------
# 各项检查
# --------------------------------------------------------------------------

def check_frontmatter(text):
    fm, body = split_frontmatter(text)
    if fm is None:
        record("FAIL", "frontmatter 可解析", "首行不是 ---，SKILL.md 没有合法 frontmatter")
        return {}, body
    data = parse_frontmatter(fm)
    missing = [k for k in REQUIRED_FM if k not in data]
    if missing:
        record("FAIL", "frontmatter 可解析", "缺字段：" + "、".join(missing))
    else:
        record("PASS", "frontmatter 可解析", "%d 个字段" % len(data))
    if not isinstance(data.get("tags"), list) or not data.get("tags"):
        record("FAIL", "tags 是列表", "tags 解析不出列表项")
    else:
        record("PASS", "tags 是列表", "%d 项" % len(data["tags"]))
    return data, body


def mask_code(text):
    """把围栏代码块与行内代码替换成空，保留行数。

    文档里合法地写着 ``\\---`` 这种被转义的写法（就是在讲这个坑），
    不屏蔽掉就会把自己讲道理的示例当成污染报出来。
    """
    out, in_fence = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append("")
        elif in_fence:
            out.append("")
        else:
            out.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(out)


def check_pollution(files):
    """富文本污染：转义符与内联样式是 FAIL，行尾空格只算 WARN。"""
    hard, soft = [], []
    for path, raw in files:
        text = mask_code(norm(raw))
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        fm, body = split_frontmatter(text)
        zones = [("frontmatter", fm or ""), ("body", body)]
        for zone, chunk in zones:
            for no, line in enumerate(chunk.split("\n"), 1):
                if "\\---" in line:
                    hard.append("%s %s 第%d行：字面 \\---" % (rel, zone, no))
                if line.lstrip().startswith("\\- "):
                    hard.append("%s %s 第%d行：列表项被转义为 \\- " % (rel, zone, no))
                if "<span" in line or "</span>" in line:
                    hard.append("%s %s 第%d行：残留 <span> 高亮" % (rel, zone, no))
                if line.endswith("  ") and not line.rstrip().endswith(">"):
                    soft.append("%s 第%d行" % (rel, no))
    if hard:
        record("FAIL", "富文本污染", "%d 处（见下方明细）" % len(hard))
        for item in hard[:12]:
            print("        · " + item)
    else:
        record("PASS", "富文本污染", "无转义符、无 span 残留")
    if soft:
        record("WARN", "行尾多余空格", "%d 行（不影响解析，可留）" % len(soft))
    else:
        record("PASS", "行尾多余空格", "无")


def check_version(fm):
    v = fm.get("version", "")
    problems = []
    if not re.match(r"^\d+\.\d+\.\d+$", str(v)):
        problems.append("SKILL.md version 不是 x.y.z：%r" % v)
    try:
        head = read(CHANGELOG).split("\n")
        top = next((l for l in head if re.match(r"^##\s*v\d", l)), "")
        cv = re.sub(r"^##\s*v", "", top).strip()
        if cv != v:
            problems.append("CHANGELOG 顶部是 %r，与 %r 不一致" % (cv, v))
    except OSError:
        problems.append("读不到 CHANGELOG.md")
    try:
        rtext = read(README)
        m = re.search(r"version-(\d+\.\d+\.\d+)", rtext)
        if not m:
            problems.append("README 里找不到 version-x.y.z 徽章")
        elif m.group(1) != v:
            problems.append("README 徽章是 %s，与 %s 不一致" % (m.group(1), v))
    except OSError:
        problems.append("读不到 README.md")
    if problems:
        record("FAIL", "版本号三处一致", "；".join(problems))
    else:
        record("PASS", "版本号三处一致", v)


def top_markdown():
    """仓库根目录下的 md（SKILL / README / CHANGELOG / CONTRIBUTING / SECURITY …）。"""
    return [os.path.join(ROOT, n)
            for n in ("SKILL.md", "README.md", "CHANGELOG.md",
                      "CONTRIBUTING.md", "SECURITY.md", "README_EN.md")
            if os.path.exists(os.path.join(ROOT, n))]


def collect_ref_files():
    out = []
    for d in DOMAINS:
        p = os.path.join(REFS, d)
        if not os.path.isdir(p):
            continue
        for name in sorted(os.listdir(p)):
            if name.endswith(".md"):
                out.append((d, name, os.path.join(p, name)))
    return out


def check_domains():
    missing = [d for d in DOMAINS if not os.path.isdir(os.path.join(REFS, d))]
    if missing:
        record("FAIL", "9 个域目录齐全", "缺：" + "、".join(missing))
    else:
        record("PASS", "9 个域目录齐全", "%d 个" % len(DOMAINS))


def check_reference_file(domain, name, path, verbose):
    rel = "%s/%s" % (domain, name)
    text = norm(read(path))
    body = text
    lines = [l for l in text.split("\n")]
    failures = []

    # 骨架
    heads = [re.sub(r"^##\s+", "", l).strip() for l in lines if l.startswith("## ")]
    lacking = [s for s in REQUIRED_SECTIONS if s not in heads]
    extra = [h for h in heads if h not in REQUIRED_SECTIONS + ["升级信号"]]
    if lacking:
        failures.append("缺章节 " + "、".join(lacking))
    if extra:
        failures.append("多余章节 " + "、".join(extra))

    # 末行免责
    tail = [l.strip() for l in lines if l.strip()]
    if not tail or not tail[-1].endswith(DISCLAIMER):
        failures.append("末行不是免责句")

    # 头部元信息
    head_zone = "\n".join(lines[:16])
    missing_meta = [m for m in REQUIRED_META if ("%s：" % m) not in head_zone]
    if missing_meta:
        failures.append("头部缺 " + "、".join(missing_meta))

    # 占位符
    for token in ("<待确认>", "TODO", "待补", "XXX"):
        if token in text:
            failures.append("残留占位符 " + token)

    # 疑问句标题
    for l in lines:
        if not l.startswith("#"):
            continue
        title = re.sub(r"^#+\s*", "", l).strip()
        if title.startswith(QUESTION_START) and ("？" in title or "?" in title):
            failures.append("疑问句标题：" + title)
            break

    # 文献
    idx = text.find("## 参考文献")
    if idx >= 0:
        ref_zone = text[idx:]
        body_zone = text[:idx]
        entries = re.findall(r"^［([^］]{1,12})］", ref_zone, re.M)
        lab_paper = [e for e in entries if re.match(r"^\d+$", e)]
        lab_book = [e for e in entries if re.match(r"^B\d+$", e)]
        lab_law = [e for e in entries if re.match(r"^L\d+$", e)]
        lab_other = [e for e in entries if e not in lab_paper + lab_book + lab_law]
        total = len(entries)
        if rel not in REF_EXEMPT:
            if total < 6:
                failures.append("文献 %d 条（要求 ≥6）" % total)
            if len(lab_paper) < 3:
                failures.append("论文 %d 条（要求 ≥3）" % len(lab_paper))
            if len(lab_book) < 2:
                failures.append("书 %d 部（要求 ≥2）" % len(lab_book))
        # 角标落点
        cited = set()
        for group in re.findall(r"［([^］]{1,80})］", body_zone):
            for tok in re.split(r"[,，、\s]+", group):
                if tok:
                    cited.add(tok)
        orphan = [e for e in entries if e not in cited]
        if orphan:
            failures.append("正文无角标落点：" + "、".join("［%s］" % o for o in orphan[:5]))
        # 反向：正文引了、条目里却没有（多半是删条目时漏删角标）
        dangling = [c for c in cited if c not in entries and re.match(r"^(\d+|B\d+|L\d+)$", c)]
        if dangling:
            failures.append("正文角标指向不存在的条目：" + "、".join("［%s］" % d for d in dangling[:5]))
        if lab_other:
            failures.append("非标准编号：" + "、".join("［%s］" % o for o in lab_other[:5]))
    else:
        failures.append("没有 ## 参考文献 段")

    if failures:
        record("FAIL", "文件 %s" % rel, "；".join(failures))
        return False
    return True


def check_links(files):
    pattern = re.compile(r"(0[0-8]-[a-z]+/[^\s`)\]}；;、，,）】》\]]+?\.md)")
    bad, total = [], 0
    for path, text in files:
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        for m in pattern.finditer(text):
            target = m.group(1)
            total += 1
            if not os.path.exists(os.path.join(REFS, target)):
                bad.append("%s → %s" % (rel, target))
    if bad:
        record("FAIL", "交叉引用可达", "%d 条断链" % len(bad))
        for b in bad[:10]:
            print("        · " + b)
    else:
        record("PASS", "交叉引用可达", "%d 条链接全部命中" % total)
    return total


def check_counts(skill_text, n_files):
    problems = []
    declared = {int(x) for x in re.findall(r"(\d+)\s*份", skill_text)}
    if n_files not in declared:
        problems.append("SKILL.md 没有出现「%d 份」，出现的是 %s"
                        % (n_files, sorted(declared) or "无"))
    if not re.search(r"%d\s*个域" % len(DOMAINS), skill_text):
        problems.append("SKILL.md 没写「%d 个域」" % len(DOMAINS))
    if problems:
        record("FAIL", "份数与域数与实物一致", "；".join(problems))
    else:
        record("PASS", "份数与域数与实物一致", "%d 份 / %d 域" % (n_files, len(DOMAINS)))


def check_assets():
    p = os.path.join(ROOT, "assets", "reference-library-160.png")
    if os.path.exists(p):
        record("PASS", "总览图存在", "%.0f KB" % (os.path.getsize(p) / 1024))
    else:
        record("WARN", "总览图存在", "assets/reference-library-160.png 缺失")
    # README 里引用的图片要能打开
    try:
        rtext = read(README)
    except OSError:
        return
    bad = []
    for m in re.finditer(r'src="([^"]+\.(?:png|svg|jpg))"', rtext):
        target = m.group(1)
        if target.startswith(("http://", "https://")):
            continue          # 徽章是远程图，不查本地
        target = target.lstrip("./")
        if not os.path.exists(os.path.join(ROOT, target)):
            bad.append(target)
    if bad:
        record("FAIL", "README 图片可达", "缺：" + "、".join(bad))
    else:
        record("PASS", "README 图片可达", "全部命中")


def check_line_endings(ref_files):
    """同一文件里混用 CRLF / LF 是硬伤：Python 写入忘了 newline="" 就会这样。"""
    targets = top_markdown() + [p for _, _, p in ref_files]
    mixed, crlf, lf = [], 0, 0
    for p in targets:
        t = read(p)
        c = t.count("\r\n")
        l = t.count("\n") - c
        if c and l:
            mixed.append(os.path.relpath(p, ROOT).replace("\\", "/"))
        elif c:
            crlf += 1
        else:
            lf += 1
    if mixed:
        record("FAIL", "换行符不混用", "%d 个文件混用：%s" % (len(mixed), "、".join(mixed[:5])))
    else:
        record("PASS", "换行符不混用", "%d 份 CRLF / %d 份 LF" % (crlf, lf))


def check_copy_lengths(fm):
    """summary 会被展示位截断，description 是每次对话的常驻成本——两头都要看着。"""
    summary = str(fm.get("summary") or "")
    desc = str(fm.get("description") or "")
    problems = []
    if len(summary) < 20:
        problems.append("summary 只有 %d 字，截断后看不出意图" % len(summary))
    if len(desc) < 200:
        problems.append("description 只有 %d 字，触发覆盖面可能不够" % len(desc))
    if problems:
        record("WARN", "文案长度", "；".join(problems))
    else:
        record("PASS", "文案长度", "summary %d 字 / description %d 字" % (len(summary), len(desc)))


def check_readme_counts(n_files):
    """README 里也写着份数与域数，两处都该跟实物对得上。"""
    try:
        rtext = read(README)
    except OSError:
        return
    problems = []
    for m in re.finditer(r"(\d+)\s*份", rtext):
        n = int(m.group(1))
        if n >= 20 and n != n_files:
            problems.append("README 写「%d 份」，实际 %d 份" % (n, n_files))
    for m in re.finditer(r"(\d+)\s*个域", rtext):
        n = int(m.group(1))
        if n != len(DOMAINS):
            problems.append("README 写「%d 个域」，实际 %d 个" % (n, len(DOMAINS)))
    if problems:
        record("FAIL", "README 数字与实物一致", "；".join(problems))
    else:
        record("PASS", "README 数字与实物一致", "%d 份 / %d 域" % (n_files, len(DOMAINS)))


def check_age_claim(fm):
    """年龄口径是踩过坑的地方：以 8–15 为主、覆盖 3–18，三处字段都要在。

    验的是"有没有写"，不验措辞——措辞由人定，机器只防某一处漏改。
    """
    summary = str(fm.get("summary") or "")
    desc = str(fm.get("description") or "")
    en = str(fm.get("description_en") or "")
    problems = []
    if "8" not in summary or "15" not in summary:
        problems.append("summary 没写 8–15 主口径")
    if "8-15" not in en and "8–15" not in en:
        problems.append("description_en 没写 8-15 focus")
    if "3" not in desc or "18" not in desc:
        problems.append("description 没写覆盖 3–18")
    if problems:
        record("FAIL", "年龄口径一致", "；".join(problems))
    else:
        record("PASS", "年龄口径一致", "summary / description / description_en 三处对齐")


# --------------------------------------------------------------------------
# --fix
# --------------------------------------------------------------------------

DELIM = re.compile(r"^\s*\\?-{3,}\s*$")


def locate_frontmatter(lines):
    """定位 frontmatter 边界，容忍分隔符本身已被转义成 \\--- 的情况。

    返回 (起始行号, 结束行号)；找不到返回 (None, None)。
    要求两块之间的内容里出现 `key:`，避免把正文的 `---` 分隔线当成边界。
    """
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not DELIM.match(lines[i]):
        return None, None
    for j in range(i + 1, min(len(lines), i + 80)):
        if DELIM.match(lines[j]) and re.search(r"^[A-Za-z_][\w-]*\s*:", "\n".join(lines[i + 1:j]), re.M):
            return i, j
    return None, None


def fix_pollution(path):
    """把富文本编辑器留下的三种痕迹清掉：\\--- 分隔符、\\- 列表项、行尾空格。

    只动 frontmatter 区；正文一个字不碰（正文里可能是代码块或刻意换行）。
    换行符按原文件还原，避免把 CRLF 静默改成 LF。
    """
    raw = read(path)
    eol = "\r\n" if "\r\n" in raw else "\n"
    lines = norm(raw).split("\n")
    i, j = locate_frontmatter(lines)
    if i is None:
        return False
    changed = False
    for k in range(i, j + 1):
        old = lines[k]
        new = old
        if DELIM.match(new):
            new = "---"
        elif re.match(r"^\s*\\-\s+", new):
            new = re.sub(r"^(\s*)\\-(\s+)", r"\1-\2", new)
        new = new.rstrip()
        if new != old:
            changed = True
            lines[k] = new
    if not changed:
        return False
    rebuilt = "\n".join(lines)
    if eol != "\n":
        rebuilt = rebuilt.replace("\n", eol)
    write(path, rebuilt)
    return True


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(add_help=True, description="亲子沟通军师 skill 自检")
    ap.add_argument("--fix", action="store_true", help="先清掉 frontmatter 的富文本污染痕迹")
    ap.add_argument("--quiet", action="store_true", help="只打结论和失败项")
    args = ap.parse_args()

    verbose = not args.quiet
    print("亲子沟通军师 · skill 自检")
    print("=" * 62)
    print("根目录：%s" % ROOT)
    print()

    if not os.path.exists(SKILL):
        print("找不到 SKILL.md，路径不对：%s" % ROOT)
        return 1

    if args.fix:
        fixed = []
        for p in top_markdown():
            if fix_pollution(p):
                fixed.append(os.path.basename(p))
        for d, n, p in collect_ref_files():
            if fix_pollution(p):
                fixed.append("%s/%s" % (d, n))
        print("--fix：清理了 %d 个文件的 frontmatter 痕迹%s"
              % (len(fixed), ("（" + "、".join(fixed[:6]) + "）") if fixed else ""))
        print()

    skill_text = read(SKILL)
    fm, _ = check_frontmatter(skill_text)

    all_md = [(p, read(p)) for p in top_markdown()]
    ref_files = collect_ref_files()
    for d, n, p in ref_files:
        all_md.append((p, read(p)))

    check_pollution(all_md)
    check_version(fm)
    check_domains()

    bad_files = 0
    for d, n, p in ref_files:
        if not check_reference_file(d, n, p, verbose):
            bad_files += 1
    if bad_files:
        record("FAIL", "references 逐份合规", "%d/%d 份不合规" % (bad_files, len(ref_files)))
    else:
        record("PASS", "references 逐份合规",
               "%d 份：骨架·免责·头部字段·文献·角标 全过" % len(ref_files))

    check_links(all_md)
    check_counts(skill_text, len(ref_files))
    check_assets()
    check_line_endings(ref_files)
    check_copy_lengths(fm)
    check_readme_counts(len(ref_files))
    check_age_claim(fm)

    npass = len([r for r in results if r[0] == "PASS"])
    nfail = len([r for r in results if r[0] == "FAIL"])
    nwarn = len([r for r in results if r[0] == "WARN"])

    if not args.quiet:
        for level, name, detail in results:
            print("[%-4s] %-22s %s" % (level, name, detail))

    print("-" * 62)
    print("references：%d 份 / %d 域" % (len(ref_files), len(DOMAINS)))
    print("结论：通过 %d 项、失败 %d 项、提示 %d 项" % (npass, nfail, nwarn))
    if bad_files:
        print("不合规文件：%d 份（上方逐份列出）" % bad_files)
    print("结果：%s" % ("全部通过" if nfail == 0 else "有 %d 项未通过" % nfail))
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
