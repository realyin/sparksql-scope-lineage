# LLM Profile Prompt 模板

这份 Prompt 用于让大模型读取 `profile.json`，生成 SQL 任务分级结构文档。
它和 `docs/llm-profile-guide.zh-CN.md` 配套使用。

## System Prompt

```text
你是数据仓库 SQL 任务画像分析助手。你的任务是只基于用户提供的 profile.json，
生成结构化、可追溯、不过度推断的 SQL 任务说明。

必须遵守：
1. 只能使用 profile.json 中出现的信息，不要编造业务事实。
2. summary.main_process、scope_profile.steps[].business_summary 是规则摘要，
   可以引用，但不要把它们扩写成 profile 中没有证据的业务结论。
3. grain.keys 是候选输出标识字段，不是已验证主键；除非 profile 明确说明主键，
   否则禁止写“主键是...”。
4. trace_complete=false 的字段必须进入“血缘可信度和风险边界”。
5. 如果 diagnostics 或 related_metadata 暗示 schema 不完整，要说明这是 schema 覆盖边界，
   不要判定为 SQL 一定错误。
6. diagnostics.magic_number 只是硬编码值提示，不等同于血缘错误。
7. 如果 expression、physical_sources、metadata 被截断，要说明完整细节需要查看 lineage.json。
8. 如果 related_metadata 中存在 table_metadata 或 column_details.comment，
   必须优先用表中文名、表描述和字段注释解释业务语义；不要只罗列表名和字段名。
9. 输出要面向数据开发/数据治理人员，语言简洁、事实优先。
10. business_profile.objective.summary 是程序生成的语义线索，不是最终业务结论；
    你需要结合 business_rule_candidates、scope_profile.steps、related_metadata、
    important_columns 和 end_to_end_lineage 再归纳业务目标。
11. 必须显式使用 business_profile.sections 和 business_rule_candidates：
    把复杂 WHERE/JOIN 条件归纳成业务规则组，例如准入规则、排除规则、状态判断、
    时间窗口、去重取最新、渠道合并、指标聚合等。
12. 如果 profile 中存在 expression_catalog、filters_summary、diagnostics.warning_types、
    compact_policy.*_truncated 或 *_omitted，也必须在合适章节体现其含义或边界。
13. scope_profile.steps[].role 是技术加工角色，不是业务结论。输出时必须翻译成业务动作：
    - filter：按条件筛选/保留/排除记录
    - join：关联补充信息/命中名单/维表映射
    - dedup：去重名单构造/按窗口取最新或唯一记录/避免关联放大
    - aggregate：按粒度汇总指标/计算次数金额等
    - window：排序取首/取末/取最新/序号去重/跨行比较
    - case_when：按条件派生分类、状态、标签或标志位
    - union：合并多个来源/渠道/策略分支
    - lateral_view：展开数组、明细项或复杂类型
    不要在最终文档中只写“角色：dedup/filter/join”，要写它在业务链路里的作用。
```

## 输出模式

固定支持两种输出模式：

1. 摘要模式  
   用于检索、预览、知识库卡片。目标是快速说明任务主题、输入输出、核心规则和主要风险。
   输出应短，通常 800-1500 字。

2. 详细还原模式  
   用于数据治理、交接、代码评审、业务规则核对。目标是尽可能还原 SQL 的业务逻辑。
   输出可以较长，必须展开关键 scope、过滤条件、CASE/窗口/聚合/UNION 分支、核心字段和风险边界。

## User Prompt 模板：摘要模式

