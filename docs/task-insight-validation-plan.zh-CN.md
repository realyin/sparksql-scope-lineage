# 任务理解工作台验证方案

## 1. 验证目标

任务理解工作台不只是把 `profile.json` 画成页面，它把 SQL 解析事实、业务画像、scope 图、字段血缘、规则和诊断信息串在一起。因此验证必须覆盖三类风险：

- 内容准确性：页面说的“读取了哪些表、用了哪些条件、哪个字段从哪里来”必须能回到 `lineage.json` / `profile.json` 的证据。
- 关联一致性：不同区域引用同一个 scope、字段、规则、表时，必须指向同一个对象，不能页面左边说一套、图上画另一套。
- 交互可信度：搜索、点击、高亮、业务视图/完整模式、缩放拖拽不能改变事实关系，也不能隐藏可能有问题的孤立 scope。

最终目标是让人和 agent 可以把页面当作可信的任务理解入口；如果有不确定、断链、schema 缺失或孤立 scope，页面必须显式提示，而不是悄悄省略。

## 2. 产物链路

标准链路如下：

```text
SQL + schema + table/column metadata
        ↓
lineage.json / profile.json / diagnostics.json
        ↓
task_insight.json
        ↓
task_insight.html
        ↓
LLM 画像 Markdown / 人工检查 / 任务理解工作台
```

验证必须至少覆盖 `lineage.json`、`profile.json`、`task_insight.json`、`task_insight.html` 四个文件。单独验证 HTML 是否能打开是不够的，因为很多错误发生在“页面模型和原始解析事实不一致”的转换层。

## 3. 内容准确性验证

### 3.1 任务级事实

- `task.task_name` 必须来自 `profile.task_name` 或 `lineage.task_id`。
- `task.target_table` 必须和 `profile.target_table` / `lineage.target_table` 一致。
- `task.lineage_scope_count` 必须等于 `lineage.scopes` 的真实数量，不能使用过期 diagnostics。
- `task.output_column_count` 应该和 `profile.end_to_end_lineage` 的输出字段数量一致。
- `trace_complete_count + trace_incomplete_count` 必须等于端到端字段数。

### 3.2 Scope 内容

每个 scope 需要验证：

- `scope_id` 能回到 `lineage.scopes` 或 `profile.scope_profile.steps`。
- `direct_inputs` 指向存在的 table/scope 对象。
- `direct_source_tables` 只表示当前 scope 直接读取的物理表。
- `physical_source_tables` 可以表示向上追溯到的物理表，但页面文案不能把间接追溯误写成“当前 scope 读取”。
- `summary` / `business_action` 中如果出现“读取”，必须有非空 `direct_source_tables` 支撑。
- `logic.filters`、`logic.joins`、`logic.window_functions`、`logic.case_when`、`logic.lateral_views` 等要和 profile 中的步骤保持一致。

重点防止这类错误：某个 CTE 实际只读取上游 CTE，但页面说明成“读取 dwd 表、ods 表等物理表”。

### 3.3 规则内容

每条业务规则候选需要验证：

- `rule.scope_ids` 指向存在的 scope。
- `rule.fields[].field_id` 指向存在的字段对象。
- `condition_summary` 和 `condition_expression` 至少保留一个；如果表达式被省略，必须标记 `expression_omitted=true`。
- 字段说明要能看出字段在规则中承担的作用，例如关联键、时间窗口、状态筛选、金额/次数指标。

### 3.4 字段血缘

每个输出字段需要验证：

- `objects.columns` 中存在对应输出字段。
- `physical_sources` 和 `derived_from_column` 链接一致。
- `trace_complete=false` 时必须有原因；`trace_complete=true` 时可以省略原因。
- 字段关联的 scope 必须存在。
- HTML 字段表和字段血缘图使用的是同一个字段对象。

### 3.5 元数据

`related_metadata` 需要验证：

- `input_tables` 覆盖输入物理表，`output_tables` 覆盖输出表。
- 页面中的表中文名、字段中文名来自 metadata 或业务知识，缺失时要降级显示英文名。
- 对于 `SELECT *` 或 schema 缺失导致的不确定字段，不能误删字段；应保留不确定项并在 diagnostics 中提示。

## 4. 页面关联验证

### 4.1 对象和链接完整性

- `links[].from` 和 `links[].to` 必须都能在 `objects` 中找到。
- `feeds` 链接只能连接表/scope 到 scope，不能连到不存在的隐藏节点。
- `references` 链接必须指向存在的 section/scope/rule/column。
- `implemented_by` 必须连接 rule 到 scope。
- `uses_field` 必须连接 rule 到 column。
- `produces` 必须连接 scope 到输出字段。

### 4.2 Scope DAG 和详情区

