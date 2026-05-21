# profile.json 业务语义还原设计方案

## 1. 背景

本项目的目标不是只输出 SQL 血缘，而是帮助人、LLM 和 agent 理解一段 SQL 任务：

- 任务整体在业务上做什么；
- 输入表和输出表分别代表什么；
- SQL 被拆成哪些 scope/模块；
- 每个 scope 做了什么筛选、关联、聚合、窗口、CASE、UNION；
- 最终字段从哪里来，表达式是什么；
- 复杂业务规则如何从 SQL 条件中还原；
- 哪些结论是解析事实，哪些是基于元数据或字段名的语义推断；
- 哪些地方因为 schema 缺失、`SELECT *`、UDF、动态 SQL 或解析边界而不确定。

现有 `profile.json` 已经具备任务摘要、`scope_profile.steps`、`business_rule_candidates`、
`important_columns`、`end_to_end_lineage` 和 `related_metadata` 等结构，可以支持基础任务画像。
但近期在复杂任务上验证发现：

- 复杂 WHERE 经常被压缩成“涉及哪些字段”，不足以还原业务逻辑；
- scope 级字段映射不足，难以解释每个模块内部到底加工了哪些字段；
- 关键字段判断偏字段名启发，缺少基于 WHERE/JOIN/WINDOW/CASE 使用场景的判断；
- 直接物理表和间接上游物理表曾经被混淆；
- 同名 scope、孤立 scope、MERGE 重复赋值等问题需要从结构和验证上预防；
- `profile.json` 如果过度瘦身，会无法生成高质量业务语义。

因此需要重新明确 `profile.json` 的定位和结构演进方向。

## 2. 核心定位

`profile.json` 是 **LLM 可读的中等详细 SQL 任务理解包**。

它不是 `lineage.json` 的轻量副本，也不是 HTML 页面协议。它的第一目标是让 LLM 在一次可读范围内，尽可能准确地还原业务语义。

产物分工如下：

```text
lineage.json
  完整机器事实层。保留所有 scope、字段、表达式、来源、图结构和 diagnostics。

profile.json
  LLM 主输入层。保留足够业务语义、关键规则、关键字段、scope 加工链路和端到端字段血缘。

task_insight.json
  页面协议层。可同时吸收 profile.json 的语义摘要和 lineage.json 的详细事实，用于联动工作台。

business_profile.md
  LLM 输出层。面向人阅读，可进入知识库或交接文档。

task_insight.html
  工作台展示层。支持人和 agent 联动审阅 scope 图、字段血缘、规则和证据链。
```

设计原则：

- `profile.json` 不能过轻，必须足以支撑业务语义还原；
- `profile.json` 不能无限膨胀，应压缩重复、长尾和纯页面信息；
- 完整细节由 `lineage.json` 承担；
- 页面细节由 `task_insight.json` 从 `profile.json + lineage.json` 归一化生成；
- 所有业务语义都要能区分解析事实、元数据事实、规则推断、LLM 推断和人工知识。

## 3. 设计目标

### 3.1 完整性

`profile.json` 至少要支持三层理解：

任务级：

- 任务名称、目标表、写入方式；
- 多 statement 任务的多个输出；
- 任务总体业务目标；
- 输入表、输出表、表中文名、表描述；
- 输出字段、表达式、物理来源；
- 整体加工链路；
- 关键业务规则；
- 风险边界。

Scope 级：

- 每个 scope 的语义；
- 直接输入 scope；
- 直接输入物理表；
- 上游可追溯物理表；
- 输出字段摘要；
- 关键字段映射；
- 过滤、关联、聚合、窗口、CASE、UNION、lateral view 等逻辑；
- SQL 证据片段。

字段级：

- 最终输出字段；
- 字段中文名/注释；
- 字段表达式；
- 来源表/来源字段；
- 加工 scope；
- 字段业务角色；
- 是否关键字段；
- `trace_complete` 和不完整原因。

### 3.2 正确性

