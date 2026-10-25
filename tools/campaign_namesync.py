"""战役命名同步：按确定性的锚点键，把同一事物在各批次里的名字统一成图里已有的写法。

背景（2026-10-24 opus2000 战役）：两个子代理并行抽相邻批次时，同一事物在不同批次起了不同名字，
图里留下重复节点（人工发现约 12 组；回放 16 个已提交批次又找到 4 起人工没发现的）。
这里不靠模型判断「是不是同一个东西」，只按规则从名字和观测正文里算出锚点键：
  file  文件/配置/文档：仓库 + 仓库内相对路径（由名字前缀 `仓库:`、括号限定或正文里的绝对路径推出；
        recruitment-intel-service 记为 zhaopin）
  id    纯代码标识符（foo_bar、Foo.bar、foo()），括号里没指明别的仓库，类型类别相同
  wt    工作树：仓库 + 工作树目录名（由正文里的 .claude/worktrees/<名>、已知工作树目录名、其前缀或 hash 后缀推出）
同键不同名 → 自动改成规范名（auto）。下面几类只报告、不自动改（review），留给执行者判断：
  core  同类型、括号前的主体相同而括号限定不同的描述性名字
  pr    同一 PR 号对应了不同名字的 Fix
  head  名字以某个标识符开头（标识符 + 短后缀），而别处有以该标识符为全名的实体
另外两类（2026-10-25 opus2000 战役 021、022 批反馈）：
  server  服务端会自己把它并进图里的某个节点（判据直接调 utils/extracted_ingest._lexically_same，不另抄规则），
          只报告「服务端会合并到 X」：像「feature/course-benefit-freetimes（libtv）」这种只比裸名多一个短括号的写法，
          服务端按包含占比并进裸名节点，以前只被列成 review，看不出它其实会被合并。
  xrepo   本批文件按正文解析到仓库 A，而图里同名的裸节点（无仓库前缀）在已提交记录里只解析到仓库 B：
          服务端按名字精确复用会把 A 的文件挂到 B 的节点上（图里 `RedisCacheKeyConstants.java` 属 throttle，
          sd-server 的同名文件被并了进去）。只报告、建议改成 `A:相对路径`，不自动改：正文里的路径可能是暂存副本或部署副本
          （回放 opus2000：`credvault_dashboard.py` 的正文路径在 fleet-ops/scratchpad 里，自动加前缀就把 credvault 的
          看板拆成了两个节点）。A、B 都得是认得的仓库（~、nas、vps 这类位置不算）。

规范名怎么选（auto 类）：
  1. 已正式提交的批次里有同键实体时，取图里实际挂得最多的节点名（按各战役 log.jsonl 里服务端的复用映射换算），
     只选干净的名字（不是绝对路径、不以任务号开头），写了 `仓库:` 前缀的不往没前缀的写法上改；
     本批名字已经是图里同键的节点名之一就不动；服务端本来就会自己合并的写法不改（列进 server）。
  2. 图里还没有、但本战役更早的待提交批次有同键实体时，取最早那批的写法（并行批次之间先到先得）。
  3. 都没有时，本批内部统一成第一次出现的写法。

提交前的分裂判据（dry-run 判据）：最近一次 dry-run 的 new_entities 里有这个名字（服务端会新建节点），
而同键在图里已有节点 → 正式提交会在图里多出一个同物节点，拒绝。

纯函数在这里；读写战役文件、接进 submit 的在 tools/extract_campaign.py（单模型在 tools/agent_extract.py）。
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from utils.extracted_ingest import _lexically_same

ALIASES = {"recruitment-intel-service": "zhaopin", "recruitment-intel": "zhaopin", "kg-hub-campaign": "kg-hub"}
FILE_TYPES = {"File", "Config", "KnowledgeDoc"}
EXT = re.compile(r"\.(py|sh|js|mjs|cjs|ts|tsx|md|json|ya?ml|sql|toml|plist|conf|txt|sqlite3?|db|env|tsv|csv|html|css|go|rs"
                 r"|ini|cfg|lock|log)$", re.I)
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
QUAL = re.compile(r"\s*[（(]([^（）()]*)[）)]\s*$")
PR = re.compile(r"(?:([A-Za-z0-9_.-]+)#|PR\s*#)(\d+)")
_PATH_END = r"[^\s,;:\"'`)\]）】>]+"
# 前两条边是照抄标题与译文（规则 21、22），改名时原样保留其 fact
VERBATIM_EDGES = 2
GRAPH_HINT_LIMIT = 20


@dataclass
class Layout:
    """路径布局：观测正文里的绝对路径怎么拆成（仓库, 相对路径, 工作树）。

    workai：各仓库所在目录（缺省 ~/work-ai，KG_HUB_WORKAI_ROOT 可改）；home：家目录（KG_HUB_NAMESYNC_HOME 可改）。
    extra_roots：另外的仓库目录（缺省 ~/workspace，公司仓库 sd-server、throttle 等在那里；KG_HUB_NAMESYNC_ROOTS
    用 os.pathsep 分隔可改，给空串就不用），只认带 .git 的子目录为仓库。
    repos：认得的仓库名；known_wts：已知的（仓库, 工作树目录名），磁盘上现存的 + 各战役正文里出现过的；
    repo_dirs：仓库名 → 本机目录（核对正文「Files read」里的相对路径是否真在该仓库，见 Body）。"""
    workai: str
    home: str
    repos: set[str] = field(default_factory=set)
    aliases: dict[str, str] = field(default_factory=lambda: dict(ALIASES))
    known_wts: set[tuple[str, str]] = field(default_factory=set)
    extra_roots: list[str] = field(default_factory=list)
    repo_dirs: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.workai = self.workai.rstrip("/") + "/"
        self.home = self.home.rstrip("/") + "/"
        self.extra_roots = [r.rstrip("/") + "/" for r in self.extra_roots if r]
        self.abs_re = re.compile(rf"({re.escape(self.home.rstrip('/'))}/{_PATH_END}|/volume\d/{_PATH_END}"
                                 rf"|/home/[A-Za-z0-9_-]+/{_PATH_END})")

    @classmethod
    def from_disk(cls, workai: str | None = None, home: str | None = None) -> "Layout":
        home = home or os.environ.get("KG_HUB_NAMESYNC_HOME") or str(Path.home())
        workai = workai or os.environ.get("KG_HUB_WORKAI_ROOT") or str(Path(home) / "work-ai")
        env_roots = os.environ.get("KG_HUB_NAMESYNC_ROOTS")
        extra = env_roots.split(os.pathsep) if env_roots is not None else [str(Path(home) / "workspace")]
        lay = cls(workai=workai, home=home, extra_roots=extra)
        for root, need_git in [(Path(lay.workai), False)] + [(Path(r), True) for r in lay.extra_roots]:
            if not root.is_dir():
                continue
            for d in root.iterdir():
                if d.is_dir() and (not need_git or (d / ".git").exists()):
                    lay.repos.add(d.name)
                    lay.repo_dirs.setdefault(lay.aliases.get(d.name, d.name), str(d))
            for d in root.glob("*/.claude/worktrees/*"):
                if d.is_dir():
                    repo = d.parent.parent.parent.name
                    lay.known_wts.add((lay.aliases.get(repo, repo), d.name))
        return lay

    def repo_of(self, token: str) -> str | None:
        token = token.strip()
        return self.aliases.get(token, token if token in self.repos or token in self.aliases.values() else None)

    def split_abs(self, p: str) -> tuple[str, str, str | None] | None:
        """绝对路径 → (仓库, 仓库内相对路径, 工作树名)；认不出的返回 None。"""
        p = p.rstrip(".")
        root = next((r for r in [self.workai] + self.extra_roots if p.startswith(r)), None)
        if root:
            parts = p[len(root):].split("/")
            repo = self.aliases.get(parts[0], parts[0])
            rest, wt = parts[1:], None
            if rest[:2] == [".claude", "worktrees"] and len(rest) >= 3:
                wt, rest = rest[2], rest[3:]
            return repo, "/".join(rest), wt
        if p.startswith(self.home):
            return "~", p[len(self.home):], None
        if p.startswith("/volume"):
            return "nas", p, None
        if p.startswith("/home/"):
            return "vps", p, None
        return None

    def is_repo(self, name: str) -> bool:
        """认得的仓库（不含 ~、nas、vps 这类位置）。"""
        return name in self.repos or name in self.aliases.values()

    def has_file(self, repo: str, rel: str) -> bool:
        d = self.repo_dirs.get(repo)
        return bool(d) and os.path.isfile(os.path.join(d, rel))

    def clean(self, name: str) -> bool:
        """能当规范名的干净名字：不是绝对路径、不含家目录、不以任务号开头。"""
        return not (name.startswith("/") or self.home.strip("/") in name or re.match(r"^T-\d", name))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("`", "").strip())


def split_name(name: str) -> tuple[str, list[str]]:
    """名字 → (括号前主体, [括号限定…])，只剥末尾的括号组。"""
    core, quals = norm(name), []
    while True:
        m = QUAL.search(core)
        if not m or m.start() == 0:
            break
        quals.insert(0, m.group(1).strip())
        core = core[:m.start()].rstrip()
    return core, quals


PROJECT_LINE = re.compile(r"^Project: *(\S+) *$", re.M)
FILES_LINE = re.compile(r"^Files (?:read|modified|edited|created|written): *(.+)$", re.M)


class Body:
    """一条观测正文里出现的绝对路径与工作树。

    claude-mem 观测末尾的「Project: <仓库>」与「Files read/modified: 相对路径, …」也算，但只在该相对路径**在本机那个仓库里
    真有这个文件**时才记成（该仓库, 相对路径）：Project 是会话的标签，不一定是文件所在的仓库（opus2000 战役里 Project 写
    fleet-ops 的观测读的多是 kg-hub、credvault 的文件，相对路径还常带 `kg-hub/` 这类上级目录）。
    公司仓库（sd-server 等）的观测多半只有这两行、没有绝对路径，不读它们就推不出仓库
    （2026-10-25：sd-server 的 RedisCacheKeyConstants.java 被并进 throttle 的同名节点）。"""

    def __init__(self, text: str, layout: Layout):
        self.paths: list[tuple[str, str]] = []
        self.wts: set[tuple[str, str]] = set()
        for p in layout.abs_re.findall(text or ""):
            r = layout.split_abs(p)
            if not r:
                continue
            repo, rel, wt = r
            if rel:
                self.paths.append((repo, rel))
            if wt:
                self.wts.add((repo, wt))
        m = PROJECT_LINE.search(text or "")
        project = layout.repo_of(m.group(1)) if m else None
        if not project:
            return
        for line in FILES_LINE.findall(text or ""):
            for rel in re.split(r",\s*", line.strip()):
                rel = rel.strip().removeprefix("./")
                if not rel or rel.startswith(("/", "~", "..")) or re.search(r"\s", rel) or not layout.has_file(project, rel):
                    continue
                parts = rel.split("/")
                if parts[:2] == [".claude", "worktrees"] and len(parts) >= 4:
                    self.wts.add((project, parts[2]))
                    rel = "/".join(parts[3:])
                self.paths.append((project, rel))

    def resolve(self, path: str, repo: str | None) -> tuple[str | None, str]:
        """用正文里的绝对路径补全仓库和完整相对路径；唯一命中才采用。"""
        hits = {(r, rel) for r, rel in self.paths
                if (repo is None or r == repo) and (rel == path or rel.endswith("/" + path))}
        if len(hits) == 1:
            return next(iter(hits))
        return repo, path


def _quals_repos(quals: list[str], layout: Layout) -> list[str]:
    return [layout.repo_of(t) for q in quals for t in re.split(r"[，,\s]+", q) if layout.repo_of(t)]


def keys_for(name: str, typ: str | None, body: Body, layout: Layout) -> list[tuple[str, tuple, bool]]:
    """(种类, 键, 是否可自动改) 列表。"""
    out: list[tuple[str, tuple, bool]] = []
    core, quals = split_name(name)
    is_path = typ in FILE_TYPES or "/" in core or bool(EXT.search(core))
    if is_path and not re.search(r"\s", core):
        repo, path = None, core
        m = re.match(r"^([A-Za-z0-9_.-]+):(?!//)(.+)$", core)
        if m and layout.repo_of(m.group(1)):
            repo, path = layout.repo_of(m.group(1)), m.group(2)
        if repo is None:
            qr = _quals_repos(quals, layout)
            repo = qr[0] if qr else None
        path = path[2:] if path.startswith("./") else path
        if path.startswith(layout.home):
            r = layout.split_abs(path)
            if r:
                repo, path = r[0], r[1]
        elif path.startswith("~/"):
            repo, path = "~", path[2:]
        repo, path = body.resolve(path, repo)
        out.append(("file", (repo or "?", path), repo is not None))
        return out
    ident = core.removesuffix("()")
    if typ not in FILE_TYPES and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", ident) and (
            re.search(r"[_.-]", ident) or re.search(r"[a-z][A-Z]", ident)):
        repos = set(_quals_repos(quals, layout))
        if len(repos) <= 1:
            out.append(("id", (ident, next(iter(repos), "")), True))
            return out
    if re.search(r"(工作树|worktree)$", core, re.I):
        low = name.lower()

        def named_in(pool):
            hit = set()
            for r, w in pool:
                wl = w.lower()
                stem, _, tail = wl.rpartition("-")
                if (wl in low or (len(stem) >= 8 and stem in low)
                        or (re.fullmatch(r"[0-9a-f]{6,}", tail)
                            and re.search(rf"(?<![0-9a-f]){tail}(?![0-9a-f])", low))):
                    hit.add((r, w))
            return hit

        other = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]{3,}", name) if not layout.repo_of(t)]
        cand = (named_in(body.wts) or named_in(layout.known_wts)
                or (body.wts if len(body.wts) == 1 and not other else set()))
        if len(cand) == 1:
            out.append(("wt", next(iter(cand)), True))
            return out
    head = IDENT.match(core)
    if head and head.end() < len(core) and core[head.end()] == " " and len(core[head.end():].strip()) <= 4:
        hid = head.group(0)
        if "_" in hid or "." in hid:
            out.append(("head", (hid, ""), False))
    if typ == "Fix":
        qrepos = _quals_repos(quals, layout)
        for m in PR.finditer(name):
            repo = (layout.repo_of(m.group(1)) if m.group(1) else None) or (qrepos[0] if len(set(qrepos)) == 1 else None)
            if repo:
                out.append(("pr", (repo, m.group(2)), False))
    if len(core) >= 4:
        out.append(("core", (tclass(typ), core.lower()), False))
    return out


def tclass(typ: str | None) -> str:
    return "file" if typ in FILE_TYPES else "issue" if typ == "Issue" else "fix" if typ == "Fix" else "thing"


def server_merges(a: str, b: str, typ: str | None = None) -> bool:
    """服务端会不会把抽出名 a 并进图里的 b：直接调服务端的词面判据 utils/extracted_ingest._lexically_same（带实体类型），
    不在这里另抄一份规则（以前只抄了「全路径与短名、只差 ()」两条，包含占比那条没跟上）。
    服务端还要求向量召回到 b、且 b 的类型标签含 a 的类型——类型由调用方查，向量召回这里假定成立（近名一般都召回得到）。"""
    return _lexically_same(norm(a), norm(b), typ)


def same_exact(a: str, b: str) -> bool:
    """服务端第一步的精确匹配：大小写无关、空白归一后相等（不看类型）。"""
    return norm(a).lower() == norm(b).lower()


_REPO_PREFIX = re.compile(r"^[A-Za-z0-9_.-]+:(?!//)")


def name_tail(name: str) -> str:
    """服务端可能合并的候选的粗索引：括号前主体去掉仓库前缀与末尾 ()，取最后一段路径，小写。
    全路径与短名、只差括号限定、只差 () 的写法落在同一个桶里。"""
    core = _REPO_PREFIX.sub("", split_name(norm(name).removesuffix("()"))[0]).removesuffix("()")
    return core.rsplit("/", 1)[-1].lower()


# ---------- 读战役目录 ----------

def load_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def items_of(d) -> list[dict]:
    if isinstance(d, dict):
        return d.get("items") or []
    return d if isinstance(d, list) else []


def results_for(cdir: Path, bid: str) -> list[dict]:
    """结果文件；还没合并时读分片（results/<批次>.part*.json），后面的分片覆盖同 id。"""
    main = load_json(cdir / "results" / f"{bid}.json")
    if main is not None:
        return items_of(main)
    merged: dict = {}
    for part in sorted((cdir / "results").glob(f"{bid}.part*.json")):
        for it in items_of(load_json(part)):
            merged[it.get("id")] = it
    return list(merged.values())


def reuse_map(cdir: Path) -> dict:
    """(批次, 观测) → {抽出名: 图里节点名}，来自服务端正式提交时记下的 reuse_mismatch。"""
    out: dict = defaultdict(dict)
    try:
        lines = (cdir / "log.jsonl").read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        for pair in row.get("reuse_mismatch") or []:
            if isinstance(pair, str) and " → " in pair:
                a, b = pair.split(" → ", 1)
                out[(row.get("batch"), row.get("id"))][a.strip()] = re.sub(r"（同批新建）$", "", b.strip())
    return out


class Record:
    __slots__ = ("campaign", "batch", "item", "name", "typ", "keys", "rank", "graph")

    def __init__(self, campaign, batch, item, name, typ, keys, rank, graph=None):
        self.campaign, self.batch, self.item, self.name, self.typ, self.keys, self.rank = (
            campaign, batch, item, name, typ, keys, rank)
        self.graph = graph  # 已提交时，这个实体在图里实际对应的节点名

    def where(self) -> str:
        return f"{self.campaign}/{self.batch}#{self.item}"


def batch_number(bid: str) -> int:
    return int(re.sub(r"\D", "", bid.rsplit("-", 1)[-1]) or 0)


def records_for(campaign: str, bid: str, items: list[dict], bodies: dict, rank: tuple, layout: Layout,
                graph_of=None) -> list[Record]:
    """一批结果里的实体记录。graph_of(观测 id, 名字) → 图里节点名（只对已提交的批次给）。"""
    out = []
    for it in items:
        body = bodies.get(it.get("id")) or Body("", layout)
        for e in it.get("entities") or []:
            nm, ty = e.get("name"), e.get("type")
            if isinstance(nm, str) and nm:
                out.append(Record(campaign, bid, it.get("id"), nm, ty, keys_for(nm, ty, body, layout), rank,
                                  graph_of(it.get("id"), nm) if graph_of else None))
    return out


def campaign_records(root: Path, target_cid: str | None, layout: Layout) -> tuple[list[Record], dict]:
    """root（…/campaigns）下全部战役的已提交批次 + 目标战役的未提交批次（含还没合并的分片）。
    meta[(战役, 批次)] = {"status", "rank"}；rank 越小越早：已提交的按提交时间，未提交的按批次号。"""
    meta: dict = {}
    staged = []  # 先读全部正文收集已知工作树，再算键
    cdirs = sorted(p for p in root.iterdir() if (p / "campaign.json").exists()) if root.is_dir() else []
    for cdir in cdirs:
        camp = load_json(cdir / "campaign.json") or {}
        rmap = None
        for b in camp.get("batches", []):
            bid, status = b.get("id"), b.get("status")
            if status == "released":
                continue
            submitted = status == "submitted"
            if not submitted and cdir.name != target_cid:
                continue
            if submitted and rmap is None:
                rmap = reuse_map(cdir)
            batch = load_json(cdir / "batches" / f"{bid}.json") or {}
            bodies = {it.get("id"): Body(it.get("episode_body", ""), layout) for it in batch.get("items", [])}
            rank = (0, b.get("submitted_at") or "", bid) if submitted else (1, f"{batch_number(bid):06d}", bid)
            meta[(cdir.name, bid)] = {"status": status, "rank": rank}
            staged.append((cdir, bid, rank, rmap if submitted else None, bodies))
    for *_, bodies in staged:
        for body in bodies.values():
            layout.known_wts.update(body.wts)
    recs: list[Record] = []
    for cdir, bid, rank, rmap, bodies in staged:
        graph_of = (lambda oid, nm, _m=rmap, _b=bid: _m.get((_b, oid), {}).get(nm, nm)) if rmap is not None else None
        recs += records_for(cdir.name, bid, results_for(cdir, bid), bodies, rank, layout, graph_of)
    return recs, meta


# ---------- 判定 ----------

def earlier_index(recs: list[Record], before: tuple) -> dict[tuple, list[Record]]:
    """(种类, 键) → 更早的记录；另有 ("gtail", 名字尾段) → 已提交（有图中节点名）的记录，按图中节点名索引，
    给 server / xrepo 找服务端可能合并过去的节点。"""
    out: dict[tuple, list[Record]] = defaultdict(list)
    for r in recs:
        if r.rank < before:
            for kind, key, _ in r.keys:
                out[(kind, key)].append(r)
            if r.graph:
                out[("gtail", name_tail(r.graph))].append(r)
    return out


def _graph_nodes(recs: list[Record]) -> dict[str, list[Record]]:
    out: dict[str, list[Record]] = defaultdict(list)
    for x in recs:
        if x.graph:
            out[x.graph].append(x)
    return out


def _node_types(xs: list[Record]) -> set:
    """图中节点的类型标签：直接用这个名字写入的记录的类型（建节点的就在其中）；没有就用并进来的记录的类型。"""
    direct = {x.typ for x in xs if x.name == x.graph}
    return direct or {x.typ for x in xs}


def _hung(xs: list[Record]) -> int:
    return len({(y.campaign, y.batch, y.item) for y in xs})


def _merge_into(r: Record, g: str, xs: list[Record]) -> bool:
    """服务端会把 r 并进图中节点 g：名字精确匹配（不看类型），或过 _lexically_same 且 g 的类型标签含 r 的类型。"""
    return same_exact(r.name, g) or (r.typ in _node_types(xs) and server_merges(r.name, g, r.typ))


def server_target(r: Record, nodes: dict[str, list[Record]]) -> tuple[str, list[Record], bool] | None:
    """nodes（图中节点名 → 挂在它上面的记录）里服务端会把 r 并进去的、名字与 r 不同的那个：(节点名, 记录, 是否精确匹配)。
    精确匹配优先（服务端先查它），其次挂得最多的；r 本身就是其中的节点名时服务端直接复用，返回 None。"""
    if r.name in nodes:
        return None
    hits = sorted(((same_exact(r.name, g), _hung(xs), g) for g, xs in nodes.items() if _merge_into(r, g, xs)),
                  key=lambda t: (-t[0], -t[1], t[2]))
    if not hits:
        return None
    exact, _, g = hits[0]
    return g, nodes[g], exact


def _drops_prefix(name: str, to: str) -> bool:
    """name 写了 `仓库:` 前缀（规则 16）而 to 没写：改过去会丢掉仓库限定，裸名还可能是混了几个仓库的泛名节点
    （opus2000 战役：`sd-server:pom.xml` 会被改成图里的 `pom.xml`）。"""
    return bool(_REPO_PREFIX.match(name)) and not _REPO_PREFIX.match(to)


def _file_repos(xs: list[Record]) -> set[str]:
    """记录里能确定的仓库（file 键可自动改的那些）。"""
    return {key[0] for x in xs for kind, key, ok in x.keys if kind == "file" and ok}


def xrepo_check(r: Record, earlier: dict[tuple, list[Record]], layout: Layout) -> dict | None:
    """本批文件确定在仓库 A，而服务端会把它并进的图中裸节点（无仓库前缀）在已提交记录里只解析到仓库 B≠A。"""
    fkey = next((key for kind, key, ok in r.keys if kind == "file" and ok), None)
    if not fkey or not layout.is_repo(fkey[0]):
        return None
    repo, path = fkey
    cands = earlier.get(("gtail", name_tail(r.name)), [])
    nodes = _graph_nodes(cands)
    if r.name in nodes:
        g, exact = r.name, True
    else:
        hit = server_target(r, nodes)
        if not hit:
            return None
        g, _, exact = hit
    xs = nodes[g]
    others = {o for o in _file_repos(xs) if layout.is_repo(o)}
    if _REPO_PREFIX.match(split_name(g)[0]) or len(others) != 1 or repo in _file_repos(xs):
        return None
    return {"graph": g, "repo": repo, "path": path, "other": next(iter(others)), "exact": exact,
            "example": xs[0].where(), "hung": _hung(xs)}


def analyse(mine: list[Record], earlier: dict[tuple, list[Record]], layout: Layout) -> dict:
    """本批每个实体名 → auto（应改成的规范名）/ review（只报告）/ server（服务端会合并到 X，只报告、不改）。"""
    first_in_batch: dict[tuple, Record] = {}
    auto, review, server, seen = [], [], [], set()

    def emit(bucket, r, to, to_type, kind, key, source):
        sig = (r.item, r.name, to, kind)
        if sig not in seen:
            seen.add(sig)
            bucket.append({"item": r.item, "from": r.name, "from_type": r.typ, "to": to, "to_type": to_type,
                           "kind": kind, "key": list(key), "source": source})

    def emit_server(r, hit, key):
        g, xs, exact = hit
        emit(server, r, g, next(iter(_node_types(xs))), "server", key,
             f"服务端会合并到「{g}」（{'名字精确匹配' if exact else '词面判据 _lexically_same 通过'}，"
             f"{_hung(xs)} 条观测挂在它上面，例 {xs[0].where()}）")

    for r in mine:
        n_auto, n_server = len(auto), len(server)
        for kind, key, can_auto in r.keys:
            k = ("id", key) if kind == "head" else (kind, key)
            prior = earlier.get(k, [])
            if kind in ("id", "head"):
                prior = [x for x in prior if tclass(x.typ) == tclass(r.typ)]
            if kind in ("id", "wt") and prior:
                same = [x for x in prior if x.typ == r.typ]
                if not same:  # 同名标识符但类型不同（如容器 Tool 对仓库 Project）：不自动统一，交人判断
                    canon = min(prior, key=lambda x: x.rank)
                    if canon.name != r.name:
                        emit(review, r, canon.name, canon.typ, kind + "-type", key, canon.where())
                    continue
                prior = same
            nodes = _graph_nodes(prior)
            if not (can_auto and kind != "head"):
                canon = min(prior, key=lambda x: x.rank) if prior else None
                if canon and canon.name != r.name:
                    hit = server_target(r, nodes)
                    if hit:
                        emit_server(r, hit, key)   # 服务端会自己并进同键的图中节点：不用人判断
                    else:
                        emit(review, r, canon.name, canon.typ, kind, key, canon.where())
                continue
            if nodes:
                if r.name in nodes or r.name in {x.name for xs in nodes.values() for x in xs}:
                    continue  # 已是图中节点名，或同一写法此前已被服务端并进某节点
                hit = server_target(r, nodes)
                if hit:
                    emit_server(r, hit, key)   # 服务端会照规则自己并过去，不改
                    continue
                ranked = sorted(nodes.items(), key=lambda kv: (-_hung(kv[1]), min(y.rank for y in kv[1])))
                best = next(((n, xs) for n, xs in ranked if layout.clean(n) and not _drops_prefix(r.name, n)), None)
                if best is None:
                    continue  # 图里同键只有脏名，或只有去掉了仓库前缀的写法：不往那上面改
                n, xs = best
                emit(auto, r, n, xs[0].typ, kind, key,
                     f"图中节点（{_hung(xs)} 条观测挂在它上面，例 {xs[0].where()}）")
                continue
            canon = min(prior, key=lambda x: x.rank) if prior else first_in_batch.setdefault(k, r)
            if canon is not r and canon.name != r.name:
                emit(auto, r, canon.name, canon.typ, kind, key,
                     canon.where() + ("（待提交批次）" if prior else "（本批更早的写法）"))
        if len(auto) > n_auto or len(server) > n_server:
            continue
        x = xrepo_check(r, earlier, layout)
        if x:
            emit(review, r, f"{x['repo']}:{x['path']}", r.typ, "xrepo", (x["repo"], x["path"]),
                 f"同名不同仓库：图中裸节点「{x['graph']}」在已提交记录里只属于仓库 {x['other']}（{x['hung']} 条观测，例 {x['example']}），"
                 f"本条在 {x['repo']}，服务端会{'按名字精确' if x['exact'] else '按词面'}复用、把两个仓库的文件并成一个节点；"
                 f"确是 {x['repo']} 的文件就加 `{x['repo']}:` 前缀")
            continue
        # 锚点键不同、却会被服务端并进去的（如只比裸名多一个短括号）：只报告
        hit = server_target(r, _graph_nodes(earlier.get(("gtail", name_tail(r.name)), [])))
        if hit:
            emit_server(r, hit, next((key for _, key, _ in r.keys), ()))
    renames = {(a["item"], a["from"]) for a in auto}
    return {"auto": auto, "review": review, "server": server, "renames": len(renames)}


def dryrun_splits(mine: list[Record], earlier: dict[tuple, list[Record]],
                  new_entities: dict[str, set[str]]) -> list[dict]:
    """会新建的名字（dry-run new_entities），而同键在图里已有别的名字的节点 → 正式提交会分裂。"""
    rows, seen = [], set()
    for r in mine:
        if r.name not in new_entities.get(str(r.item), set()):
            continue
        for kind, key, can_auto in r.keys:
            if not can_auto:
                continue
            graph = {x.graph for x in earlier.get((kind, key), [])
                     if x.graph and (kind not in ("id", "wt") or x.typ == r.typ) and not _drops_prefix(r.name, x.graph)}
            if graph and r.name not in graph and (r.name, kind) not in seen:
                seen.add((r.name, kind))
                rows.append({"item": r.item, "name": r.name, "kind": kind, "key": list(key),
                             "graph_names": sorted(graph)[:5]})
    return rows


def applied_rows(auto: list[dict], skipped: list[dict]) -> list[dict]:
    """apply_renames 实际写回的改名：同一条观测同一旧名只认第一条 auto（与 apply_renames 一致），
    去掉因会成自环而没改的那些名字。返回 [{item, from, to, kind, source}]。"""
    dropped = {(s["item"], n) for s in skipped for n in s["edge"]}
    out, done = [], set()
    for a in auto:
        sig = (a["item"], a["from"])
        if sig in done or sig in dropped:
            continue
        done.add(sig)
        out.append({k: a.get(k) for k in ("item", "from", "to", "kind", "source")})
    return out


def apply_renames(data, auto: list[dict]) -> list[dict]:
    """就地改写结果：实体名、边两端，以及第 3 条边起 fact 里整词出现的旧名（前两条照抄标题与译文不动）。
    改名后会变成自环的边不改那一对名字，记进返回的 skipped。"""
    ren: dict = defaultdict(dict)
    for a in auto:
        ren[a["item"]].setdefault(a["from"], a["to"])
    skipped = []
    for it in items_of(data):
        m = ren.get(it.get("id"))
        if not m:
            continue
        for e in it.get("edges") or []:
            s, t = m.get(e.get("source"), e.get("source")), m.get(e.get("target"), e.get("target"))
            if s == t and e.get("source") != e.get("target"):
                skipped.append({"item": it.get("id"), "edge": [e.get("source"), e.get("target")], "why": "改名后成自环"})
                m = {k: v for k, v in m.items() if k not in (e.get("source"), e.get("target"))}
        ents, kept = [], set()
        for e in it.get("entities") or []:
            e = dict(e)
            e["name"] = m.get(e["name"], e["name"])
            if e["name"] in kept:
                continue
            kept.add(e["name"])
            ents.append(e)
        it["entities"] = ents
        edges, eseen = [], set()
        for idx, e in enumerate(it.get("edges") or []):
            e = dict(e)
            e["source"], e["target"] = m.get(e["source"], e["source"]), m.get(e["target"], e["target"])
            if idx >= VERBATIM_EDGES and isinstance(e.get("fact"), str):
                for old, new in sorted(m.items(), key=lambda kv: -len(kv[0])):
                    if len(old) >= 4 and new not in e["fact"]:
                        e["fact"] = re.sub(rf"(?<![\w./-]){re.escape(old)}(?![\w./-])", lambda _m, _n=new: _n, e["fact"])
            sig = (e["source"], e.get("name"), e["target"], e.get("fact"))
            if sig not in eseen:
                eseen.add(sig)
                edges.append(e)
        it["edges"] = edges
    return skipped


def head_idents(mine: list[Record]) -> list[str]:
    """「标识符 + 短后缀」名里的标识符（去重保序），给 --graph 问图里有没有同名短节点。"""
    out: list[str] = []
    for r in mine:
        for kind, key, _ in r.keys:
            if kind == "head" and key[0] not in out:
                out.append(key[0])
    return out


def graph_hints(mine: list[Record], lookup, layout: Layout | None = None) -> list[dict]:
    """lookup(名字, limit=1) → 图里同名节点的 {"matched_node", "labels", "neighbors"}（只读，每类至多 GRAPH_HINT_LIMIT 次）。
    1. 「标识符 + 短后缀」名：图里有以该标识符为全名的短节点。
    2. 给了 layout 时，本批确定仓库 A 的裸文件名（无仓库前缀）：图里同名节点的邻居（名字与 fact）提到别的仓库 →
       服务端会按名字精确复用、把 A 的文件挂到那个节点上（xrepo 的图侧补充：不在任何战役里的旧图节点只能这样看）。"""
    out = []
    for ident in head_idents(mine)[:GRAPH_HINT_LIMIT]:
        try:
            j = lookup(ident) or {}
        except Exception as exc:  # noqa: BLE001
            out.append({"ident": ident, "error": str(exc)[:120]})
            continue
        if j.get("matched_node") == ident:
            users = sorted({r.name for r in mine for k, key, _ in r.keys if k == "head" and key[0] == ident})
            out.append({"ident": ident, "graph_has_exact": True, "labels": j.get("labels"), "names_in_batch": users})
    if layout is None:
        return out
    files: dict[str, tuple[str, str]] = {}
    for r in mine:
        fkey = next((key for kind, key, ok in r.keys if kind == "file" and ok), None)
        if fkey and fkey[0] in layout.repos | set(layout.aliases.values()) and not _REPO_PREFIX.match(r.name):
            files.setdefault(r.name, fkey)
    repos = sorted(r for r in layout.repos | set(layout.aliases.values()) if len(r) >= 5)
    for name, (repo, path) in list(files.items())[:GRAPH_HINT_LIMIT]:
        try:
            j = lookup(name, limit=GRAPH_HINT_LIMIT) or {}
        except Exception as exc:  # noqa: BLE001
            out.append({"name": name, "error": str(exc)[:120]})
            continue
        if not same_exact(j.get("matched_node") or "", name):
            continue
        text = "\n".join(f"{n.get('name') or ''} {n.get('fact') or ''}" for n in j.get("neighbors") or [])
        seen = {layout.aliases.get(r, r) for r in repos if re.search(rf"(?<![\w-]){re.escape(r)}(?![\w-])", text)}
        if seen - {repo}:
            out.append({"name": name, "xrepo": True, "repo": repo, "graph_node": j.get("matched_node"),
                        "neighbors_mention": sorted(seen), "suggest": f"{repo}:{path}"})
    return out


def batch_report(root: Path, cid: str, bid: str, layout: Layout,
                 new_entities: dict[str, set[str]] | None = None) -> tuple[dict, list[Record]]:
    """战役里一批的命名同步报告：auto / review / 分裂（给了 dry-run new_entities 才判）。"""
    recs, meta = campaign_records(root, cid, layout)
    me = meta.get((cid, bid))
    if not me:
        raise SystemExit(f"战役 {cid} 里找不到可同步的批次 {bid}（已回收或不存在）")
    mine = [r for r in recs if r.campaign == cid and r.batch == bid]
    earlier = earlier_index([r for r in recs if not (r.campaign == cid and r.batch == bid)], me["rank"])
    rep = {"batch": bid, **analyse(mine, earlier, layout)}
    rep["splits"] = dryrun_splits(mine, earlier, new_entities) if new_entities is not None else []
    return rep, mine


def replay(root: Path, cid: str, layout: Layout, new_entities_of) -> list[dict]:
    """回放本战役各批（统计用）：每批会被抓到的 auto / review / 分裂。new_entities_of(批次) → dry-run new_entities 或 None。"""
    recs, meta = campaign_records(root, cid, layout)
    rows = []
    for (c, bid), m in sorted(meta.items(), key=lambda kv: kv[1]["rank"]):
        if c != cid:
            continue
        mine = [r for r in recs if r.campaign == cid and r.batch == bid]
        earlier = earlier_index([r for r in recs if not (r.campaign == cid and r.batch == bid)], m["rank"])
        rep = analyse(mine, earlier, layout)
        ne = new_entities_of(bid)
        splits = dryrun_splits(mine, earlier, ne) if ne is not None else []
        rows.append({"batch": bid, "status": m["status"], "auto": len(rep["auto"]), "review": len(rep["review"]),
                     "server": len(rep["server"]), "splits": len(splits), "dryrun": ne is not None,
                     "auto_rows": rep["auto"], "review_rows": rep["review"], "server_rows": rep["server"],
                     "split_rows": splits})
    return rows
