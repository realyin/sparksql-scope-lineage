# LLM Profile 使用指南

`profile.json` 是给大模型读取的 SQL 任务画像文件。它不是完整血缘明细，
而是从 `lineage.json` 中抽出的高密度摘要，用来帮助大模型快速判断：

- 这个 SQL 任务在生成什么表；
- 输入表和输出表是什么；
- SQL 有哪些主要加工步骤；
- 每个步骤大致做了什么；
- 哪些输出字段是关键字段或指标；
- 字段血缘是否完整，有哪些准确性边界。

完整机器血缘仍然以 `lineage.json` 为准；完整诊断信息以 `diagnostics.json` 为准。

## 推荐读取顺序

建议 LLM 按 `profile.json.read_order` 读取。当前推荐顺序是：

1. `summary`
2. `business_profile`
3. `grain`
4. `scope_profile.steps`
5. `business_rule_candidates`
6. `important_columns`
7. `end_to_end_lineage`
8. `related_metadata`

这个顺序的意图是先建立全局理解，再看加工链路，最后核对字段和血缘可信度。

`business_profile` 和 `business_rule_candidates` 是业务解释层。前者给出任务目标、
每个 scope 的业务段落骨架和处理动作；后者把 WHERE/HAVING/JOIN 条件拆成规则候选，
附上涉及字段、字段中文注释、操作符线索和原始条件摘要。LLM 应优先用这两部分归纳
“这个任务的目标是什么、由几部分构成、每部分判断条件是什么、然后怎么处理”，再用
`scope_profile.steps` 和 `end_to_end_lineage` 校验事实。

详细还原模式下，LLM 输出必须保留三类定位关系：

- 阶段和 scope 的关系：每个加工阶段都要写出 `scope_profile.steps[].name`；
- 规则和条件的关系：每条规则都要写出条件表达式或可读条件摘要；
- 字段和规则的关系：每个关键字段都要解释它在条件、关联、聚合、窗口或派生中
  承担什么判断作用。

如果只输出“涉及字段：a、b、c”，但没有说明条件是什么、字段如何参与判断，
这类结果不能视为合格的详细还原。

注意：`business_profile.objective.summary` 仍然是程序生成的线索，不是人工确认的最终业务
结论。LLM 不应只复述它，而应结合：

- `business_profile.sections`：任务拆成了哪些处理阶段；
- `business_rule_candidates`：每个阶段的 WHERE/JOIN/HAVING 条件涉及哪些字段；
- `related_metadata`：表中文名、表描述、字段中文注释；
- `important_columns` 和 `end_to_end_lineage`：核心输出字段及来源；
- `expression_catalog`、`filters_summary`、`diagnostics`：派生逻辑、过滤规则和风险边界；

共同归纳业务目标和判断逻辑。

## 输出模式

建议把基于 profile 的 LLM 输出固定为两种模式：

- 摘要模式：用于检索、预览、知识库卡片。只保留任务定位、输入输出、核心规则、核心字段和主要风险。
- 详细还原模式：用于数据治理、交接、代码评审、业务规则核对。尽可能还原 SQL 业务逻辑，展开关键
  scope、过滤条件、CASE/窗口/聚合/UNION 分支、字段含义和风险边界。

两种模式使用同一个 `profile.json`，区别只在 prompt 对输出详略的要求。

## role 的理解方式

`scope_profile.steps[].role` 是技术加工角色，不是业务结论。LLM 生成文档时应把 role 翻译成业务动作：

- `filter`：按条件筛选、保留或排除记录；
- `join`：关联补充信息、维表映射、名单命中判断；
- `dedup`：构造去重名单、取最新/唯一记录、避免关联放大；
- `aggregate`：按某个粒度汇总指标，例如次数、金额、最近时间；
- `window`：排序取首、取末、取最新、序号去重、跨行比较；
- `case_when`：派生分类、状态、标签或标志位；
- `union`：合并多个来源、渠道或策略分支；
- `lateral_view`：展开数组、明细项或复杂类型。

例如 `role: dedup` 不应在文档里只写“角色：dedup”，而应解释为：

```text
该步骤从超额放款合同维表中构造合同号去重名单，用于后续判断主表合同是否命中特殊合同范围。
```

## 1. 从 summary 判断任务主题

`summary` 是任务入口。重点读取：

- `task_name`：任务名；
- `target_table`：写入目标表；
- `stmt_kind`：写入类型，例如 `INSERT`、`INSERT_OVERWRITE`；
- `input_table_count`：输入物理表数量；
- `output_column_count`：输出字段数量；
- `main_operations`：主要加工类型；
- `main_process`：规则生成的任务加工概述。

LLM 可以用它生成一级概览，例如：