- 图上的每个 scope 节点必须能在详情区打开对应对象。
- 业务阶段列表引用的 scope 必须在 scope 对象中存在。
- 点击某个 scope 时，高亮范围应该只包含直接相关的输入、输出、规则、字段，不能把大量无关节点高亮。
- 如果一个 scope 没有下游，不能静默隐藏；业务视图可默认隐藏实现细节，但必须在页面提示，并能在完整模式显示。

### 4.3 业务视图和完整模式

- `业务视图`：默认展示对业务理解有帮助的主链路，隐藏 `hidden_in_business_view=true` 的实现细节节点。
- `完整模式`：必须展示所有 scope，尤其是孤立 scope、解析中间 scope、可能有问题的 dangling scope。
- 顶部统计需要同时说明完整 scope 数、展示 scope 数、隐藏 scope 数。
- `task.full_graph_scope_count` 必须等于完整 scope 对象数。
- `task.visible_scope_count` 必须等于业务视图可见 scope 对象数。

### 4.4 字段区和图联动

- 字段表点击某个输出字段后，字段血缘图必须展示该字段、物理来源字段和来源表。
- `traceFilter=INCOMPLETE` 只展示追溯不完整的字段。
- 字段中文名、英文名、转换表达式、追溯可信度必须来自同一个 column 对象。

## 5. 交互和布局验证

浏览器侧至少验证：

- 页面可离线打开，内嵌 JSON 可解析。
- Scope DAG 能缩放、平移、重置。
- 字段血缘图能缩放、平移、重置。
- scope 节点可拖动，拖动后连线随节点更新。
- 搜索 scope 后只弱化不匹配节点，不改变底层数据。
- 业务视图/完整模式切换后节点数量与统计一致。
- 大图不能被压成一条细线；布局应保留从输入到输出的加工方向。
- 两排之间要有足够间距，节点不能大面积重叠。

这些属于端到端浏览器验证，单元测试不能完全替代。每次改布局算法或交互逻辑，都需要至少用 3 个复杂任务截图确认。

## 6. 语料回归集

建议固定以下回归样本：

- 大型客服宽表任务：多输入表、多 union、多 scope、字段很多，用于验证图布局和直接/间接物理表说明。
- 催收入催任务：业务规则条件复杂，用于验证过滤条件、规则字段和业务目标还原。
- 包含 `lateral view posexplode` 的任务：验证数组展开是否进入 `lateral_views`。
- 包含 `UNION ALL` 多分支的任务：验证分支节点和分支来源表。
- 包含 `SELECT *` 的任务：验证 schema 补全和 trace 不完整提示。
- 缺少 schema 的任务：验证 UNKNOWN、未展开星号、metadata 缺失等边界不会被说成确定事实。
- 超大任务：验证 profile 瘦身后页面仍能说明核心业务逻辑。

## 7. 自动化校验

新增 `tools/validate_task_insight.py`，对生成物做静态交叉验证：

```bash
python tools/validate_task_insight.py --input <task_output_dir>
python tools/validate_task_insight.py --root <corpus_output_dir> --json-out /tmp/task_insight_validation.json
```

校验输出分为：

- `error`：事实或引用不一致，必须修复。
- `warning`：存在风险或需要人工确认，例如孤立 scope、metadata 缺失、HTML 未嵌入最新 JSON。

建议门禁：

- 本地开发：`error_count=0` 才能提交。
- 全量语料：`error_count=0`，warning 必须分类解释；新增 warning 类型需要补充说明。
- 发布前：固定样本截图验证通过，至少覆盖业务视图、完整模式、字段血缘图和详情区。

## 8. 手工验收清单

每次涉及 `profile.json`、`task_insight.json` 或 HTML 图逻辑的改动，至少抽查 5 个复杂任务：

- 任务目标是否能从业务语义理解出来。
- 输入表、输出表是否带英文名和中文语义。
- scope 顺序是否能看出加工链路。
- 每个阶段是否说明了关键条件，而不只是列字段名。
- 规则里字段和逻辑的关系是否明确。
- 直接读取表和间接追溯表是否区分清楚。
- 孤立 scope 是否被提示，并能在完整模式看到。
- 字段血缘是否能追到物理表/物理字段。
- trace 不完整、schema 缺失、星号未展开等边界是否显式披露。

## 9. 当前验收结论模板

每次完整验证后，应输出类似结论：

```text
验证范围：
- 单元测试：xxx passed
- 语料任务：ok=N, error=0
- task_insight 静态校验：tasks=N, errors=0, warnings=M
- 浏览器抽样：任务 A/B/C，业务视图、完整模式、字段血缘、点击详情通过

发现问题：
- warning_type_1: N 个，原因和处理计划
- warning_type_2: N 个，原因和处理计划

是否可合并：
- error=0 且 warning 已解释：可以合并
- 仍有事实错误或引用不一致：不可合并
```

