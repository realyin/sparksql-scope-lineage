# 任务理解工作台设计方案

## 1. 背景和目标

当前项目已经可以从 SQL 生成多类产物：

- `lineage.json`：完整机器血缘，包含 scope、字段、图结构和 diagnostics；
- `profile.json`：面向 LLM/agent 的任务画像结构，包含任务摘要、scope 加工链路、业务规则候选、核心字段、端到端血缘和元数据；
- `report.html`：离线血缘可视化报告，包含 scope DAG、ROOT 字段表、字段级血缘图；
- LLM 生成的画像 Markdown：面向人阅读的任务分级结构说明。

这些信息目前是分散的。用户需要在多个文件之间切换，才能回答：

- 业务画像里提到的加工阶段，对应 SQL 中哪个 scope？
- 某条业务规则由哪个 WHERE/JOIN/窗口函数实现？
- 某个输出字段的血缘经过哪些 scope？
- 某个表或字段的中文语义是什么？
- 哪些业务结论是解析事实，哪些是 LLM 推断，哪些来自人工知识沉淀？
- 哪些地方因为 schema 缺失、profile 瘦身或静态解析限制，需要回看完整 `lineage.json`？

因此下一阶段目标是建设一个 **任务理解工作台**：

```text
SQL + schema + metadata
        ↓
lineage.json / profile.json / diagnostics.json
        ↓
task_insight.json
        ↓
task_insight.html
```

`task_insight.html` 不是单纯更大的血缘图，而是一个将业务画像、scope 血缘图、字段级血缘图、
规则、元数据和风险边界有机连接的离线交互页面。

## 2. 设计原则

### 2.1 血缘是底座，业务理解是目标

工作台的核心价值不是只展示“字段从哪里来”，而是帮助人和 agent 理解：

- 任务生产什么业务对象；
- SQL 被拆成哪些业务阶段；
- 每个阶段的筛选、关联、去重、聚合、窗口和 CASE 逻辑是什么；
- 核心字段和指标在业务规则中承担什么作用；
- 业务说明是否有证据链支撑。

### 2.2 HTML 不直接绑定 profile.json 当前结构

`profile.json` 会继续演进：可能增删字段、调整瘦身策略、合并结构、引入更多业务知识。

因此页面不应直接依赖：

```js
profile.business_profile.sections
profile.business_rule_candidates
profile.scope_profile.steps
```

而应依赖一个更稳定的页面协议：

```text
task_insight.json
```

`task_insight.json` 由 adapter 从 `lineage.json`、`profile.json`、`diagnostics.json`、
可选的画像 Markdown 索引和业务知识库中归一化生成。

### 2.3 对象和关系优先

页面联动的基础不是 Markdown 文本，而是结构化对象和关系：

- scope
- output column
- physical table
- physical column
- rule
- business section
- diagnostic
- knowledge item

以及它们之间的关系：

- scope produces column
- rule implemented_by scope
- rule uses field
- column derived_from table column
- section references scope/rule/column
- diagnostic affects scope/column
- knowledge enriches table/column/rule/task

### 2.4 所有结论都要有 evidence

工作台必须区分三类信息：

| 类型 | 来源 | 可信含义 |
| --- | --- | --- |
| 解析事实 | `lineage.json` / `profile.json` / `diagnostics.json` | 静态解析得到的结构化事实 |
| 模型推断 | LLM 画像 Markdown 或结构化索引 | 基于 profile 的业务解释，需要证据校验 |
| 人工知识 | `business_knowledge.json` 或知识库服务 | 可沉淀、可复用、可信度更高的业务定义 |

每个业务结论、规则、字段解释都尽量带 `evidence`，指向来源文件和 JSON path。

### 2.5 缺失信息要优雅降级

页面必须能处理信息缺失：