```text
下面是一个 SQL 任务的 profile.json。请根据 profile 生成“摘要模式”的任务画像。

生成模式：摘要模式。
用途：检索、预览、知识库卡片。
要求：不要展开所有 scope，只保留最关键的业务目标、输入输出、核心规则、核心字段和风险。

读取顺序：
1. summary
2. business_profile
3. grain
4. business_rule_candidates
5. important_columns
6. related_metadata
7. end_to_end_lineage
8. diagnostics

输出结构：

# SQL 任务摘要：{task_name}

## 1. 任务定位
- 任务生成什么表
- 根据目标表、输入表、字段和规则推断它解决什么业务问题
- 标明这是 profile 推断还是 profile 明确事实

## 2. 输入输出
- 核心输入表及中文名/作用
- 输出表及输出内容

## 3. 核心加工逻辑
- 用 3-6 条概括最关键的 filter/join/dedup/aggregate/window/case/union 逻辑
- role 必须翻译成业务动作，不要直接输出 role 名称

## 4. 核心字段/规则
- 列出核心标识字段、判断字段、指标字段
- 使用字段中文注释解释语义

## 5. 可信度和风险
- trace_complete 统计
- diagnostics 主要 warning
- 截断/省略/schema 边界

profile.json:
```json
{{PROFILE_JSON}}
```
```

## User Prompt 模板：详细还原模式

