# V1 Case 1 根因调查与改进方案

本次只做离线、低优先级本地实验，不使用模型 API，不修改生产源码、官方 sibling replay protocol、数据集或历史输出。重算实验串行执行，使用独立目录。诊断脚本位于本目录。以下区分已证明的故障、局部修复结果及尚未验证的端到端收益。

## 必须纠正的旧结论

- 旧回复中“23 个 Basic 对 Financial 错”的筛选混入了 Financial 缺失/未评分任务；该数字无效。
- 原始结果加成功 ProviderError retry、按 task_id 合并后：Financial 180 个已评分，Basic 176 个。共同具有 Case 1 评分的任务 165 个。
- 配对四格：两者通过 40；两者失败 96；仅 Financial 通过 14；仅 Basic 通过 15。Financial 并非在相同任务上全面劣于 Basic。
- 两组全部已评分历史轨迹中，`harness.skills.routed.selected` 选中 `spreadsheet-financial-model` 的次数均为 0。插件出现在 composition/activated 事件，不等于提示词启用。
- 165 个配对任务中，156 个首次成功模型 response 对应的请求 payload SHA 相同，其中 155 个 response 文本不同。该 SHA 在 agent 中对 chat wire payload 序列化计算，包含请求内容；不包含服务端部署身份。不能推断具体后端非确定性的来源。
- 15 个仅 Basic 通过任务中，14 个首次成功模型请求 SHA 相同；剩下 `504-10` 的 Financial planner 截断，没有成功 response 可比较。不能把计划差异直接归因于 Financial 提示词。
- 先前报告合并均值计算有误。重新逐条求和：Financial Soft=0.3018518519，Hard=0.2611111111；Basic Soft=0.3143939394，Hard=0.2556818182。原始结果未修改。

## 15 个仅 Basic Case 1 通过的任务

`35207, 37228, 39931, 40757, 435-36, 46856, 491-6, 49782, 504-10, 50472, 54717, 56451, 592-34, 59791, CF_21040`

| task | 已核对的 Financial 实际问题 | 证据/限制 |
|---|---|---|
| 35207 | 每行重复三行和，未保留组间空白；数值也与 gold 缓存不同 | 不能仅用 Basic 通过证明其公式数学语义正确，尚需核对缓存及输入类型 |
| 37228 | 只写 G2:G6，G12:G23 保持空白；扩展后仍有单窗口分支错误 | 原 evidence 只展示前 6 行，但 dimension 明确到 23 |
| 39931 | 仅覆盖部分 C4:F6；查找源截为 I3:K6，C6 为 #N/A | gold 和 Basic 能匹配更完整 I3:K22 数据 |
| 40757 | 改 B9，而真正输出 B10:B11 仍空白 | 执行器运行了，但未纠正遗漏目标 |
| 435-36 | E7:F9 漏改，仍为 Wrong Company Name/空白 | evidence 跳过 7–9 行，planner 仅编辑展示过的片段 |
| 46856 | 新建 A1:C1 示例，B15:C15 真正输出留空 | Basic 执行器定位了已有汇总区域 |
| 491-6 | 要求 merge，却以 =B2 等公式复制内容，没有真正合并 | official value 对比中下方 cells 应为空；不是简单数值任务 |
| 49782 | 只写 B4:B6，B8:B11 漏填 | 扩展同一公式可恢复整题 Case 1 |
| 504-10 | planner model_response_truncated，没有完成执行 | 没有证据表明是 Financial skill 导致截断 |
| 50472 | 只写 B1:B2/B4:B5，其他非空源行漏填 | 直接扩展会在空行产生 #VALUE!，需要空值保护 |
| 54717 | 捏造 Z1:Z4 示例，S10 留空 | evidence 每行 cell 数受限，未提供 S 列实际参数 |
| 56451 | 只写 B2:F2，C3:F7 保留错误旧公式 | 扩展原公式到 B2:F7 可恢复 Case 1；官方答案不要求 G 列 |
| 592-34 | TEXTSPLIT 产生 #NAME?，delimiter 也被转义损坏，未展开输出 | 不只是范围问题；需能力兼容与数据字符串验证 |
| 59791 | 整数差值也无条件乘 100，D3=-4900，gold/Basic=-49 | 旧回复归因为 RF/空状态分支错误不成立，任务明确要求这些分支 |
| CF_21040 | 把高亮任务变为写公式，污染空白值；转义后 #VALUE! | 原始不修改 workbook 也能通过官方 value-only 检查，故 Basic 通过不能证明高亮正确 |