- 没有 LLM 画像：使用 `profile.json` 自动生成业务说明区；
- 没有业务知识：隐藏知识增强或标注“未接入业务知识”；
- 没有字段中文名：显示英文名，并标注“中文名缺失”；
- profile 被瘦身：显示截断/省略边界，并提示查看 `lineage.json`；
- 字段血缘不完整：显示 `trace_complete=false` 和原因；
- 某些规则没有完整表达式：显示条件摘要和边界。

## 3. 目标产物

第一阶段新增两个产物：

```text
task_insight.json
task_insight.html
```

完整输出目录示例：

```text
task_output/
  lineage.json
  profile.json
  diagnostics.json
  report.html
  task_insight.json
  task_insight.html
  lineage.md
  views/
```

后续可选接入：

```text
business_profile.md
business_profile.index.json
business_knowledge.json
```

## 4. task_insight.json 设计

### 4.1 顶层结构

```json
{
  "schema_version": "1.0",
  "task": {},
  "objects": {
    "scopes": {},
    "columns": {},
    "tables": {},
    "rules": {},
    "sections": {},
    "diagnostics": {},
    "knowledge": {}
  },
  "links": [],
  "sources": {},
  "capabilities": {},
  "warnings": []
}
```

### 4.2 task

```json
{
  "task": {
    "task_id": "bj_bas_mxg_ca_customer_inbound_log_d_df",
    "task_name": "bj_bas_mxg_ca_customer_inbound_log_d_df",
    "target_table": "hw_jhy_iceberg.report_csc_ana.bj_bas_mxg_ca_customer_inbound_log_d_df",
    "target_table_label": "客户提现后触达宽表",
    "stmt_kind": "INSERT_OVERWRITE",
    "summary": "生成客户提现完成后的首次触达宽表",
    "input_table_count": 6,
    "output_column_count": 56,
    "trace_complete_count": 56,
    "trace_incomplete_count": 0,
    "risk_level": "YELLOW"
  }
}
```

`target_table_label` 优先来自：

1. 业务知识；
2. `related_metadata.output_tables[].table_metadata.table_name_cn`；
3. `profile.business_profile.objective.target_table_label`；
4. 英文表名。

### 4.3 objects.scopes

scope 对象来自 `lineage.json.scopes` 和 `profile.json.scope_profile.steps`。

```json
{
  "objects": {
    "scopes": {
      "scope:t2": {
        "id": "scope:t2",
        "scope_id": "cte:t2",
        "name": "t2",
        "kind": "cte",
        "role": "dedup",
        "business_action": "按客户和申请主体取最新贷款、合同和完成时间",
        "summary": "读取提现审批表；关联上游；使用窗口函数取最新；去重",
        "direct_inputs": ["scope:t1", "table:hw_jhy_iceberg.dwd.dwd_ap_wdraw_aprv_det_df"],
        "physical_source_tables": ["table:hw_jhy_iceberg.dwd.dwd_ap_wdraw_aprv_det_df"],
        "output_column_count": 6,
        "logic": {
          "joins": [],
          "filters": [],
          "window_functions": [],
          "case_when": [],
          "aggregations": [],
          "distinct": true,
          "union_branches": 0,
          "lateral_views": []
        },
        "evidence": [
          {"source": "profile", "path": "$.scope_profile.steps[2]"},
          {"source": "lineage", "path": "$.scopes['cte:t2']"}
        ]
      }
    }
  }
}
```

### 4.4 objects.rules

规则对象来自：

- `profile.business_rule_candidates`
- `profile.filters_summary`
- `scope_profile.steps[].logic.joins`
- `scope_profile.steps[].logic.window_functions`
- `scope_profile.steps[].logic.case_when`
- 后续 LLM 画像索引
- 后续业务知识库