正确性分为两层：

- 事实正确：对象数量、字段来源、scope 引用、输入输出、trace 状态必须和 `lineage.json`/SQL 一致。
- 语义正确：业务解释必须有证据；推断必须标记推断来源和置信度，不能伪装成 SQL 明确事实。

### 3.3 可落地性

设计必须能分阶段实现：

- 第一阶段保留旧字段，新增 v2 结构，不破坏现有 prompt 和 HTML；
- 第二阶段增强规则、字段和 scope 语义；
- 第三阶段让 prompt 和 `task_insight.json` 优先使用新结构；
- 第四阶段再考虑逐步弱化旧结构。

## 4. 顶层结构

建议在兼容旧结构的基础上新增 `semantic_profile`。这样可以避免一次性大改所有消费者。

```json
{
  "profile_version": "1.x",
  "task_name": "...",
  "target_table": "...",
  "summary": {},
  "scope_profile": {},
  "business_rule_candidates": [],
  "important_columns": [],
  "end_to_end_lineage": [],
  "related_metadata": {},
  "diagnostics": {},
  "compact_policy": {},

  "semantic_profile": {
    "version": "2.0",
    "task": {},
    "business_summary": {},
    "tables": {},
    "process": {},
    "rules": [],
    "fields": {},
    "quality": {}
  }
}
```

短期内保留旧结构的原因：

- 现有 prompt、测试、工作台和下游流程不需要一次性迁移；
- 可以对比旧字段和新字段的一致性；
- 全量语料回归更稳。

长期可在主要消费者迁移后，把旧结构视为 compatibility view。

## 5. semantic_profile.task

回答“这个任务是谁、写到哪里”。

```json
{
  "task": {
    "task_name": "clct_file_in_collect_loan",
    "stmt_kind": "MULTI_STATEMENT",
    "statement_count": 3,
    "source_table_count": 2,
    "target_table_count": 2,
    "target_tables": [
      {
        "table": "collection_files.clct_cf_pre_in_coll_cust",
        "table_cn": "待入催客户清单",
        "write_modes": ["INSERT_OVERWRITE", "INSERT"],
        "statement_ids": ["clct_file_in_collect_loan#0", "clct_file_in_collect_loan#1"]
      },
      {
        "table": "collection_files.loan_info_public_a_tmp",
        "table_cn": "待入催贷款明细",
        "write_modes": ["INSERT_OVERWRITE"],
        "statement_ids": ["clct_file_in_collect_loan#2"]
      }
    ]
  }
}
```

设计约束：

- 多 statement 任务必须显式建模；
- `task_name` 使用业务任务名，`statement_id` 用于区分同一任务中的不同 SQL statement；
- 输出表必须保留英文名，中文名只做辅助，避免歧义。

## 6. semantic_profile.business_summary

回答“整体上做什么”。

```json
{
  "business_summary": {
    "objective": "从全量贷款数据中筛选待入催客户，并生成待入催贷款明细",
    "main_business_object": "待入催客户/贷款",
    "process_summary": [
      "筛选满足入催条件的客户账户",
      "补充跨日状态变化进入入催范围的账户",
      "回连贷款全量表生成待入催贷款明细"
    ],
    "semantic_confidence": "medium",
    "evidence": [
      {
        "type": "parsed_sql",
        "source": "profile",
        "path": "$.semantic_profile.rules[0]"
      }
    ],
    "inference_notes": [
      "目标说明基于任务名、输出表名、过滤字段和 SQL 条件推断"
    ]
  }
}
```

设计约束：

- `objective` 可以是推断，但必须说明证据；
- 不应只复述“从 N 张表读取数据，经过 filter/join 写入目标表”；
- 业务目标优先由任务名、表中文名、字段注释、关键规则和输出表共同推断。

## 7. semantic_profile.tables

回答“数据从哪里来、到哪里去、每张表在任务中扮演什么角色”。