## 隔离实验结果

### 1. YAML 字符传输

`_yaml_evidence()` 在 YAML dump 后调用 `_safe_evidence()`，把 `<`/`>` 替换为字面量 `\u003c`/`\u003e`；`_parse_planner_yaml()` 未还原，字面量进入 Excel 公式。

纯函数复现：`=IF(B1>0,"<ok>","")` 经过规范化/解析变为 `=IF(B1\u003e0,"\u003cok\u003e","")`。这不是模型原始输出的错误。

Financial 已评分 180 条中 29 条 verified action 含这类转义，5 条 Case 1 通过、24 条失败；Basic 176 条中对应 37 条、6 通过、31 失败。不能直接把这些失败全部算成转义导致。

对 Financial 最终文件中仍保留被损坏公式的 18 个失败任务，仅在副本修正字符并重算：1/18 整题恢复通过（52050），多个任务减少错误单元格但仍受范围/语义问题影响。实验目录：`/tmp/v1-case1-transport-audit-8t1a_vm7/results.json`。每个源文件实验前后 SHA 校验一致。

### 2. 目标覆盖控制实验

保留 Financial 原公式，仅按输入 key 列最后非空行延伸到真实数据范围：

| task | 扩展范围 | 原始 | 仅扩展 | 扩展 + 最小语义保护 |
|---|---|---|---|---|
| 37228 | G2:G23 | fail | fail | pass（单窗口保留 F 值） |
| 49782 | B4:B11 | fail | pass | pass |
| 50472 | B1:B17 | fail | fail | pass（空白源行返回空白） |
| 56451 | B2:F7 | fail | pass | pass |

范围实验：`/tmp/v1-case1-coverage-audit-q5sxss4o/results.json`。

范围加语义保护：`/tmp/v1-case1-coverage-audit-xsp2dqa9/results.json`。

这 4 个是看过失败后手工选择的诊断案例，不是 held-out，4/4 修复不能表述为一般算法提升率。没有从 gold 复制值或公式；gold 仅用于最终评分。

## 证据边界与后续验证

1. 是否有准确区分部分 evidence 与完整覆盖的通用 gate，可以拦住坏计划又不误伤正确任务。
2. 对 14 个 Financial 正 Basic 错的反方向任务做对称审计，避免只找支持预设结论的案例。
3. 评估 executor 缺失与真实错误的关系；12/15 Financial 退化任务未调用 executor 只是关联，不能直接当作“强制 executor 能修好 12 个”。
4. 复查历史 manifest、wire payload 与当前源码差异，建立可复现且不影响运行实验的正式消融设计。

## 后续审计增量

### 不修改输入的官方评分基线

对固定 200 个 Case 1 的原始 input 直接运行原有 official comparator（只读）：18/200 已经通过。其中 Financial 实际仍通过 13 个、改成失败 3 个、未评分 2 个；Basic 对应 12、4、2。这个结果不用于替代正式分数，只说明 value-only 比较有盲区；特别不能把 CF task 的通过当作正确高亮的证明。

脚本：`analyse/probe_case1_noop.py`。18 个 ID：151-13、193-42、31420、435-38、494-13、570-43、7-5、CF_12196、CF_12429、CF_13984、CF_17940、CF_18814、CF_18989、CF_21040、CF_24632、CF_29431、CF_8276、CF_9945。

### 候选门槛：写入目标出现公式错误时不能直接结束

规则只读取历史 verified actions 指向的最终单元格错误类型，不读取 gold。用于回顾性评估，尚未运行 executor 补救。

| Arm | verified 且未调用 executor | 其中官方失败 | 错误值门槛命中 | 命中中的官方失败/通过 |
|---|---:|---:|---:|---:|
| Financial | 89 | 62 | 26 | 22 / 4 |
| Basic | 82 | 57 | 31 | 26 / 5 |