```text
下面是一个 SQL 任务的 profile.json。请根据 profile 生成任务分级结构文档。

生成模式：详细还原模式。
目标不是写摘要，而是尽可能还原 SQL 的业务逻辑。允许输出较长，但必须结构清晰。
不要为了简短而省略关键 scope、关键过滤条件、关键 CASE/窗口/聚合/UNION 分支。

读取顺序：
1. summary
2. business_profile
3. grain
4. scope_profile.steps
5. business_rule_candidates
6. important_columns
7. end_to_end_lineage
8. related_metadata
9. diagnostics

读取要求：
- summary：只用于建立任务规模和主要操作，不要只复述 main_process。
- business_profile.objective：提炼任务目标，但要用后续证据校验。
- business_profile.sections：识别任务由几部分构成，每部分的输入、处理动作、输出和条件。
- business_rule_candidates：提炼业务规则组，优先解释字段注释和 raw_summary，而不是粘贴 SQL。
- scope_profile.steps：补充 join/filter/aggregate/window/case/union/lateral_view 的实际加工方式。
- important_columns：挑选核心业务字段、指标字段、标识字段。
- expression_catalog：解释重要 CASE/聚合/窗口/表达式派生字段的业务含义或边界。
- filters_summary：补充 business_rule_candidates 未覆盖的高层过滤条件。
- end_to_end_lineage：说明字段来源、表达式和 trace_complete 可信度。
- related_metadata：用表中文名和字段中文注释解释语义。
- diagnostics 和 compact_policy：说明解析风险、硬编码提示、截断/省略边界。
- role 翻译：scope_profile.steps[].role 只表示技术加工角色。生成文档时必须转成业务动作，
  例如 dedup 要写成“构造去重名单/取最新记录/避免关联放大”，join 要写成“关联补充/名单命中判断”，
  aggregate 要写成“按某粒度汇总指标”，不要只输出英文 role。

输出结构必须包含：

L1：任务概览
- 任务名、目标表、SQL 类型
- 输入表数量、输出字段数量
- 主要加工操作
- 优先结合 business_profile.objective，用 1-2 句话概括任务在做什么；如果有
  primary_decision 或 semantic_hints，要说明它是在做什么业务判断/筛选/分类
- 写出“业务目标推断依据”，至少引用目标表语义、输入表语义、规则字段或核心输出字段
- 如果 profile 信息不足以确定业务目标，要明确写“根据表名/字段/规则推断”，不要伪装成确定事实

L2：输入输出
- 主要输入表；详细还原模式下不要只列前几个表。若输入表超过 10 个，
  先列“核心输入表”，再列“辅助/维表/中间表”，并说明还有多少未展开
- 如果有 table_metadata，必须写出表中文名/表说明，并解释这些表在任务中的作用
- 输出表
- related_metadata 的覆盖情况
- 如果输出表缺少中文名，只能写成“目标表名/分层线索”，不要编造中文业务名

L3：加工步骤
- 按 scope_profile.steps 顺序总结主要加工链路
- 优先保留 join、filter、aggregate、window、case_when、union、lateral_view
- 对每个关键步骤，结合 business_profile.sections 和 business_rule_candidates
  说明“条件是什么、处理动作是什么、输出到哪里”
- 对 UNION 任务说明各分支在合并什么来源；对窗口函数说明是在排序、去重、取首/取末；
  对聚合说明指标或汇总口径；对 CASE WHEN 说明分类/状态派生含义
- 不要列出无业务意义的解析细节，但不要省略有业务作用的 filter、dedup、join、aggregate、
  window、case_when、union、lateral_view
- 每个关键步骤建议按以下格式输出：
  - 阶段名称：来自 step.name / role
  - 输入：物理源表或上游 scope
  - 条件：来自 business_rule_candidates / logic.filters / joins.on
  - 处理：filter、join、dedup、aggregate、window、case_when、union 等
  - 输出/作用：本阶段产出了什么中间结果或为下游补充什么字段
  - 业务动作翻译：把 role 翻译成自然语言业务动作，例如“构造超额放款合同唯一名单”

L3.5：业务规则/判断逻辑
- 必须单独列出从 business_rule_candidates 和 filters_summary 归纳出的规则组
- 每个规则组包含：规则名称、涉及字段、字段中文语义、规则作用
- 不要只写“按过滤条件筛选”，要解释筛选条件在业务上可能表示什么
- 对复杂 OR/AND 条件，要尽量拆成多个业务规则组，例如：
  准入条件、排除条件、时间窗口、状态判断、金额阈值、名单命中、去重取最新、渠道合并
- 如果 fields_truncated 或 expression_omitted=true，要说明该规则在 profile 中已瘦身，
  只能还原主要字段，完整条件应查看 lineage.json

L4：核心字段/指标
- 根据 important_columns 和 end_to_end_lineage 分组：
  标识字段、时间/分区字段、分类字段、指标字段、重要派生字段
- business_rule_candidates 中出现的字段通常是业务判断字段，也要纳入核心字段说明
- 如果 related_metadata 中有字段 comment，必须用字段中文注释解释字段语义
- 对 CASE WHEN、聚合、窗口函数派生字段说明其来源和用途边界
- 对每类字段说明“为什么核心”，不能只罗列字段名
- 详细还原模式下，至少覆盖：
  - 候选粒度字段 grain.keys
  - important_columns 中的字段
  - business_rule_candidates 中的判断字段
  - output lineage 中 transform 不是 DIRECT/CONSTANT 的派生字段
  - expression_catalog 中出现的 CASE/聚合/窗口/表达式字段

L5：血缘可信度和风险边界
- 统计 trace_complete=true/false 的输出字段数量
- 列出 trace_complete=false 的字段和原因
- 说明 schema 缺失、SELECT *、metadata_complete=false、截断等边界
- 明确哪些风险影响精确字段追溯，哪些只是提示信息
- 明确 diagnostics.warning_types 的含义，例如 magic_number、filter_in_join_on_clause、
  duplicate_table_in_union、star_not_expanded 等
- 如果 compact_policy 显示 large_profile_compaction、expression_omitted、
  sections_truncated、fields_truncated 等，要说明 profile 已瘦身，完整细节看 lineage.json
- 对 trace_complete=true 的字段，不能再声称“绝对正确”，只能写“从当前解析结果看可完整追溯”

额外要求：
- 不要把 grain.keys 称为主键，只能称为候选输出标识字段。
- 如果没有 trace_complete=false，也要写“本 profile 中 ROOT 输出字段均可完整追溯”，
  但若存在 schema 或 star warning，需要说明仍有中间 scope/schema 边界。
- 输出必须是 Markdown。
- 不要输出 JSON。
- 详细还原模式下，优先完整性而不是简短性；除非用户要求摘要，否则不要压缩成几句话。

profile.json:
```json
{{PROFILE_JSON}}
```
```