```json
{
  "tables": {
    "inputs": [
      {
        "table": "collection_files.clct_cf_loan_all",
        "table_cn": "贷款全量表",
        "description": "贷款、账户、逾期、还款、产品等全量信息",
        "role": "主数据来源",
        "used_for": "提供贷款状态、逾期、还款、产品和客户字段，用于判断是否进入入催范围",
        "used_columns": [
          {
            "name": "in_clct_dpd",
            "comment": "入催DPD",
            "used_in": ["filter"],
            "business_role": "判断预逾期入催窗口"
          }
        ],
        "metadata_complete": true
      }
    ],
    "outputs": [
      {
        "table": "collection_files.clct_cf_pre_in_coll_cust",
        "table_cn": "待入催客户清单",
        "business_meaning": "进入催收范围的客户与账户清单",
        "columns": [
          {
            "name": "internal_customer_id",
            "comment": "客户ID",
            "business_role": "标识待入催客户"
          }
        ]
      }
    ]
  }
}
```

设计约束：

- 输入表和输出表都必须保留英文表名；
- 表中文名/描述来自 `related_metadata` 或业务知识；
- 若无中文名，不编造，降级展示英文表名；
- `used_for` 可以是推断，但要基于字段使用场景和规则证据。

## 8. semantic_profile.process

回答“SQL 被拆成哪些模块，每个模块做什么”。

```json
{
  "process": {
    "step_count": 3,
    "steps": [
      {
        "step_no": 1,
        "statement_id": "clct_file_in_collect_loan#0",
        "scope_id": "ROOT",
        "scope_name": "ROOT",
        "kind": "root",
        "role": "filter_join",
        "semantic_role": "筛选待入催客户",
        "business_object": "待入催客户清单",
        "direct_inputs": ["table:collection_files.clct_cf_loan_all", "scope:b"],
        "direct_source_tables": ["collection_files.clct_cf_loan_all"],
        "upstream_physical_tables": [
          "collection_files.clct_cf_loan_all",
          "collection_files.dim_excess_contra"
        ],
        "outputs": {
          "column_count": 2,
          "key_columns": [
            {
              "name": "internal_customer_id",
              "expression": "a.internal_customer_id",
              "meaning": "客户ID"
            },
            {
              "name": "acct_nbr",
              "expression": "a.acct_nbr",
              "meaning": "账户号"
            }
          ]
        },
        "logic": {
          "filters": ["rule:in_collect_filter"],
          "joins": ["rule:excess_contra_join"],
          "aggregations": [],
          "window_functions": [],
          "case_when": [],
          "distinct": false,
          "union": null,
          "lateral_views": []
        },
        "key_fields": [
          {
            "field": "product_cd",
            "used_in": ["filter"],
            "business_role": "产品纳入/排除判断"
          }
        ],
        "sql_evidence": [
          {
            "type": "where",
            "fragment": "where dt = '20260426' and ...",
            "truncated": true
          }
        ]
      }
    ]
  }
}
```

设计约束：

- 内部引用必须使用稳定 `scope_id`，不能只用 display name；
- 同名 scope 必须保留完整 scope id；
- `direct_source_tables` 只能表示当前 scope 直接读取的物理表；
- `upstream_physical_tables` 表示沿血缘追溯到的物理表；
- 文案中只有 `direct_source_tables` 非空时才能说“读取某物理表”；
- `role` 是技术处理角色，`semantic_role` 是业务语义角色；
- `semantic_role` 可推断，但要由 rules/tables/fields 证据支撑；
- 孤立或实现细节 scope 不删除，应进入 `quality.dangling_scopes` 或 `visibility`。

## 9. semantic_profile.rules

回答“关键业务规则是什么”。这是业务语义还原的核心。