```json
{
  "id": "rule:root:first_touch_after_complete",
  "type": "rule",
  "title": "提现完成后首次触达",
  "rule_kind": "time_window",
  "scope_ids": ["scope:robot_first_detail", "scope:manual_first_detail"],
  "condition_summary": "触达时间 >= final_complete_dt，并按触达时间升序取 FIRST_VALUE",
  "condition_expression": "unix_timestamp(b.xma_begin_time) >= unix_timestamp(t2.final_complete_dt)",
  "fields": [
    {
      "field_id": "column:final_complete_dt",
      "role": "time_window_start",
      "meaning": "提现最终完成时间，作为触达时间下界"
    },
    {
      "field_id": "column:xma_begin_time",
      "role": "event_time",
      "meaning": "触达开始时间，用于判断和排序"
    }
  ],
  "result": "生成各渠道首次触达时间和会话 ID",
  "confidence": "derived",
  "evidence": [
    {"source": "profile", "path": "$.scope_profile.steps[12].logic.joins[0]"}
  ]
}
```

### 4.5 objects.columns

字段对象分为输出字段和物理字段。第一版可以优先建 ROOT 输出字段。

```json
{
  "id": "column:first_robot_time",
  "type": "output_column",
  "name": "first_robot_time",
  "label": "首次机器人触达时间",
  "transform": "DIRECT",
  "trace_complete": true,
  "expression": "`b`.`xma_begin_time`",
  "semantic_role": "event_time",
  "source_columns": [
    "physical_column:report_csc_ana.mxg_online_base_info_di.xma_begin_time"
  ],
  "scope_ids": ["scope:robot_first_detail", "scope:ROOT"],
  "rule_ids": ["rule:root:first_touch_after_complete"],
  "evidence": [
    {"source": "profile", "path": "$.end_to_end_lineage[5]"}
  ]
}
```

字段中文名优先级：

1. 业务知识；
2. `related_metadata` 字段 comment；
3. LLM 画像索引；
4. 字段名启发式。

### 4.6 objects.tables

```json
{
  "id": "table:hw_jhy_iceberg.dwd.dwd_ap_wdraw_aprv_det_df",
  "type": "table",
  "name": "hw_jhy_iceberg.dwd.dwd_ap_wdraw_aprv_det_df",
  "label": "提现审批表",
  "description": "提现审批表",
  "layer": "DWD",
  "role": "input",
  "used_columns": ["unique_id", "app_code", "final_complete_dt"],
  "metadata_complete": true,
  "evidence": [
    {"source": "profile", "path": "$.related_metadata.input_tables['hw_jhy_iceberg.dwd.dwd_ap_wdraw_aprv_det_df']"}
  ]
}
```

### 4.7 objects.sections

section 是页面左侧业务说明的结构化段落。第一版从 `profile.business_profile.sections`
和 `scope_profile.steps` 自动生成；后续可以由 LLM 画像索引覆盖或增强。

```json
{
  "id": "section:L3:first_robot_touch",
  "title": "取首次机器人触达",
  "level": "L3",
  "body": "把统一机器人触达明细与最新贷款基准关联，限定触达时间晚于提现完成时间，再取首次触达。",
  "scope_ids": ["scope:robot_first_detail", "scope:robot_first_detail_ivr"],
  "rule_ids": ["rule:root:first_touch_after_complete"],
  "column_ids": ["column:first_robot_time", "column:first_robot_session_id"],
  "source": "derived_from_profile"
}
```

### 4.8 objects.diagnostics

```json
{
  "id": "diagnostic:filter_in_join_on_clause:robot_first_detail",
  "type": "diagnostic",
  "severity": "warning",
  "code": "filter_in_join_on_clause",
  "message": "JOIN ON 中包含时间窗口过滤条件",
  "scope_ids": ["scope:robot_first_detail"],
  "column_ids": [],
  "meaning": "不一定是错误，但说明 JOIN 同时承担关联和业务过滤作用"
}
```

### 4.9 objects.knowledge

业务知识可以后续接入，第一版预留结构。

```json
{
  "id": "knowledge:rule:first_touch_after_complete",
  "type": "knowledge",
  "target_id": "rule:root:first_touch_after_complete",
  "title": "提现后首次触达",
  "definition": "以最终完成时间为起点，取之后最早发生的客服触达。",
  "source": "business_knowledge",
  "confidence": "curated"
}
```