```text
该任务从多张上游表读取数据，经过过滤、关联、聚合和 CASE 派生后，
写入目标宽表 xxx。
```

注意：`main_process` 是基于 SQL 结构生成的事实摘要，不应被理解为人工确认过的业务定义。

## 2. 从 grain 判断数据粒度

`grain` 用来提示输出表的一行大概代表什么。重点读取：

- `type`：`record_level` 或 `aggregate_level`；
- `keys`：候选输出标识字段；
- `key_type`：当前固定为 `candidate_output_keys`；
- `confidence`：粒度推断置信度；
- `evidence`：推断依据；
- `note`：粒度解释边界。

`grain.keys` 是启发式候选字段，不是经过约束验证的主键。LLM 在输出中应写成：

```text
候选粒度字段包括 call_id、unique_id，推断粒度为明细/聚合级别。
这些字段是候选输出标识，不等同于已验证主键。
```

不要写成：

```text
该表主键是 call_id、unique_id。
```

除非 SQL 或外部元数据明确提供了主键约束。

## 3. 从 scope_profile.steps 还原 SQL 加工链路

`scope_profile.steps` 是理解 SQL 加工过程的核心。每个 step 代表一个保留下来的
有业务意义的 scope，例如 CTE、子查询、UNION 汇总或 ROOT。

重点字段：

- `name`：步骤名；
- `kind`：scope 类型，例如 `cte`、`subquery`、`union`、`root`；
- `role`：步骤角色，例如 `filter`、`join`、`aggregate`、`dedup`；
- `operations`：实际操作类型；
- `business_summary`：规则生成的一句话加工摘要；
- `direct_inputs`：直接上游；
- `physical_source_tables`：追溯到的物理源表；
- `output_columns`：该步骤输出字段数；
- `logic`：结构化加工逻辑。

`logic` 中常用信息：

- `joins`：关联方式、右表、关联条件；
- `filters`：WHERE/HAVING 条件；
- `aggregations`：聚合字段；
- `window_functions`：窗口函数字段；
- `case_when`：CASE 派生字段摘要；
- `key_renames`：关键字段重命名；
- `distinct`：是否去重；
- `union_branches`：UNION 分支数量；
- `lateral_views`：数组展开或生成函数。

LLM 应按 steps 的顺序还原加工链路，例如：

```text
1. scope=cte:base_call：读取通话明细表，按 dt='20260515' 和 is_deleted='false'
   过滤有效分区数据。
2. scope=cte:call_agg：对通话记录按 call_id 聚合，生成通话次数和最近通话时间。
3. scope=ROOT：关联工单、评价、坐席等信息，补充上下文字段，并写入目标宽表。
```

详细还原模式下，每个关键步骤建议使用固定结构：

```text
阶段名称：构造待入催贷款基础范围
scope：cte:loan_info_public_a_tmp
scope 类型：cte
技术角色：filter
业务动作：从贷款全量表中筛选可能需要催收关注的贷款记录
输入：clct_file_fasloan_model（贷款全量数据）
关键条件：
- dpd between -7 and 0：用逾期天数字段识别提前 7 天到当天的预警窗口。
- forced_pay_off = 'Y'：用强制还款标记识别特殊纳入范围。
关键字段关系：
- dpd / 逾期天数：参与提前预警和逾期窗口判断。
- forced_pay_off / 强制还款标记：参与特殊纳入规则。
输出/作用：产出待入催贷款明细，供后续客户清单和贷款明细输出使用。
```

如果 profile 里没有完整条件，只能根据字段和 raw_summary 推断，应写明：

```text
profile 中没有保留完整条件表达式；以下仅根据 business_rule_candidates.fields
和字段注释推断规则作用，完整条件需查看 lineage.json。
```

如果 `role` 是 `aggregate`，优先解释分组和指标。  
如果 `role` 是 `join`，优先解释主表和补充字段。  
如果 `role` 是 `dedup`，优先解释窗口排序或去重逻辑。  
如果存在 `lateral_views`，说明有数组或复杂类型展开。

## 4. 从 important_columns 找核心字段和指标

`important_columns` 是从最终输出字段中筛出的重点字段索引。它不是完整字段列表，
而是帮助 LLM 优先关注关键字段。

重点字段：

- `column`：字段名；
- `transform`：最终字段转换类型；
- `importance`：重要性级别；
- `reasons`：命中原因。

常见 `reasons`：

- `id_or_key_column`：像 ID 或 key；
- `date_or_partition_column`：日期或分区字段；
- `business_classification_column`：状态、类型、层级、标记字段；
- `metric_like_column`：金额、数量、分数、比例类指标；
- `derived_from_physical_sources`：来源中存在派生加工；
- `transform:AGGREGATE`、`transform:CONDITIONAL` 等：字段本身是聚合或条件派生。