```json
{
  "rules": [
    {
      "rule_id": "rule:in_collect_filter",
      "statement_id": "clct_file_in_collect_loan#0",
      "scope_id": "ROOT",
      "source": "WHERE",
      "rule_type": "business_filter",
      "business_name": "入催筛选规则",
      "summary": "保留满足预逾期、已逾期、宽限期、强制还款、账单延期、有还款金额或超额合同等条件的账户",
      "condition_groups": [
        {
          "group_id": "rule:in_collect_filter:g01",
          "name": "产品纳入/排除",
          "expression": "substr(product_cd, 3, 1) != '2' and product_cd not in ('005800','005605','005502')",
          "fields": ["product_cd"],
          "operators": ["SUBSTRING", "NOT_IN", "!="],
          "meaning_hint": "排除部分产品类型，同时保留特殊纳入产品",
          "evidence_type": "parsed_sql",
          "sql_fragment": "and ((substr(product_cd, 3, 1) != '2' ...",
          "truncated": true
        },
        {
          "group_id": "rule:in_collect_filter:g02",
          "name": "预逾期入催窗口",
          "expression": "in_clct_dpd >= -7 and in_clct_dpd <= 0",
          "fields": ["in_clct_dpd"],
          "operators": [">=", "<="],
          "meaning_hint": "提前7天到当天的预逾期窗口",
          "evidence_type": "parsed_sql"
        },
        {
          "group_id": "rule:in_collect_filter:g03",
          "name": "强制还款/结清标记",
          "expression": "forced_pay_off = 'Y'",
          "fields": ["forced_pay_off"],
          "operators": ["="],
          "meaning_hint": "命中强制还款或结清相关标记",
          "evidence_type": "parsed_sql"
        }
      ],
      "key_fields": [
        {
          "field": "in_clct_dpd",
          "table": "collection_files.clct_cf_loan_all",
          "comment": "入催DPD",
          "business_role": "判断预逾期入催窗口"
        }
      ],
      "expression_omitted": false,
      "truncated": true,
      "evidence": [
        {
          "source": "lineage",
          "path": "$.scopes.ROOT.filters[0]"
        }
      ]
    }
  ]
}
```

规则类型建议：

- `partition_filter`：分区/日期过滤；
- `business_filter`：业务筛选/准入/排除；
- `join_condition`：关联规则；
- `dedup_rule`：去重/取最新/取首次；
- `metric_rule`：指标计算；
- `classification_rule`：CASE 分类；
- `union_branch_rule`：UNION 分支来源或渠道规则；
- `explode_rule`：lateral view / explode 展开规则。

设计约束：

- 复杂 WHERE 不能只保留“涉及字段”；
- 至少保留关键条件组、表达式、字段、操作符和 SQL 片段；
- 表达式太长可以截断，但必须保留核心子条件；
- `condition_groups` 可以不是完整布尔 AST，但必须能支撑业务还原；
- 业务含义字段命名为 `meaning_hint`，表示语义线索，不等同于人工确认事实；
- 如果 `meaning_hint` 来源于字段名/注释推断，应标记 `evidence_type` 或 confidence。

## 10. semantic_profile.fields

回答“字段怎么来的、有什么业务作用”。

```json
{
  "fields": {
    "output_lineage": [
      {
        "column": "first_manual_online_time",
        "comment": "首次在线人工触达时间",
        "expression": "FIRST_VALUE(b.xma_begin_time) OVER (PARTITION BY t2.unique_id, t2.app_code ORDER BY b.xma_begin_time)",
        "transform": "WINDOW",
        "scope_id": "cte:manual_first_detail_online",
        "business_role": "放款后首次在线人工触达时间",
        "source_columns": [
          {
            "table": "report_csc_ana.mxg_online_base_info_di",
            "column": "xma_begin_time",
            "comment": "进线开始时间"
          }
        ],
        "trace_complete": true
      }
    ],
    "important_fields": [
      {
        "field": "final_complete_dt",
        "table": "hw_jhy_iceberg.dwd.dwd_ap_wdraw_aprv_det_df",
        "comment": "放款完成时间",
        "used_in": ["join_time_window", "window_order", "output"],
        "business_role": "作为放款后触达时间窗口的起点",
        "importance_reasons": [
          "used_in_join_condition",
          "used_in_output_lineage",
          "time_boundary_field"
        ]
      }
    ]
  }
}
```

