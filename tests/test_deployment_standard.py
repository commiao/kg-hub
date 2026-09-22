"""发布准则（docs/deployment-standard.md）里可机检的那几条。

光写文档会漂：半年后没人记得为什么不能用 `:latest`，然后就有人加回去，回滚又变
成不可能。2026-09-08 kg-hub 的通用部署被禁用（T-0046），表面理由是"Compose 会删
掉重命名的旧容器备份"，但根子是 compose 把五个服务都指向 `kg-hub-server:latest`
——可变标签一 build 就把旧镜像覆盖成悬空层，"回滚"根本没有对象，才不得不去抢救
旧**容器**。这套测试钉住的就是别再回到那个状态。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
RELEASE = ROOT / "deploy" / "nas" / "release.sh"
BLOCKED = ROOT / "deploy" / "nas" / "redeploy.sh"
STANDARD = ROOT / "docs" / "deployment-standard.md"

# 第三方镜像不归本准则管，那是上游的事。
THIRD_PARTY = ("falkordb/",)


class ImageTagTests(unittest.TestCase):
    """准则二、三：自建镜像按 commit 打不可变标签，compose 硬引用。"""

    def setUp(self):
        self.compose = COMPOSE.read_text("utf-8")

    def own_images(self):
        for line in self.compose.splitlines():
            match = re.match(r"\s*image:\s*(\S+)", line)
            if match and not match.group(1).startswith(THIRD_PARTY):
                yield match.group(1)

    def test_no_self_built_image_uses_latest(self):
        offenders = [i for i in self.own_images() if i.endswith(":latest")]
        self.assertEqual(offenders, [], "自建镜像不许用 :latest —— 旧版本会被覆盖，回滚就没有对象了")

    def test_tag_is_a_hard_reference_not_a_soft_default(self):
        # `:-latest` 这种软默认等于没改：少了变量就悄悄发一个来路不明的镜像。
        for image in self.own_images():
            with self.subTest(image=image):
                self.assertIn("${", image, "标签必须来自变量，不能写死")
                self.assertIn(":?", image, "必须是硬引用 ${VAR:?...}，不许 ${VAR:-default}")
                self.assertNotIn(":-", image)

    def test_every_service_sharing_the_image_uses_the_same_variable(self):
        variables = {re.search(r"\$\{([A-Z_]+)", i).group(1) for i in self.own_images()}
        self.assertEqual(variables, {"KG_HUB_IMAGE_TAG"},
                         "同一个镜像必须由同一个变量控制，否则切换会切一半")


class ReleaseScriptTests(unittest.TestCase):
    """准则一、四、五：发 commit 不发工作区；回滚靠标签；项目名写死。"""

    def setUp(self):
        if not RELEASE.exists():
            self.fail("发布脚本不见了：deploy/nas/release.sh")
        self.source = RELEASE.read_text("utf-8")
        # 注释里会引用旧写法作为反面教材，检查实际代码时必须先把注释去掉，
        # 否则测试会因为「文档写得详细」而误报。
        self.code = "\n".join(line for line in self.source.splitlines()
                              if not line.lstrip().startswith("#"))

    def test_source_comes_from_git_not_the_working_tree(self):
        self.assertIn("git archive", self.code)
        # 旧脚本 `tar -cf - -C "$REPO"` 打的是工作区：本地脏文件会被发上线。
        self.assertNotRegex(self.code, r"tar -c[^|]*\$REPO")

    def test_release_refuses_a_commit_that_is_not_on_the_remote(self):
        # 线上跑的东西必须在别人的仓库里也找得到，否则无从对账、无法复现。
        self.assertIn("merge-base --is-ancestor", self.code)
        self.assertIn("origin/main", self.code)

    def test_the_env_gap_is_closed_before_the_slow_build(self):
        # 硬引用会影响别人：变量写进 .env 之前谁跑 compose 谁失败。补写必须排在
        # 构建之前，否则窗口有几分钟长。
        seed = self.code.index("grep -q '^KG_HUB_IMAGE_TAG='")
        build = self.code.index("build -t kg-hub-server:")
        self.assertLess(seed, build)

    def test_compose_is_proven_to_parse_before_producers_stop(self):
        """停生产者之前，整套 compose 必须先证明解析得通。

        一旦 refinery/ingester 停了，后面每一步 —— 起候选容器、回滚 —— 都要 compose
        能解析。解析不通的话，生产者已经停了，而**任何一条恢复路径都走不了**。
        那正是「切了之后回不来」的形态（准则 6）。

        不是假设：compose 里用的是 `${...:?}` 硬引用（铁律三），这意味着
        **.env 少一行 = compose 整体不可用**，而 .env 在发布过程中会被改写。
        上面那几条 ensure_* 各自只覆盖它们关心的那一个变量。

        这一条 2026-09-18 就该做，挂了两天没人接 —— 所以它现在有测试。
        """
        self.assertIn("config -q", self.code, "没有 compose 解析预检")
        # 量的必须是**调用**的位置，不是函数定义的位置 —— 定义总在文件前面，
        # 拿它比较的话，把调用挪到停机之后也测不出来（第一版就是这么漏的）。
        lines = self.code.splitlines()
        calls = [i for i, l in enumerate(lines) if l.strip() == "ensure_compose_parses"]
        self.assertEqual(len(calls), 1, "预检没有被调用，或被调用了多次")
        stops = [i for i, l in enumerate(lines) if "stop -t 30 refinery ingester" in l]
        self.assertTrue(stops, "找不到停生产者那一步，锚点失效了")
        self.assertLess(calls[0], stops[0], "预检排在停生产者之后，等于没检")

    def test_the_preflight_uses_the_same_files_and_project_as_the_real_run(self):
        """校验的必须是**将要执行的那一套**，不是另一套。

        文件少传一个、项目名写错，解析的就是别的东西 —— 准则 28：
        比较的两端要取自同一来源。
        """
        # 挑**真正执行**的那一行，不是 dry-run 的提示行 —— 后者里没有参数，
        # 拿它来断言等于什么都没验。
        line = next(l for l in self.code.splitlines()
                    if "config -q" in l and "compose" in l and "dry-run" not in l)
        for token in ("$COMPOSE_BASE", "$COMPOSE_GATEWAY_OVERRIDE", "$PROJECT"):
            self.assertIn(token, line, f"预检没带上 {token}")

    def test_rollback_reuses_the_old_image_and_verifies_it_exists(self):
        self.assertIn("--rollback", self.source)  # 用法说明里也要有
        self.assertIn("image inspect kg-hub-server:", self.code)
        # 不许"回滚"时重新构建：那样拿到的不是当初跑的那个东西。
        rollback = self.source.split("回滚不重建", 1)
        self.assertEqual(len(rollback), 2, "回滚路径必须显式不重建")

    def test_health_check_runs_after_the_swap_and_auto_reverts(self):
        swap = self.code.index("up -d --no-deps --no-build")
        check = self.code.index("验收：镜像 ID 比对")
        self.assertLess(swap, check, "验收必须在切换之后，否则验的是旧的")
        self.assertIn("自动回到", self.code)

    def test_project_name_is_pinned_in_the_script(self):
        # 多 actor 用不同 -p 会各建一套容器抢同一个端口。
        self.assertIn('KG_HUB_COMPOSE_PROJECT:-kg-hub', self.code)
        self.assertIn("-p $PROJECT", self.code)

    def test_release_keeps_model_callers_on_the_private_gateway_network(self):
        self.assertIn("deploy/model-gateway-network.override.yml", self.code)
        self.assertIn("ensure_model_gateway_private_network", self.code)

    def _gateway_route_gate(self) -> str:
        """只取那道闸的函数体，且已去掉注释 —— 注释里引着旧写法当反面教材。"""
        lines = self.code.splitlines()
        start = next(i for i, l in enumerate(lines)
                     if l.startswith("ensure_compose_gateway_route()"))
        end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
        return "\n".join(lines[start:end + 1])

    def test_gateway_route_gate_asks_compose_not_the_env_file(self):
        """判据取生效值。2026-09-22：原写法查 .env，而值住在 override 的默认里。

        同一个仓库的 check_env_drift.py 判「与 git 默认相同的冗余覆盖，建议从
        .env 删掉」；有人照做之后，这道闸让 kg-hub 十七小时发不了版而没人知道。
        两个检查要求正好相反，根子是这道闸问错了对象（准则 27）。
        """
        body = self._gateway_route_gate()
        # 钉的是「真的发出了那条命令」。只查 "compose" 会被函数名
        # ensure_compose_gateway_route 满足 —— 第一版就是这么写的，变异验证
        # 时才发现它不承重（同一形状今天在别处栽过四次）。
        self.assertIn("$DK compose", body, "闸没有真的调用 compose")
        self.assertIn("-p $PROJECT config", body, "调了 compose 但没要 config 的输出")
        self.assertNotIn("sed -n 's/^ANTHROPIC_BASE_URL=//p' .env", body,
                         "又回去读 .env 那一行了")

    def test_gateway_route_gate_treats_absent_as_normal_but_duplicate_as_error(self):
        """零次正常、多次报错 —— 这两半缺一不可。

        只放宽不拦重复，.env 里写两遍不同的值就会静悄悄取其一；
        只拦重复不放宽，就是回到把 kg-hub 锁死的那个写法。
        """
        body = self._gateway_route_gate()
        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
            self.assertRegex(
                body, rf"grep -c '\^{key}=' \.env[^\n]*\)? -le 1",
                f"{key} 的出现次数不是按「至多一次」判的")
            self.assertNotRegex(
                body, rf"{key}=' \.env \|\| true\)\\?\"? = 1",
                f"{key} 仍然要求恰好出现一次")


class GuardrailTests(unittest.TestCase):
    def test_the_unsafe_script_stays_disabled(self):
        # T-0046 明确写了不得通过删除 exit 或临时开关恢复使用。新路径是另起一条，
        # 不是把旧的解禁。
        if not BLOCKED.exists():
            self.skipTest("旧脚本已删除")
        source = BLOCKED.read_text("utf-8")
        head = source.split("\n", 40)
        self.assertIn("DEPLOY_BLOCKED", "\n".join(head[:40]))

    def test_the_standard_is_written_down(self):
        self.assertTrue(STANDARD.exists(), "准则必须在仓库里，不能只在某次对话里")
        text = STANDARD.read_text("utf-8")
        for rule in (":latest", "git archive", "--rollback", "项目名"):
            self.assertIn(rule, text)



class ConcurrencyAndDrainTests(unittest.TestCase):
    """从 credvault 借来的三条：部署锁、排空、换完校验镜像 ID。

    credvault 的 cutover 有约 60 个函数，这里只借这三条。其余（人工确认口令、
    配置快照事务、精确容器身份记录）是为「管钱、且必须恢复到同一个容器实例」设计
    的；kg-hub 的容器不持有状态，套过来只是负担。这套测试钉住借的这三条别被删掉。
    """

    def setUp(self):
        self.raw = RELEASE.read_text("utf-8")
        self.code = "\n".join(line for line in self.raw.splitlines()
                               if not line.lstrip().startswith("#"))

    def test_a_deploy_lock_prevents_two_actors_releasing_at_once(self):
        # 这个工作区是多 actor 的：两个发布同时跑会互相覆盖 .env、抢同一批容器。
        self.assertIn("acquire_lock", self.code)
        self.assertIn("mkdir '$LOCK'", self.code)   # mkdir 在同一文件系统上是原子的
        self.assertIn("trap ", self.code)           # 退出时释放
        self.assertIn("-mmin +40", self.code)       # 崩溃留下的死锁能被抢占

    def test_release_drains_in_flight_extractions_before_swapping(self):
        # 直接换容器会掐断在飞的流式抽取，而那会在网关留下永不过期的 unknown
        # 记录 —— 发布本身就制造了挡住下次发布的东西。
        drain = self.code.index("active_extractions")
        # `rollback_to_previous_image` also contains compose up, but it is a
        # failure helper.  The candidate cutover is the up after step [5/6].
        cutover = self.code.index('say "[5/6]')
        swap = self.code.index("up -d --no-deps --no-build", cutover)
        self.assertLess(drain, swap, "排空必须在换容器之前")
        self.assertIn("stop -t 30 refinery ingester", self.code)

    def test_drain_distinguishes_a_missing_field_from_zero(self):
        # 字段缺失 = 线上还是旧版本（只能盲等）；值为 0 = 真的排空了。用 sed 抠
        # 字符串分不清这两者，会把「旧版本」当成「已排空」直接换掉。
        self.assertIn("json.load", self.code)
        self.assertIn("盲等", self.raw)

    def test_acceptance_proves_the_new_image_is_actually_running(self):
        # curl /health 只证明「有个东西在听」。compose 完全可能压根没重建容器。
        self.assertIn("image inspect -f '{{.Id}}'", self.code)
        self.assertIn("inspect -f '{{.Image}}'", self.code)


class ServerDrainSignalTests(unittest.TestCase):
    """服务端要能说出「现在有几条抽取在飞」，否则发布只能盲等。"""

    def setUp(self):
        self.server = (ROOT / "kg_hub_server.py").read_text("utf-8")

    def test_health_exposes_the_in_flight_count(self):
        self.assertIn('"active_extractions": active_extractions()', self.server)

    def test_the_counter_is_incremented_outside_the_never_raise_body(self):
        # do_extract 是 never-raise 契约、出口很多；加减写在函数体里迟早漏一条，
        # 计数只增不减，发布就永远等不到归零。所以套薄壳 + finally。
        shim = self.server.split("async def do_extract(", 1)[1][:700]
        self.assertIn("_extraction_started()", shim)
        self.assertIn("finally:", shim)
        self.assertIn("_extraction_finished()", shim)

    def test_the_counter_cannot_go_negative(self):
        self.assertIn("max(0, _active_extractions - 1)", self.server)



class ScopeCoverageTests(unittest.TestCase):
    """准则要覆盖全部落地形态，且对没做的部分保持诚实。

    一份说谎的准则比没有准则更糟：别人会照着它假设「已经统一了」，然后在没收口的
    地方踩坑。所以「还没收口」那张表必须一直如实列着。
    """

    def setUp(self):
        self.text = STANDARD.read_text("utf-8")

    def test_covers_every_machine_and_both_forms(self):
        for topic in ("NAS", "Mac", "Windows", "Docker", "本机"):
            self.assertIn(topic, self.text, f"准则没覆盖 {topic}")

    def test_each_form_says_where_things_land_and_how_to_roll_back(self):
        for section in ("部署位置", "部署流程", "git 管理", "落地位置速查"):
            self.assertIn(section, self.text, f"缺少「{section}」")

    def test_native_services_are_forbidden_from_running_the_working_tree(self):
        # Mac 上 11 个 launchd 服务当前全部直接跑工作区：改一个文件就是上线，
        # 没提交的半成品也会上线。这条禁令是 B 章存在的全部理由。
        self.assertIn("常驻服务不许指向 git 工作区", self.text)
        self.assertIn("current", self.text)

    def test_unfinished_work_is_still_listed_as_unfinished(self):
        tail = self.text.split("还没收口", 1)
        self.assertEqual(len(tail), 2, "必须保留「还没收口」一节")
        # 这三处确实还没改；哪天真改完了，是删表项，不是删这条测试。
        for pending in ("task-hub", "report-portal", "launchd"):
            self.assertIn(pending, tail[1], f"{pending} 的现状不许从表里消失")

    def test_credvault_is_explicitly_carved_out(self):
        # 它必须恢复到同一个容器实例，套用本准则反而是错的。
        self.assertIn("credvault", self.text)
        self.assertIn("不套用本准则", self.text)



class DrainFailureTests(unittest.TestCase):
    """排空失败时的处置：宁可不发，也不能掐断；而且生产者必须起回来。"""

    def setUp(self):
        self.raw = RELEASE.read_text("utf-8")
        self.code = "\n".join(line for line in self.raw.splitlines()
                               if not line.lstrip().startswith("#"))

    def test_a_failed_drain_aborts_instead_of_cutting_requests(self):
        # 硬换会掐断在飞的流式抽取，在网关留下永不过期的记录——那正是这一步要
        # 避免的东西。此刻容器还没换，中止是干净的。
        tail = self.code.split('[ "$drained" != 1 ]', 1)
        self.assertEqual(len(tail), 2, "必须有排空失败分支")
        branch = tail[1][:600]
        self.assertIn("die ", branch)
        self.assertIn("restore_producers", branch)

    def test_drain_failure_has_no_force_swap_escape_hatch(self):
        # 即使操作者环境里设了同名变量，发布器也必须中止，不能掐断在飞请求。
        self.assertNotIn("KG_HUB_FORCE_SWAP", self.code)

    def test_producers_are_always_restarted_if_the_release_stops_early(self):
        # 生产者停下之后任何一步失败而没人管，整条采集就静悄悄停了——比发布
        # 失败本身严重得多。
        self.assertIn("restore_producers", self.code)
        self.assertIn("start refinery ingester", self.code)
        # trap 交给统一出口；该出口同时会恢复受控的 .env 事务。
        self.assertIn("trap 'release_exit'", self.code)
        exit_handler = self.code.split("release_exit()", 1)[1][:500]
        self.assertIn("restore_producers", exit_handler)

    def test_restore_producers_is_defined_before_the_trap_installs_it(self):
        # trap 在拿锁时就装好，而真正的实现在排空那一步才出现：中间任何一次 die
        # 都会调到它，所以必须先有一个占位定义。
        placeholder = self.code.index("restore_producers() { :; }")
        trap_at = self.code.index("trap 'release_exit")
        self.assertLess(placeholder, trap_at)

    def test_the_drain_waits_for_in_flight_not_for_the_backlog(self):
        # 积压有几千条、要跑几个月；等积压等于永远发不了。这条必须写死在文档里，
        # 免得以后有人「顺手」把等待条件改成积压清零。
        self.assertIn("等的是在飞，不是积压", self.raw)
        standard = STANDARD.read_text("utf-8")
        self.assertIn("active_extractions", standard)


if __name__ == "__main__":
    unittest.main()