### 4.10 links

所有联动依赖 `links`：

```json
[
  {"from": "scope:t2", "to": "column:current_newest_loan_no", "type": "produces"},
  {"from": "rule:root:first_touch_after_complete", "to": "scope:robot_first_detail", "type": "implemented_by"},
  {"from": "rule:root:first_touch_after_complete", "to": "column:final_complete_dt", "type": "uses_field"},
  {"from": "column:first_robot_time", "to": "table:report_csc_ana.mxg_online_base_info_di", "type": "derived_from"},
  {"from": "section:L3:first_robot_touch", "to": "scope:robot_first_detail", "type": "references"}
]
```

前端不需要知道这些关系来自哪个源，只要根据 `links` 做高亮和过滤。

### 4.11 sources

```json
{
  "sources": {
    "lineage": {
      "path": "lineage.json",
      "schema_version": "lineage-v1"
    },
    "profile": {
      "path": "profile.json",
      "schema_version": "profile-v1",
      "compact_policy": {}
    },
    "diagnostics": {
      "path": "diagnostics.json"
    },
    "business_doc": {
      "path": "business_profile.md",
      "available": false
    },
    "business_knowledge": {
      "path": "business_knowledge.json",
      "available": false
    }
  }
}
```

## 5. task_insight.html 页面设计