设计约束：

- `output_lineage` 是最终输出字段级血缘，不等同于所有中间字段；
- 复杂任务可以限制 `output_lineage.physical_sources` 数量，但必须保留 count/truncated；
- `important_fields` 必须基于使用场景判断，而不仅是字段名启发；
- 重要字段来源包括 WHERE、JOIN ON、GROUP BY、WINDOW PARTITION、WINDOW ORDER、CASE WHEN、输出表达式和跨 scope 传递；
- 字段中文名/注释来自 metadata；缺失时不编造。

## 11. semantic_profile.quality

回答“哪里可信，哪里需要谨慎”。

```json
{
  "quality": {
    "trace_complete": true,
    "trace_incomplete_columns": [],
    "schema_coverage": {
      "input_tables_with_metadata": 2,
      "input_tables_total": 2,
      "missing_metadata_tables": []
    },
    "dangling_scopes": [
      {
        "scope_id": "subq:derived_0",
        "reason": "lineage-only scope has no downstream feeds",
        "risk": "可能是 SQL 未使用分支，也可能是解析漏连"
      }
    ],
    "warnings": [],
    "known_limits": [
      "部分业务语义基于字段名、表名和注释推断，需结合业务知识确认"
    ]
  }
}
```

设计约束：

- `trace_complete=false` 必须有原因；
- 孤立 scope 不能静默删除；
- schema 缺失、星号未展开、UDF 黑盒、动态 SQL、表达式截断都必须进入质量边界；
- `quality` 中的 warning 不一定阻断生成，但必须可被验证器统计。

## 12. Evidence 规范

所有关键语义对象都应支持 evidence。

```json
{
  "evidence": [
    {
      "type": "parsed_sql",
      "source": "profile",
      "path": "$.semantic_profile.rules[0].condition_groups[1]",
      "sql_fragment": "in_clct_dpd >= -7 and in_clct_dpd <= 0"
    },
    {
      "type": "metadata",
      "source": "related_metadata",
      "table": "collection_files.clct_cf_loan_all",
      "column": "in_clct_dpd",
      "comment": "入催DPD"
    }
  ],
  "confidence": "medium"
}
```

Evidence 类型：

- `parsed_sql`：SQL 解析事实；
- `lineage`：血缘追溯事实；
- `metadata`：表/字段元数据；
- `diagnostic`：解析诊断；
- `heuristic_inference`：规则/字段名启发；
- `llm_inferred`：LLM 推断；
- `user_knowledge`：人工知识或业务知识库。

设计约束：

- SQL 明确事实和语义推断必须分开；
- LLM 生成文档时，应优先表达 `parsed_sql` 和 `metadata`，对 `heuristic_inference` 使用谨慎措辞；
- 人工知识可以覆盖或增强推断，但必须保留来源。

## 13. Compact Policy

`profile.json` 不能太轻，但必须控制体积。瘦身原则是“去重复、保核心、留证据”。

必须保留：

- 任务目标和流程摘要；
- 输入/输出表语义；
- 每个主要 scope 的语义、输入输出和关键逻辑；
- 关键 WHERE/JOIN/WINDOW/CASE/UNION 规则；
- 关键字段和字段作用；
- 最终输出字段血缘；
- trace 和质量边界。

可以压缩：

- 超长表达式；
- 重复出现的完整 CASE/WHERE；
- 大量非关键字段元数据；
- 超多物理来源字段；
- 所有中间字段完整映射；
- 纯页面布局信息。

建议策略：

```json
{
  "compact_policy": {
    "target_profile_size_kb": 80,
    "large_profile_soft_limit_kb": 150,
    "max_condition_groups_per_rule": 12,
    "max_sql_fragment_chars": 600,
    "max_scope_key_output_columns": 20,
    "max_output_physical_sources": 8,
    "truncation_preserves": [
      "business_rules",
      "join_keys",
      "filter_fields",
      "window_keys",
      "case_fields",
      "important_output_columns"
    ]
  }
}
```