LLM 可以基于它生成核心字段/指标说明：

```text
核心标识字段包括 call_id、unique_id；
核心时间字段包括 begin_call_dt、end_call_dt；
核心分类字段包括 call_type、risklevel；
核心指标字段包括 call_cnt、duration、score。
```

不要把 `important_columns` 当成全部输出字段。完整输出字段应以 `end_to_end_lineage`
或目标表 schema 为准。

## 5. 从 end_to_end_lineage 判断字段来源可信度

`end_to_end_lineage` 是 ROOT 输出字段到物理表字段的端到端追溯结果。

重点字段：

- `column`：输出字段；
- `transform`：字段转换类型；
- `expression`：目标侧表达式，可能被截断；
- `trace_complete`：是否完整追溯到物理字段；
- `physical_sources`：物理来源字段；
- `trace_incomplete_reasons`：仅在追溯不完整时出现。

如果 `trace_complete=true`，LLM 可以认为该字段已经追溯到物理来源。  
如果 `trace_complete=false`，LLM 应明确说明该字段血缘存在边界。

示例输出：

```text
字段 call_id 可完整追溯到 dm_opr.dmd_opr_lia_call_info_df.call_id。
字段 a.* 因 SELECT * 未完全展开，无法确认全部物理来源。
```

`physical_sources` 可能因 profile 瘦身被截断。如果存在：

- `physical_sources_truncated=true`
- `physical_source_count`
- `shown_physical_source_count`

说明 profile 只展示了部分来源，完整细节应查看 `lineage.json`。

## 6. 处理 trace_complete=false

`trace_complete=false` 不一定代表解析错误，它表示当前 profile 不能完整证明字段来源。
常见原因：

- `star_not_expanded`：`SELECT *` 或 `alias.*` 没有完整 schema 支撑；
- `unknown_source`：字段无法绑定到明确来源；
- `missing_scope_column`：中间 scope 没有暴露被引用字段；
- `cycle_detected`：出现循环引用保护。

LLM 在生成任务画像时应将这些字段放入“风险边界”部分，而不是强行解释来源。

建议表达：

```text
以下字段血缘不完整，主要原因是 SELECT * 未展开或 schema 覆盖不足。
这些字段不影响对主加工链路的理解，但不适合用于精确字段追溯。
```

不要表达为：

```text
这些字段一定没有来源。
```

## 7. 识别 schema 缺失导致的边界

schema 缺失或不完整通常会体现在：

- `diagnostics.warning_types.star_not_expanded`；
- `diagnostics.warning_types.unresolved_unqualified_no_schema`；
- `end_to_end_lineage[].trace_incomplete_reasons` 包含 `star_not_expanded`；
- `related_metadata.*.metadata_complete=false`；
- audit 或 YELLOW action 报告中的 `schema_incomplete_column_ref`。

如果出现这些信号，LLM 应说明：

```text
当前结果受 schema 覆盖限制。提供完整 schema 后，SELECT * 字段和部分未限定字段
可以进一步补全。
```

在 schema 不完整时，LLM 可以继续解释 SQL 的主链路和已解析字段，但不应声称所有字段血缘都完整。

## 8. 使用表中文名和字段中文注释增强语义

当传入 `--table-metadata` 和带 `column_comment` 的 schema 后，
`related_metadata.input_tables/output_tables` 会包含两类语义信息：

- `table_metadata`：表中文名、表描述、数据分层；
- `column_details[].comment`：字段中文注释或业务含义。

LLM 应优先使用这些信息解释输入输出和核心字段。

例如，不要只写：

```text
读取 dm_opr.dmd_opr_lia_call_info_df。
```

如果 `table_metadata.table_name_cn` 或 `table_desc` 存在，应写成：

```text
读取“热线通话明细表”（dm_opr.dmd_opr_lia_call_info_df），作为热线通话事实来源。
```

字段说明也应优先使用 `column_details.comment`：

```text
核心字段 call_id 表示通话ID，begin_call_dt 表示通话开始时间。
```

如果字段没有 comment，再退回字段名和 SQL 表达式进行解释。

## 任务分级结构输出模板

LLM 最终可以按 L1-L5 输出任务画像。

### L1：任务概览

说明任务目标、目标表、SQL 类型、输入表数量、输出字段数量、主要操作。

模板：

```text
任务 {task_name} 写入 {target_table}，SQL 类型为 {stmt_kind}。
该任务从 {input_table_count} 张输入表读取数据，输出 {output_column_count} 个字段。
主要加工包括 {main_operations}。
```

### L2：输入输出