### 5.1 页面布局

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ 顶部任务概览：任务名 / 目标表 / 输入输出统计 / trace_complete / 风险       │
├──────────────────────┬────────────────────────────┬────────────────────────┤
│ 左侧：业务画像        │ 中间：Scope DAG             │ 右侧：详情 / 证据链     │
│ - L1 任务概览         │ - scope 节点                │ - scope 详情            │
│ - L2 输入输出         │ - 物理表节点                │ - 规则详情              │
│ - L3 加工步骤         │ - 高亮选中路径              │ - 字段详情              │
│ - L3.5 业务规则       │                            │ - evidence              │
├──────────────────────┴────────────────────────────┴────────────────────────┤
│ 底部：ROOT 字段表 + 字段级血缘图                                           │
└────────────────────────────────────────────────────────────────────────────┘
```

页面保持离线自包含，不依赖 CDN，不请求远程资源。

### 5.2 顶部任务概览

显示：

- `task_name`
- `target_table`
- 目标表中文名/业务标签
- 输入表数量
- 输出字段数量
- scope 数量
- trace complete / incomplete
- warning 数量
- profile 是否瘦身

### 5.3 左侧业务画像

第一版由 `task_insight.objects.sections` 渲染，而不是直接渲染 Markdown。

每个 section 需要：

- 标题；
- 正文摘要；
- 关联 scope；
- 关联规则；
- 关联字段；
- 来源标签：解析事实 / LLM 推断 / 人工知识。

点击 section：

- 中间 Scope DAG 高亮相关 scope；
- 底部字段表筛选/高亮相关字段；
- 右侧详情展示 section 的 evidence。

### 5.4 Scope DAG

基于 `lineage.scope_graph` 或 `task_insight.links` 渲染。

节点类型：

- ROOT
- CTE
- subquery
- union
- physical table

节点颜色建议：

- ROOT：深色强调；
- CTE/subquery：中性色；
- union：蓝色；
- physical table：绿色；
- warning scope：黄色边框；
- trace incomplete 相关 scope：红色边框。

点击 scope：

- 左侧滚动到相关 section；
- 右侧显示 scope 的 filters / joins / windows / case_when / output columns；
- 底部字段血缘高亮经过该 scope 的字段。

### 5.5 规则面板

规则是业务理解的核心。页面需要专门展示：

- 规则名称；
- 所在 scope；
- 条件表达式或摘要；
- 涉及字段；
- 字段中文含义；
- 字段在规则中的作用；
- 处理结果；
- evidence。

点击规则：

- 高亮实现它的 scope；
- 高亮涉及字段；
- 右侧展示完整证据。

### 5.6 字段级血缘

底部包含：

- ROOT 字段表；
- 字段搜索；
- 风险过滤；
- transform 过滤；
- 字段级上游图。

点击输出字段：

- 高亮字段经过的 scope；
- 展示物理来源表字段；
- 展示字段中文注释；
- 展示参与的规则；
- 展示 `trace_complete` 和 incomplete reasons。

### 5.7 详情和证据链

右侧详情面板根据当前选中对象变化：

- 选中 scope：显示该 scope 的输入、输出、逻辑和规则；
- 选中 rule：显示条件、字段作用、处理结果和 evidence；
- 选中 column：显示字段语义、表达式、血缘、相关规则；
- 选中 table：显示表中文名、描述、使用字段；
- 选中 diagnostic：显示风险含义和影响范围。

每个面板底部显示证据：

```text
Evidence
- profile: $.scope_profile.steps[12].logic.joins[0]
- lineage: $.scopes["cte:robot_first_detail"]
```

## 6. 兼容未来变化

### 6.1 profile.json 变化

`profile.json` 后续可能：

- 增加 `profile_schema_version`；
- 合并或删除某些摘要字段；
- 调整 `business_profile`；
- 增加新的业务规则结构；
- 为了瘦身省略更多表达式；
- 接入更多表字段语义。

应对策略：

1. `task_insight.json` 定为页面稳定协议；
2. 编写 `profile -> insight` adapter；
3. adapter 支持 profile 版本分支；
4. 页面只读 `task_insight.json`；
5. 缺失字段时使用 fallback，不报错。

### 6.2 LLM 画像变化

LLM 画像 Markdown 是人读文档，不适合作为联动主数据源。

长期建议让 LLM 输出两份文件：

```text
business_profile.md
business_profile.index.json
```

`business_profile.index.json` 只负责结构化锚点：

```json
{
  "sections": [
    {
      "id": "doc:L3:first_robot_touch",
      "title": "取首次机器人触达",
      "scope_ids": ["scope:robot_first_detail"],
      "rule_ids": ["rule:first_touch_after_complete"],
      "column_ids": ["column:first_robot_time"],
      "markdown_anchor": "取首次机器人触达"
    }
  ]
}
```

这样 Markdown 文案可以变化，但联动锚点稳定。

### 6.3 业务知识沉淀

业务知识不建议直接塞进 `profile.json`，否则 profile 会变重，也会混淆“解析事实”和“人工知识”。

建议独立接入：

```text
business_knowledge.json
```

结构按实体组织：

```json
{
  "tasks": {},
  "tables": {},
  "columns": {},
  "rules": {},
  "domains": {}
}
```

页面展示时明确标注来源：

- 解析事实；
- LLM 推断；
- 人工知识。

### 6.4 多版本和缺失能力

`task_insight.json.capabilities` 用来告诉页面当前有哪些能力：

```json
{
  "capabilities": {
    "has_business_doc": false,
    "has_business_knowledge": false,
    "has_complete_lineage": true,
    "has_scope_graph": true,
    "has_field_lineage": true,
    "has_rule_index": true,
    "has_diagnostics": true
  }
}
```

页面根据能力开关渲染，不因为某个输入缺失而失败。

## 7. CLI 和 API 设计

### 7.1 CLI

单文件解析新增：

```bash
scope-lineage parse \
  --sql-file task.sql \
  --schema schema_info.csv \
  --table-metadata tables_metadata.csv \
  --out /tmp/out \
  --insight
```

可选增强：

```bash
scope-lineage parse \
  --sql-file task.sql \
  --out /tmp/out \
  --insight \
  --business-md business_profile.md \
  --business-index business_profile.index.json \
  --business-knowledge business_knowledge.json
```

批量解析新增：

```bash
python3 tools/run_scope_corpus.py \
  --input-dir task_info \
  --out /tmp/out \
  --schema schema_info.csv \
  --table-metadata tables_metadata.csv \
  --insight
