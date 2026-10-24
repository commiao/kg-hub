# 独立语义回归与 admission-v2

新版准入增加固定关键案例门。随机盲评零错误、格式或检索指标通过，都不能覆盖固定旧错案例的失败。固定案例单独报告，不进入随机盲评的样本分母。`agent-v5`、完整抽取提示、原质量版本和原指标阈值不因此改版。

历史 `pass` 保持其原始含义和记录。新开战役需要 `admission-v2` 和当前语义套件的有效凭据；旧记录会显示“需补新版语义评审，不必重抽原始输出”。已开战役继续使用冻结规则，claim/submit 不补查新开局合同。

## 本机固定题库登记

语料、结果、评审全文和策略文件保存在本机私有目录。仓库不自带默认生产题库，也不会用公开合成题库代替真实旧错题库。操作者应把已确认的关键历史错误都纳入固定集合，之后每次评审覆盖全部集合；不能逐模型临时挑选容易通过的案例。

下面是完全合成的 spec 示例。实际操作须把 `obs_id`、案例编号、选择理由和评审要求替换为本机已审历史案例。`scope=synthetic-test` 的题库可以验证流程，但不能放行正式战役。生产固定题库须由操作者明确选择 `private-production`。

```json
{
  "suite_id": "ownership-condition-regressions",
  "revision": 1,
  "scope": "synthetic-test",
  "selection_rationale": "固定复查既往责任主体与前置条件错误；所有关键案例必须纳入。",
  "cases": [{
    "case_id": "cedar-approval-after-maple-verification",
    "obs_id": 7,
    "critical": true,
    "rationale": "既往输出把核验方误写为批准方，并遗漏先核验再批准的条件。",
    "criteria": [{
      "criterion_id": "responsible-party",
      "requirement": "逐项核对实际关系的责任主体、对象与来源归属，不把核验责任改写为批准责任。"
    }, {
      "criterion_id": "prior-condition",
      "requirement": "判断输出是否保留原文的必要前置条件，不能把有条件成立改写为无条件成立。"
    }]
  }]
}
```

登记会自动绑定冻结 bench 的 manifest、完整原文和 lookahead 摘要。`private-production` 的每个 case 还必须填写 `known_failure_run`，值为本机原已知错运行目录；登记只读并验证其完整 native 封存（合法历史 v1 亦可），保存原运行和错误输出的摘要、原模型，要求原运行的观测正文与 lookahead 和当前冻结 bench 完全对应。随意路径或手工登记的旧结果不能替代原生证据，错误输出差异也不会用来自动判断新输出的语义正确性。公开合成 spec 可省略此字段。空题库、空标准或非关键案例不能登记。`activate` 的姓名和摘要确认是操作者声明，不是机器证明真人授权，也不代表语义评审通过。

```sh
extract regression create --bench /private/bench --spec /private/semantic-spec.json --out /private/semantic-suite.json
extract regression activate --suite /private/semantic-suite.json --reviewer '实际操作者' --confirm-suite-sha '<create输出的suite_sha256>'
extract regression sheet --bench /private/bench --run /private/existing-native-run --out /private/semantic-review.json
```

策略注册表位于 `$KG_HUB_BENCH_ROOT/semantic-policy.json`，缺省为 `~/.kg-hub/bench/semantic-policy.json`。同一 bench 的固定集合由当前策略选定，admit 不接受临时更换的任意 suite 参数。更换集合或修订标准需要新 revision、显式重新激活和补审；不会回改旧成绩。

## 独立评审与完整性校验

sheet 包含实际完整原文、lookahead、实际实体和关系、完整运行上下文摘要。所有判断初始为 `pending`，没有预填 `ok/true`。独立评审者须填写：

- `reviewed_at`：带时区的实际评审时间；重新运行 check 不会刷新这个时间。
- `reviewer.name`、`method=human|model`、`identity_evidence=self-declared`、`independence_declaration`：如实声明身份、方式和独立性。参评模型不能自评。当前命令不提供官方模型包装器，不接受伪造 `provider-wrapper` 等级。
- 每项标准的 `verdict=pass|fail`、准确且充分的 `source_quote`、指向实际结果的 `claim_pointer`（如 `/edges/2/fact`）、对应 `claim_quote`、责任主体判断与解释。不能只填布尔值。

校验器确认引文存在、输出锚点存在、案例与标准完整、材料与原运行相同。它不依据关键词判断语义真值；评审者仍须阅读原文并承担判断。姓名声明和本地哈希都不是身份签名或供应商认证。

```sh
extract regression check --bench /private/bench --run /private/existing-native-run --review /private/semantic-review.json --out /private/semantic-receipt.json
extract bench admit --bench /private/bench --run /private/existing-native-run --sheet /private/completed-random-blind.json --semantic-review /private/semantic-receipt.json
```

以上命令不会重抽、探测模型或外发材料。合法历史 native v5 原始输出可直接补审。发送给外部评审模型需要按既有出站授权流程处理完整材料；生成 sheet 本身不代表允许外发。

凭据版本为 `semantic-review-receipt-v1`，有效期为所声明实际评审时间起 30 天，绑定模型、执行器、run 路径及字节摘要、全部结果、完整 prompt 摘要、固定 suite 摘要和完整 review。缺失、过期、材料修改或绑定不符返回 `pending_semantic_regression`，不追加模型失败记录。完整评审发现语义错误才追加 `semantic_regression_fail`。有效 pass 凭据也仍需通过原随机盲评和全部指标。

## 新开局的真实模型探测

质量准入与调用可用性独立。新开局还要求每模型一份 15 分钟内、同一工具/模型/推理强度/主机/代码/代理配置的真实 nonce 回应凭据。登录状态、二进制存在或服务能力查询不能代替真实模型回应。

```sh
extract runtime probe --executor codex --model '<模型>' --effort medium --proxy-mode auto --out /private/model-probe.json
extract campaign start --id '<战役>' --models '<模型>' --executor codex --quota 200 --batch 50 --model-preflight /private/model-probe.json --effort medium --proxy-mode auto
```

多个模型给多份 `--model-preflight` 文件。`proxy-mode` 选项为 `auto|env-only|none`。手工工具没有可靠的实际模型探测接口时明确显示 `requiring_tool_side_probe`，不会假绿。已有显式 `--skip-admission '原因'` 仍只作为质量准入的人类例外记录；不会自动添加，也不会把缺失的真实模型探测当作通过。

新战役保存 `campaign-start-v2`、语义凭据摘要和探测凭据摘要。旧战役不会被自动迁移或重签。回归测试只证明新门真实参与判决，不代表任何真实参评模型已获新版准入。