超大 SQL 的处理：

- `profile.json` 保留关键规则和摘要；
- `lineage.json` 保留完整字段和表达式；
- `task_insight.json` 可以从 lineage 补页面展开详情；
- 后续可选拆出 `scope_details/*.json`，但这不是第一阶段要求。

## 14. 从设计上避免历史问题

### 14.1 避免直接/间接来源混淆

结构强制区分：

- `direct_inputs`
- `direct_source_tables`
- `upstream_physical_tables`

生成文案规则：

- 只有 `direct_source_tables` 可用于“读取某物理表”；
- `upstream_physical_tables` 只能用于“上游可追溯至”；
- 验证器必须检查 summary 中“读取”和 `direct_source_tables` 的一致性。

### 14.2 避免 scope 数量和图数量不一致

结构强制：

- `scope_id` 使用 lineage 原始 id；
- `object_id` 可用于页面，但不能替代 `scope_id`；
- 同名 scope 必须用完整 `scope_id` 区分；
- 不允许用 display name 覆盖 scope 对象。

### 14.3 避免孤立 scope 静默隐藏

孤立 scope 必须进入：

- `semantic_profile.quality.dangling_scopes`
- `task_insight.graph_diagnostics.dangling_scope_ids`

业务视图可默认隐藏，但完整模式必须显示。

### 14.4 避免复杂 WHERE 只剩字段列表

规则对象必须包含：

- `condition_groups`
- `expression`
- `fields`
- `operators`
- `meaning_hint`
- `sql_fragment`

如果表达式被截断，需要标记 `truncated=true` 和 `original_length`。

### 14.5 避免关键字段误判

重要字段必须记录 `used_in`：

- `filter`
- `join`
- `group_by`
- `window_partition`
- `window_order`
- `case_condition`
- `output_expression`
- `propagated_to_output`

字段名启发只能作为附加 reason。

### 14.6 避免 LLM 推断被当成事实

所有语义摘要、`meaning_hint`、`business_role` 都应有 evidence/confidence。若没有 SQL 或 metadata 支撑，只能标为推断。

### 14.7 避免多 statement 任务割裂

任务级必须保留：

- `statement_count`
- `statements`
- `target_tables`
- statement 到 output table 的关系

LLM 先读任务级多 statement 流程，再读单 statement 细节。

## 15. 验证方案

### 15.1 结构一致性验证

继续使用并扩展 `tools/validate_task_insight.py`：

- `lineage_scope_count == len(lineage.scopes)`；
- 每个 lineage scope 都进入 `task_insight.objects.scopes`；
- HTML 内嵌 JSON 和 `task_insight.json` 一致；
- links endpoint 存在；
- hidden/dangling scope 有原因；
- output field count 和唯一输出字段一致；
- direct/indirect source 文案不混淆。

### 15.2 profile 语义完整性验证

新增 `tools/validate_profile_semantics.py`。

建议检查：

- `semantic_profile` 顶层结构存在；
- 每个主要 step 有 `semantic_role`；
- 每个 business filter 有 `condition_groups`；
- 复杂 WHERE 不能只有 raw_summary/field list；
- condition group 至少有 expression、fields、operators 或 sql_fragment；
- important fields 覆盖 WHERE/JOIN/WINDOW/CASE 使用字段；
- 多 statement 任务有 statement 建模；
- `trace_complete=false` 有原因；
- `business_role` / `meaning_hint` 有 evidence/confidence；
- `direct_source_tables` 为空时，summary 不得写“读取物理表”。

### 15.3 内容语义验收

固定 5 到 10 个标杆任务，维护人工 checklist。

首批建议：