该门槛能够检出 Financial 的 22/62 个 fast-path 失败，但不能预测修复后的通过率；4 个已通过任务也会被拦截。部分命中中的“官方通过”仅因为错误被写到答案范围外，所以不应简单视为误报。部署时应检查新增错误、合理错误值与验证范围，不能照搬全表零错误规则。

脚本：`analyse/probe_case1_gates.py`。这也直接证明 mutation verified 不等于公式运行成功。

### 反方向对照（Financial 对、Basic 错）

- `36842`：Basic 原公式的固定文本源写为相对 `Sheet1!A1`，填充后 B3 引用了 A2，输出 0 而 gold=1；Financial 使用 `$A$1`，输出正确。证实引用绑定是共享问题。
- `32895`：Basic 写了 Product List 的示例区域，Inventory!C9:C14 仍空；Financial executor 填完整。与反方向的“凭样例结束”是同一问题。
- `37229`：Basic 原始 SUMIFS 公式本身合理，但运行输出保存了字面量 `\\u003e=`/`\\u003c=`，导致结果空；Financial 用不含比较字符的 SUMPRODUCT 通过。不能把这个差异归为领域能力。
- `23-24`：Basic 删除后答案尾部输出 0，gold/Financial 为空；需要检查尾部清理的真实工具执行，不是重放问题。
- `2768`：Basic 公式比较字符被损坏，且填充后部分范围长度不一致；Financial 的 MIN/SUMIFS 方案通过。不能仅修字符后就保证通过。
- `38462`：Basic C2 留空，另找输出位置，Financial C2=49332；是目标定位差异。
- `41692`：Basic AGGREGATE/IFERROR 方案输出全空，Financial SMALL/IF 方案有值；需要进一步定位引擎兼容性，不能仅凭函数名定因。
- `50232`：Basic 首场休息日=1，gold/Financial=0；首行边界值不同。
- `51090`：Basic 写 S 列而 Q3 保持 0，gold/Financial=699；再次出现错列。
- `531-48`：Basic 过滤后 A4/A5 空白，gold/Financial 有保留记录；需要进一步检查原地覆盖/过滤实现。
- `547-43`：Basic 写入 `Emp.$A$2:$A$4` 等引用，最终 #NAME?；Financial 使用 `Emp!` 正常。
- `59433`：Basic B5/B6 留空，Financial 填出 site code；目标定位而非 Financial 领域语义。
- `CF_18814`：两者 C3 的公式都是 =E1，Basic 缓存 #VALUE! 而 Financial 日期正常；需要追溯依赖/重算，不应错误归为公式字符串不同。
- `CF_9945`：Basic 用普通公式覆盖 C3:G3 原值，Financial 把公式写在 C14:I14，未破坏答案区；Financial 通过并不能证明条件格式完成。

## 改进方案（按实施顺序）

1. **先修共享的表示层错误**：执行结构化 plan 与展示给模型的转义 evidence 分离；在 YAML 解析对象层保证字符串逐字保真。不能全局 unicode 解码，否则会损坏合法的反斜杠文本。增加 `<`、`>`、`<>`、HTML delimiter、单/双引号 YAML 的 round-trip 测试。
2. **把 fast path 完成条件从变更单元格计数改为契约**：目前“至少 3 个变更”不是完整性证据。门槛至少包含目标覆盖、操作类型匹配和重算后的目标结果检查；不能把原本 merge/delete/highlight 的指令通过写公式视为完成。
3. **明确 evidence 是部分视图**：现有样本行被 planner 当作完整数据。提供区域边界、实际非空 key 范围、示例区与待填区、未展示区域标记；预算有限时先让 executor 定位，而不是创作 Z1:Z4 演示。
4. **边界测试由输入驱动，不读 gold**：单/双窗口、空源行、首行、已有错误/空白、相对/绝对引用；只放大填充范围会引入新错误，4 个目标覆盖实验已经证实。
5. **新实验必须区分插件消融与生成波动**：V1 当前 financial skill 路由未启用，现有两组近似相同提示条件的重复采样，不是有效的 Financial 专项增强消融。正式比较要锁定代码快照、记录真实 selected skills 和 request SHA；对相同输入至少做重复运行，按配对 Case 1 与费用汇报，不修改官方评分或 sibling protocol。