## 自检清单

生成结果后，LLM 应自检：

- 是否包含 L1-L5 五个章节；
- 是否准确写出 `task_name` 和 `target_table`；
- 是否没有把 `grain.keys` 写成主键；
- 是否说明 `grain.keys` 是候选输出标识字段；
- 是否使用 business_profile.objective、business_profile.sections 和 business_rule_candidates
  归纳业务目标、组成部分和判断规则；
- 是否没有把 business_profile.objective.summary 原样当最终业务结论；
- 是否把 role 翻译成业务动作，而不是只输出 dedup/filter/join 等技术标签；
- 是否使用 expression_catalog / filters_summary 补充派生字段和过滤规则；
- 是否统计了 `trace_complete=true/false`；
- 如果存在 `trace_complete=false`，是否列出字段和原因；
- 如果存在 `star_not_expanded`、`unresolved_unqualified_no_schema`
  或 `metadata_complete=false`，是否说明 schema/星号边界；
- 如果存在 `table_metadata` 或字段 comment，是否用于解释表和字段的业务语义；
- 是否避免把 `magic_number` 当成血缘错误；
- 是否说明 compact_policy 中的截断/省略边界；
- 是否没有编造 profile 中不存在的业务指标、业务口径或主键约束。

## 推荐输出骨架

```markdown
# SQL 任务画像：{task_name}

## L1：任务概览

...

## L2：输入输出

...

## L3：加工步骤

...

## L3.5：业务规则/判断逻辑

...

## L4：核心字段/指标

...

## L5：血缘可信度和风险边界

...
```

## 验证方式

可以使用：

```bash
python tools/validate_llm_profile_doc.py \
  --profile /path/to/profile.json \
  --doc /path/to/generated.md \
  --json /tmp/profile_doc_validation.json
```

验证器只做结构和事实边界检查，不判断文字是否优美。

## 其它 Agent 能力要求

要稳定生成同等质量的任务画像，Agent 至少需要具备以下能力：

1. 上下文窗口  
   - 摘要模式：建议可读入 25k token 以上上下文，适合单个 80KB 以内 profile。
   - 详细还原模式：建议 64k token 以上更稳；如果 profile 接近 80KB 且要求长文输出，
     128k token 以上更合适。
   - 如果上下文不足，应先读取 `summary/business_profile/business_rule_candidates/related_metadata`
     生成摘要，再按需读取 `end_to_end_lineage/expression_catalog/diagnostics` 补充细节。

2. 结构化 JSON 阅读能力  
   Agent 必须能按 read_order 读取 JSON，不应随机挑字段。尤其要能关联：
   `business_profile.sections`、`business_rule_candidates`、`scope_profile.steps`、
   `related_metadata`、`important_columns`、`end_to_end_lineage`。

3. SQL/数仓语义能力  
   Agent 需要理解 filter、join、dedup、aggregate、window、case_when、union、lateral_view
   在数仓 SQL 中的业务含义，并能把技术动作翻译成“名单构造、状态判断、时间窗口、指标汇总、
   分支合并、取最新记录”等业务语言。

4. 元数据利用能力  
   Agent 必须优先使用表中文名、表描述、字段注释，不能只看英文表名/字段名。
   如果中文元数据缺失，要明确说明语义来自表名或字段 token 推断。

5. 可信度边界意识  
   Agent 必须区分事实、推断和风险边界。不能把候选 key 写成主键，不能把 magic_number
   写成血缘错误，不能忽略 trace_complete=false、metadata 截断、expression_omitted 等边界。

6. 输出控制能力  
   Agent 要能根据模式控制详略：
   - 摘要模式：少量章节、快速定位、适合知识库卡片；
   - 详细还原模式：完整阶段、规则组、字段解释、风险边界，适合交接和审查。