```

### 7.2 Python API

建议新增：

```python
from lineage_parser import build_task_insight, render_task_insight_html, write_task_insight_report

insight = build_task_insight(
    lineage=lineage_dict,
    profile=profile_dict,
    diagnostics=diagnostics_dict,
    business_doc=business_doc_text,
    business_doc_index=business_doc_index,
    business_knowledge=business_knowledge,
)

write_task_insight_report(insight, output_dir)
```

也可以提供从目录读取的便捷方法：

```python
write_task_insight_report_from_dir(
    "/tmp/out/task_a",
    business_doc="business_profile.md",
    business_doc_index="business_profile.index.json",
    business_knowledge="business_knowledge.json",
)
```

## 8. 模块设计

建议新增模块：

```text
lineage_parser/
  insight_model.py
  insight_builder.py
  insight_report.py
```

### 8.1 insight_model.py

职责：

- 定义 insight 对象 ID 规则；
- 定义标准对象类型；
- 定义 link 类型；
- 提供基础校验。

### 8.2 insight_builder.py

职责：

- 读取 `lineage/profile/diagnostics`；
- 从 profile 生成 task/scopes/rules/sections/tables/columns；
- 从 lineage 补全 scope 图和字段路径；
- 从 diagnostics 生成风险对象；
- 从业务知识补充 label/definition；
- 输出 `task_insight.json`。

### 8.3 insight_report.py

职责：

- 读取 `task_insight.json`；
- 渲染离线自包含 `task_insight.html`；
- 管理 CSS/JS 模板；
- 实现联动交互。

## 9. 第一版 MVP 范围

### 9.1 必做

- 生成 `task_insight.json`；
- 生成 `task_insight.html`；
- 顶部任务概览；
- 左侧业务阶段列表；
- 中间 Scope DAG；
- 右侧详情/证据链；
- 底部 ROOT 字段表；
- 点击 scope 联动 section、规则、字段；
- 点击字段展示字段血缘和相关规则；
- 点击规则高亮相关 scope 和字段；
- 展示表/字段中文元数据；
- 展示 diagnostics 和 compact_policy 风险边界；
- CLI 支持 `--insight`；
- 批量工具支持 `--insight`。

### 9.2 暂不做

- 不做在线服务；
- 不做跨任务全局门户；
- 不要求第一版解析 LLM Markdown；
- 不强依赖业务知识库；
- 不做复杂图布局算法替换，先复用现有 HTML report 的 SVG 布局思路；
- 不做编辑和回写。

## 10. 分阶段实施计划

### 阶段 1：Insight 数据模型

目标：生成 `task_insight.json`。

任务：

- 新增 `insight_model.py`；
- 新增 `insight_builder.py`；
- 从 `profile.json` 抽取 task/scopes/rules/sections/tables/columns；
- 从 `lineage.json` 补充 scope graph 和字段血缘；
- 从 `diagnostics.json` 生成 diagnostic objects；
- 生成 links；
- 增加单元测试。

验收：

- 对简单 SQL 能生成完整 `task_insight.json`；
- 对最大 profile 能生成 insight 且不丢 scope/field/rule 关系；
- insight 中每个对象 ID 稳定；
- links 能支持 scope、field、rule 的双向查询。

### 阶段 2：离线 HTML 工作台

目标：生成第一版 `task_insight.html`。

任务：

- 新增 `insight_report.py`；
- 实现顶部概览；
- 实现业务 section 列表；
- 实现 Scope DAG；
- 实现字段表；
- 实现字段级血缘图；
- 实现详情面板；
- 实现点击联动和高亮。

验收：

- `task_insight.html` 可离线打开；
- 点击 scope 能定位业务阶段并显示详情；
- 点击字段能显示上游路径；
- 点击规则能高亮相关 scope/字段；
- 大任务页面不空白、不明显卡顿。

### 阶段 3：CLI 和批量集成

目标：正式输出 `task_insight.json/html`。

任务：

- `scope-lineage parse` 增加 `--insight`；
- `tools/run_scope_corpus.py` 增加 `--insight`；
- README 和中文 README 更新；
- 增加 examples；
- 增加回归测试。

验收：

- 单任务 CLI 输出包含 `task_insight.json/html`；
- 批量任务可选输出 insight；
- 现有 `--html`、`--md` 行为不受影响。

### 阶段 4：LLM 画像和业务知识接入

目标：让工作台接入更丰富的业务语义。

任务：

- 支持读取 `business_profile.md`；
- 支持读取 `business_profile.index.json`；
- 支持读取 `business_knowledge.json`；
- 实现知识覆盖优先级；
- 在页面标注“解析事实/LLM 推断/人工知识”。

验收：

- 有业务知识时，表/字段/规则显示人工定义；
- 无业务知识时，页面仍正常；
- Markdown 改写不影响联动，只要 index 稳定。

## 11. 测试策略

### 11.1 单元测试

- insight object ID 生成；
- profile adapter；
- lineage adapter；
- links 构造；
- diagnostics 映射；
- 缺失字段 fallback；
- compact_policy 边界标记。

### 11.2 快照测试

对固定 SQL 生成 `task_insight.json`，做结构快照：

- scope 数量；
- rule 数量；
- column 数量；
- links 数量；
- 核心 ID 是否存在。

### 11.3 HTML smoke test

用 Playwright 或轻量 HTML 检查：

- 页面能加载；
- 顶部 task title 存在；
- Scope DAG 非空；
- 字段表非空；
- 点击 scope 后详情面板更新；
- 点击字段后字段图更新。

### 11.4 大任务验证

至少用以下任务验证：

- `bj_bas_mxg_ca_customer_inbound_log_d_df`：大 profile、复杂 union/window/join；
- `clct_file_in_collect_loan`：强业务筛选规则；
- 含 `lateral view posexplode` 的热线宽表任务；
- 含 `SELECT *` 的 schema 展开任务。

## 12. 风险和应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| profile 结构变化 | 页面失效 | 页面只读 `task_insight.json`，由 adapter 兼容 profile 版本 |
| LLM Markdown 不稳定 | 联动不稳定 | Markdown 只做人读，联动依赖 `business_profile.index.json` |
| 业务知识不断增加 | profile 过大 | 业务知识独立文件或服务接入，不塞进 profile |
| 大任务图太复杂 | 页面卡顿 | 默认折叠低价值节点，支持搜索/过滤/聚焦 |
| 规则表达式被瘦身 | 业务说明不完整 | 显示 `expression_omitted`，提示查看 `lineage.json` |
| 字段中文名缺失 | 语义不足 | 显示英文名和推断边界，后续由业务知识补齐 |
| 静态解析有边界 | 误导用户 | 所有风险进入 diagnostics/证据链，不把推断写成事实 |

## 13. 开发优先级建议

优先级从高到低：

1. `task_insight.json` 稳定模型；
2. scope/field/rule/table/diagnostic 对象索引；
3. links 双向关系；
4. 基础 `task_insight.html`；
5. scope 点击联动；
6. 字段点击联动；
7. 规则点击联动；
8. 证据链展示；
9. CLI `--insight`；
10. LLM 画像 index；
11. 业务知识接入；
12. 跨任务门户。

## 14. 结论

任务理解工作台是可行的，而且应该作为项目下一阶段主线。

关键不是把现有 `lineage.json`、`profile.json`、Markdown 和 HTML 简单拼在一起，
而是先建立稳定的 `task_insight.json` 中间语义模型。这个模型把 scope、字段、规则、
表、诊断和业务知识都抽象成对象和关系，页面只依赖这层稳定协议。

这样即使未来 `profile.json` 调整、LLM 画像格式变化、业务知识不断沉淀，工作台也只需要
更新 adapter，不需要重写页面和交互逻辑。