说明输入表、输出表、关键元数据覆盖情况。优先使用 `table_metadata.table_name_cn`
和 `table_metadata.table_desc` 解释表的业务含义。

模板：

```text
输入表：
- 英文表名：clct_file_fasloan_model
  中文表名：贷款全量数据
  表语义：作为贷款账户、账单、逾期和还款状态的事实来源。
  核心字段：
  - acct_nbr / 账户号：用于识别贷款账户。
  - dpd / 逾期天数：用于入催窗口判断。

输出表：
- 英文表名：clct_cf_pre_in_coll_cust
  中文表名：待入催客户清单
  输出对象：满足入催条件的客户和账户组合。
  核心字段：
  - internal_customer_id / 内部客户号：输出客户标识。
  - acct_nbr / 账户号：输出账户标识。

输入/输出字段元数据来自 related_metadata；若 metadata_complete=false，
表示该表字段类型或注释可能不完整。
```

### L3：加工步骤

基于 `scope_profile.steps` 输出加工链路。

模板：

```text
加工链路：
1. 阶段名称：...
   scope：{step.name}
   scope 类型：{step.kind}
   技术角色：{step.role}
   业务动作：...
   输入：...
   关键条件：...
   关键字段关系：...
   输出/作用：...
```

需要时补充 joins、filters、aggregations、window_functions、case_when。

### L4：核心字段/指标

基于 `important_columns` 和 `end_to_end_lineage` 输出核心字段。
如果 `related_metadata` 中有字段注释，优先用字段注释解释字段语义。

模板：

```text
核心标识字段：...
核心时间/分区字段：...
核心分类字段：...
核心指标字段：...
重要派生字段：...
```

如果字段来自 `case_when`，说明其分支数量和大致用途。  
如果字段来自聚合，说明聚合函数和来源字段。

### L3.5：业务规则/判断逻辑

基于 `business_rule_candidates`、`filters_summary` 和 `scope_profile.steps[].logic`
输出业务规则。规则必须绑定 scope，并解释条件和字段关系。

模板：

```text
规则名称：提前入催预警
所在 scope：cte:loan_info_public_a_tmp
条件：dpd between -7 and 0
涉及字段：
- dpd / 逾期天数：在该条件中用于判断贷款是否进入提前 7 天预警窗口。
规则作用：满足该条件的贷款会进入待入催基础范围。
处理结果：后续输出到待入催客户清单和贷款明细。
```

不要写成：

```text
规则：入催筛选
涉及字段：dpd、forced_pay_off、grace_date
```

这种写法缺少条件和字段作用，不能支撑业务核对。

### L5：血缘可信度和风险边界

基于 `trace_complete`、`diagnostics` 和 schema 信号输出可信度。

模板：

```text
血缘可信度：
- 已完整追溯字段：N 个；
- 追溯不完整字段：M 个；
- 主要边界：SELECT * 未完整展开 / schema 覆盖不足 / 未限定字段保守绑定。

风险边界：
- 字段 xxx trace_complete=false，原因是 star_not_expanded；
- 表 yyy metadata_complete=false，字段类型/注释可能不完整；
- diagnostics 中存在 magic_number，仅提示硬编码值，不一定影响血缘。
```

## 建议 Prompt 片段

可以把 `profile.json` 作为输入，并要求 LLM 按以下规则输出：

```text
你是数据仓库 SQL 任务画像分析助手。
请只基于给定 profile.json 生成任务分级结构，不要编造 profile 中不存在的业务事实。

阅读顺序：
1. summary
2. business_profile
3. grain
4. scope_profile.steps
5. business_rule_candidates
6. important_columns
7. end_to_end_lineage
8. related_metadata

输出结构：
L1：任务概览
L2：输入输出
L3：加工步骤
L4：核心字段/指标
L5：血缘可信度和风险边界

要求：
- grain.keys 只能描述为候选输出标识字段，不要说成主键。
- trace_complete=false 的字段必须放入风险边界。
- schema 缺失导致的 SELECT * 不完整要明确说明。
- 如果 expression 或 physical_sources 被截断，说明需要查看 lineage.json 获取完整细节。
- 不要把 diagnostics.magic_number 当作血缘错误，只作为硬编码提示。
```

## 使用边界

`profile.json` 适合：

- 生成任务画像；
- 解释 SQL 主加工逻辑；
- 找输入输出表和关键字段；
- 给 LLM 做问答上下文；
- 快速判断字段血缘是否完整。

`profile.json` 不适合单独承担：

- 完整中间 scope 逐字段追溯；
- 所有物理来源字段穷举；
- 完整 diagnostics 排查；
- Mermaid 图或 HTML 报告渲染。

需要这些信息时，应读取 `lineage.json`、`diagnostics.json` 或 `report.html`。