以上属于方案而不是已部署改动；生产源码和现有实验仍未触碰。

## 重算对照及可复核资料

对 52050、37228、49782、50472、56451 做无修复对照：同样 load/save 和重算，但不改变公式/范围，0/5 恢复。结合修复组结果，排除了这 5 个案例仅因重新保存或重算就恢复的解释。每个原始输出的 SHA 在实验前后相同。

- `case1_paired_evidence.json`：固定 200 个 task 的合并结果来源、Case 1 评分、首次成功请求 SHA、实际 selected skills。
- `case1_local_experiments.json`：18 个转义实验、4 个范围实验、4 个范围加语义保护实验、5 个无修复重算对照；包含候选文件路径及具体修改。
- `probe_recalculation_control.py`：对照复现脚本。
- 所有修复只作用于 `/tmp/v1-case1-*-audit-*` 或 `/tmp/v1-case1-recalc-control-*` 内的副本。JSON 已归档在本目录，临时 workbook 路径若被系统清理，可用脚本重新生成。

## 正式验收设计：不能直接声称准确率已提升

| 阶段 | 唯一主要变量 | 验收标准 | 不允许的结论 |
|---|---|---|---|
| P0 表示层 | 执行 plan 保持结构化，展示转义不回流执行 | raw→parse→execute 的字符串逐字一致；合法反斜杠不被错误解码；52050 定向回归通过 | 不能把 29 个含转义任务都算成可救回 |
| P1 完成门槛 | 保存后重算，新增目标错误触发 executor | 错误包含 sheet/cell/value；不吞失败；合理 #N/A 由任务契约判断；对照已通过任务的误伤 | 22 个被检出的失败不等于 22 个能被 executor 救回 |
| P2 覆盖与定位 | evidence 标记采样边界；planner 给出源/目标完整区域和来源 | 覆盖首/中/尾及空行；缺定位证据就检查，不捏造示例区；4 个案例通过且增加反例测试 | 禁止按 task_id 或官方 answer_position 在运行时指定范围 |
| P3 操作语义 | merge/delete/highlight 与 mutation 类型对应 | 合并需真实 merged ranges；删除需保留行序及清理尾部；条件格式不能污染值 | 不以 value-only pass 代替样式验证，也不修改官方分数 |
| P4 端到端确认 | 锁定代码快照逐项开启 P0/P1/P2/P3 | 原 200 task 完整 protocol、同预算；按 task 配对，报告 Case 1/Soft/Hard、覆盖率、调用数和耗时；保留全部原结果 | 不把定向诊断的 4/4 当作总体或 held-out 通过率 |

初步建议先实现 P0，再做 P1/P2，而不是先改 Financial Plugin。P0 是代码级可复现错误；P2 的两个纯范围修复有独立评分证据；P1 是可测量的缺失防线，但收益还须新的 executor 运行验证。

正式模型实验应在现有实验完成或获得独立配额后进行，避免争用同一服务。至少给相同配置重复运行以估计波动；报告 Financial/Basic 的实际路由是否有区别。对本次已经查看过的任务只能报告回归结果，另设未查看的任务做确认，不能称这些手工案例为泛化验证。

## 最终判断

**没有证据支持“Financial Plugin 在这些 V1 任务上额外做了金融修复，从而把结果破坏”。有直接证据支持：两组多数首次请求相同，计划却不同；共同的 harness 把不完整/不兼容/被转义损坏的计划执行完就结束。**

根因链条是：局部 evidence → 错误地推断完整任务 → 缺少完整范围/操作类型证明 → 只验证计划里的 mutation → 跳过 executor/缺少计算反馈。公式字符传输错误又造成独立的执行内容损坏。这解释了为什么之前 execution contract 修复生效却没有明显涨分：它证明“计划里的字串确实写了”，没有证明“写入字串保真、公式能运行、任务范围完整”。

不能据这些材料唯一解释所有 121 个 Financial Case 1 失败；本调查的强证据集中在配对退化案例和两个共享系统缺陷。引擎兼容性、缓存异常与其余失败还应保留为待归因，而非填上未经证实的统一解释。
