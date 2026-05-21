from lineage_parser import parse_scope_lineage, to_profile_dict


def test_semantic_profile_contains_task_tables_process_fields_and_quality():
    sql = """
    INSERT OVERWRITE TABLE mart.customer_touch
    WITH base AS (
      SELECT customer_id, app_code, event_time
      FROM ods.touch_events
      WHERE dt = '20260515' AND status = 'connected'
    )
    SELECT customer_id, app_code, event_time AS first_touch_time
    FROM base
    """
    schema = {
        "ods.touch_events": [
            {"name": "customer_id", "type": "string", "comment": "客户ID"},
            {"name": "app_code", "type": "string", "comment": "申请主体"},
            {"name": "event_time", "type": "string", "comment": "触达时间"},
            {"name": "dt", "type": "string", "comment": "日期分区"},
            {"name": "status", "type": "string", "comment": "触达状态"},
        ],
        "mart.customer_touch": [
            {"name": "customer_id", "type": "string", "comment": "客户ID"},
            {"name": "app_code", "type": "string", "comment": "申请主体"},
            {"name": "first_touch_time", "type": "string", "comment": "首次触达时间"},
        ],
    }

    result = parse_scope_lineage(sql, "customer_touch_task", schema=schema)
    profile = to_profile_dict(result)
    semantic = profile["semantic_profile"]

    assert semantic["version"] == "2.0"
    assert semantic["task"]["task_name"] == "customer_touch_task"
    assert semantic["task"]["target_tables"][0]["table"] == "mart.customer_touch"
    assert semantic["tables"]["inputs"][0]["table"] == "ods.touch_events"
    assert semantic["tables"]["outputs"][0]["table"] == "mart.customer_touch"
    assert semantic["process"]["steps"]
    assert semantic["fields"]["output_lineage"]
    assert semantic["quality"]["trace_complete"] is True


def test_semantic_rules_preserve_complex_where_condition_groups():
    sql = """
    INSERT OVERWRITE TABLE collection_files.clct_cf_pre_in_coll_cust
    SELECT a.internal_customer_id, a.acct_nbr
    FROM collection_files.clct_cf_loan_all a
    LEFT JOIN (
      SELECT DISTINCT contra_no FROM collection_files.dim_excess_contra
    ) b ON a.contr_nbr = b.contra_no
    WHERE dt = '20260426'
      AND ((substr(product_cd, 3, 1) != '2' AND product_cd NOT IN ('005800','005605','005502'))
           OR product_cd IN ('002301','002316'))
      AND (
        (overdue_date IS NULL AND paid_out_date IS NULL AND in_clct_dpd >= -7 AND in_clct_dpd <= 0)
        OR (forced_pay_off = 'Y')
        OR stmt_delay_ind = 'Y'
        OR (grace_date >= '2026-04-27' AND tot_due_amt > 0)
        OR nvl(repay_amt, 0) > 0
        OR b.contra_no IS NOT NULL
      )
    """
    schema = {
        "collection_files.clct_cf_loan_all": [
            {"name": "internal_customer_id", "comment": "客户ID"},
            {"name": "acct_nbr", "comment": "账户号"},
            {"name": "contr_nbr", "comment": "合同号"},
            {"name": "dt", "comment": "日期分区"},
            {"name": "product_cd", "comment": "产品码"},
            {"name": "overdue_date", "comment": "逾期日期"},
            {"name": "paid_out_date", "comment": "结清日期"},
            {"name": "in_clct_dpd", "comment": "入催DPD"},
            {"name": "forced_pay_off", "comment": "强制还款标记"},
            {"name": "stmt_delay_ind", "comment": "账单延期标记"},
            {"name": "grace_date", "comment": "宽限期日期"},
            {"name": "tot_due_amt", "comment": "总应还金额"},
            {"name": "repay_amt", "comment": "还款金额"},
        ],
        "collection_files.dim_excess_contra": [{"name": "contra_no", "comment": "超额合同号"}],
        "collection_files.clct_cf_pre_in_coll_cust": [
            {"name": "internal_customer_id", "comment": "客户ID"},
            {"name": "acct_nbr", "comment": "账户号"},
        ],
    }

    result = parse_scope_lineage(sql, "clct_file_in_collect_loan", schema=schema)
    profile = to_profile_dict(result)
    rules = profile["semantic_profile"]["rules"]
    where_rule = next(rule for rule in rules if rule["source"] == "WHERE")
    expressions = [group["expression"] for group in where_rule["condition_groups"]]

    assert any("IN_CLCT_DPD" in expression.upper() and ">=" in expression for expression in expressions)
    assert any("FORCED_PAY_OFF" in expression.upper() for expression in expressions)
    assert any("STMT_DELAY_IND" in expression.upper() for expression in expressions)
    assert any("REPAY_AMT" in expression.upper() for expression in expressions)
    assert where_rule["key_fields"]