- `clct_file_in_collect_loan`：复杂入催规则、多 statement、多输出；
- `bj_bas_mxg_ca_customer_inbound_log_d_df`：多渠道 UNION、首次触达、窗口函数；
- 包含 `lateral view posexplode` 的热线宽表任务；
- 一个包含 `SELECT *` 的任务；
- 一个超大复杂任务。

`clct_file_in_collect_loan` 必须能从 profile 支撑 LLM 说出：

- 任务目标是筛选待入催客户并生成贷款明细；
- 三段输出分别是什么；
- 主要入催条件包括产品排除/纳入、DPD -7 到 0、已逾期窗口、出账日至还款日、当日激活、强制还款、账单延期、宽限期、有还款金额、超额合同；
- 关键字段包括 `in_clct_dpd`、`overdue_date`、`paid_out_date`、`forced_pay_off`、`stmt_delay_ind`、`grace_date`、`repay_amt`、`product_cd`、`contra_no`；
- 哪些是 SQL 明确事实，哪些是基于字段名或注释推断。

### 15.4 LLM 输出验证

继续使用并扩展 `tools/validate_llm_profile_doc.py`：

- 必须提到 task_name/target_table；
- 必须覆盖 business_summary/process/rules/fields/quality；
- 不得把 candidate grain 说成主键；
- 必须披露 trace 不完整和 schema 边界；
- 对固定标杆任务检查 expected semantic checklist；
- 发现 profile 中有关键规则但文档未提到时给 error。

## 16. 实施阶段

### 阶段 1：设计和验证骨架

- 添加本设计文档；
- 添加 `validate_profile_semantics.py` 骨架；
- 添加标杆任务 checklist；
- 不改变现有输出语义。

### 阶段 2：增强规则结构

- 从 WHERE/JOIN/HAVING 中提取 `condition_groups`；
- 保留关键 SQL 片段；
- 增强 `business_rule_candidates` 或生成 `semantic_profile.rules`；
- 针对 `clct_file_in_collect_loan` 验证入催规则。

### 阶段 3：增强 scope 和字段

- 增加 scope 级 `key_output_columns`；
- 增加 scope 级 `key_fields`；
- 增加字段 `used_in`；
- 增强 `important_columns` 的理由。

### 阶段 4：LLM Prompt 和 HTML 迁移

- Prompt 优先读取 `semantic_profile`；
- `task_insight.json` 从 `semantic_profile + lineage` 生成更丰富详情；
- 页面详情区展示规则组、字段作用和 SQL evidence。

### 阶段 5：全量回归

- 全量 corpus 生成；
- profile size 分布统计；
- `validate_profile_semantics.py`；
- `validate_task_insight.py`；
- 5 到 10 个标杆任务内容验收；
- LLM 输出抽样评审。

## 17. 成功标准

结构成功：

- `profile.json` 能表达任务级、scope 级、字段级、规则级、质量边界；
- 旧消费者不被破坏；
- `task_insight.json` 可以继续从 `profile.json + lineage.json` 生成。

语义成功：

- LLM 基于 `profile.json` 可以稳定生成任务业务说明；
- 对复杂规则任务，能说清核心业务规则，而不是只列字段；
- 对多 statement 任务，能说明多个输出之间的关系；
- 对多渠道/窗口任务，能说明首次、最新、分支来源和时间窗口。

验证成功：

- 全量结构校验 `error=0`；
- profile 语义校验核心 error=0；
- 标杆任务 checklist 通过；
- warning 类型可解释；
- 任何直接/间接来源混淆、scope 漏节点、HTML/JSON 不一致都能被自动检查发现。

## 18. 结论

`profile.json` 应该成为 SQL 业务语义还原的主输入，而不是过度瘦身的目录。它需要在可控体积内保留任务目标、输入输出、scope 模块、关键规则、关键字段、字段血缘和证据边界。

本设计采用兼容演进方式：新增 `semantic_profile`，保留现有字段；先增强规则和字段使用场景，再迁移 prompt 和页面。这样可以在不破坏现有能力的前提下，把项目从“血缘解析”推进到“SQL 任务理解”。

