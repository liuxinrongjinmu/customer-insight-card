#!/usr/bin/env python3
"""生成客户认知卡原型（单文件 HTML，可直接双击打开）。

为什么用脚本生成而不是手写死数据：
置信度、档位、保鲜期、冲突归并这些数字必须来自真实的 schema.py，
否则原型会演示一套规则、后端跑另一套规则，这种「原型与实现两张皮」是最容易
在评审时被戳穿的地方。这里每一张卡片上的 0.94 / 待确认 / 已过期 都是算出来的。

用法：
    python3 demo/build_demo.py
产物：
    demo/客户认知卡-原型.html

产物不入库（见 .gitignore）：要对外的可交互版本是 `客户认知卡-演示.html`，
那份要后端；这份是它的静态前身，双击即看，用来发给不想起服务的人。
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.extraction.schema import (  # noqa: E402
    FRESHNESS_DAYS,
    SUBJECT_KEY_FIELDS,
    _to_date,
    compute_confidence,
    display_tier,
)
from src.extraction.validator import normalize_fact  # noqa: E402
from src.aggregation.card import CustomerProfile, build_card  # noqa: E402
from src.advice.engine import generate_advice  # noqa: E402
from src.pipeline import to_fact  # noqa: E402
from demo.brief import account_strip, handover, proposal_brief, visit_brief  # noqa: E402
from demo.viewmodel import NEXT_STEP  # noqa: E402

AS_OF = date(2026, 10, 25)
OUT = Path(__file__).resolve().parent / "客户认知卡-原型.html"


# ---------------------------------------------------------------------------
# 演示数据：三家公司、12 条记录。
# 这些文本是人工撰写的演示素材，事实由人工标注（等价于「抽取结果」），
# 置信度与保鲜期由引擎计算——原型要展示的是交互形态与信息架构，
# 不是模型准确率（模型实测见 README）。
# ---------------------------------------------------------------------------

CUSTOMERS = [
    {
        "id": "c_30001",
        "name": "恒远重工",
        "industry": "装备制造",
        "stage": "选型 POC 阶段",
        "owner": "你（本周接手）",
        # 档案层：主数据。事实答的是「他提过什么」，「这个客户是谁、值多少」只能从这儿来。
        "profile": {
            "scale": "3200 人 · 年产值 45 亿",
            "since": "2019-04",
            "open_opportunity": {"name": "智能质检二期", "amount": 180, "stage": "POC 验证"},
            "contract": {"expires_on": "2027-03-31", "arr": 96},
            "tickets_90d": {"n": 7, "avg_hours": 14},
        },
        "records": [
            {"id": "r11", "date": "2026-09-08", "type": "visit_note", "type_cn": "拜访纪要",
             "text": "9月8日拜访恒远重工，采购总监李国强接待。李总说集团刚下发降本指标，今年采购成本要压 8%。"
                     "他提到现有供应商响应慢，上个月设备故障报了三天才有人到场，这个他们意见很大。"
                     "我们答应9月11日前把服务响应时效的承诺函发过去。"},
            {"id": "r12", "date": "2026-09-28", "type": "meeting_minutes", "type_cn": "会议纪要",
             "text": "9月28日选型会。与会确认三家进入短名单：我方、云枢科技、博思数智。"
                     "李总说云枢在集团另一家子公司已经上线，领导层倾向先看他们的案例。"
                     "会议定 10 月 15 日做 POC，两家同时进场。"},
            {"id": "r13", "date": "2026-10-09", "type": "call_transcript", "type_cn": "通话记录",
             "text": "客户：你们比云枢报得高，差不多高 12%。我们财务要求软件类支出同比下降 10%，还要分期付款。"
                     "另外我担心你们实施人手不够，上次那个项目拖了两个月。销售：人手可以保证。客户：那就先看 POC。"},
            {"id": "r14", "date": "2026-10-20", "type": "im_message", "type_cn": "即时通讯",
             "text": "销售：李总，POC 时间定了吗？客户：等我们内部定，这两天没消息。"},
            {"id": "r15", "date": "2026-10-18", "type": "email", "type_cn": "邮件",
             "text": "关于服务响应时效承诺函：我方内部评审后确认 10 月 30 日前发出，"
                     "比原定的 9 月 11 日推迟。客户回复已知悉，并要求不要再延期。"},
        ],
        "facts": [
            {"rid": "r11", "fact_type": "need", "statement": "集团要求今年采购成本压降 8%。",
             "span": "今年采购成本要压 8%",
             "payload": {"need_text": "压降采购成本 8%", "category": "cost", "explicitness": "explicit", "priority": "must"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r11", "fact_type": "concern", "statement": "客户对现有供应商的故障响应速度强烈不满。",
             "span": "上个月设备故障报了三天才有人到场，这个他们意见很大",
             "payload": {"concern_text": "现有供应商报修三天才到场，客户意见很大",
                         "verbatim_quote": "上个月设备故障报了三天才有人到场，这个他们意见很大",
                         "category": "support", "severity": "blocker", "is_resolved": False},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r11", "fact_type": "commitment", "statement": "我方承诺 9 月 11 日前发出服务响应时效承诺函。",
             "span": "我们答应9月11日前把服务响应时效的承诺函发过去",
             "payload": {"commitment_text": "发出服务响应时效承诺函", "direction": "we_owe",
                         "owner_id": "u_1027", "due_date": "2026-09-11", "due_precision": "day",
                         "fulfillment": "pending"},
             "basis": "direct_quote", "subject": ("account",)},
            # 同一件事在 10-18 被改期：这是演示「演进 vs 矛盾」判据的唯一素材。
            # 两条记录相隔 40 天（> 3 天窗口）→ 判为改期，旧的置 superseded；
            # 如果两条写于同一天前后，同一件事出现两个到期日才是口径矛盾。
            {"rid": "r15", "fact_type": "commitment", "statement": "我方改期承诺 10 月 30 日前发出服务响应时效承诺函。",
             "span": "确认 10 月 30 日前发出",
             "payload": {"commitment_text": "发出服务响应时效承诺函", "direction": "we_owe",
                         "owner_id": "u_1027", "due_date": "2026-10-30", "due_precision": "day",
                         "fulfillment": "pending"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r11", "fact_type": "stakeholder", "statement": "李国强任采购总监，是本项目主要推动者。",
             "span": "采购总监李国强接待",
             "payload": {"contact_id": None, "name": "李国强", "title": "采购总监",
                         "role_in_deal": "decision_maker", "attitude": "supportive", "influence": "high"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r12", "fact_type": "competition", "statement": "云枢科技与我方同时进入短名单，其集团内案例占先。",
             "span": "云枢在集团另一家子公司已经上线，领导层倾向先看他们的案例",
             "payload": {"competitor_name": "云枢科技", "stage": "shortlisted",
                         "customer_view": "子公司已上线，领导层倾向先看其案例", "our_position": "trailing",
                         "dimensions": ["reference_case"]},
             "basis": "paraphrase", "subject": ("account",)},
            {"rid": "r12", "fact_type": "competition", "statement": "博思数智进入短名单，客户未给出评价。",
             "span": "三家进入短名单：我方、云枢科技、博思数智",
             "payload": {"competitor_name": "博思数智", "stage": "shortlisted",
                         "customer_view": "未评价", "our_position": "unknown"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r12", "fact_type": "timeline", "statement": "双方于 10 月 15 日同时进场做 POC。",
             "span": "会议定 10 月 15 日做 POC",
             "payload": {"event": "POC 进场", "target_date": "2026-10-15", "precision": "day",
                         "hardness": "hard", "driver": "procurement"},
             "basis": "paraphrase", "subject": ("opportunity",)},
            {"rid": "r13", "fact_type": "need", "statement": "财务要求软件类支出同比下降 10%。",
             "span": "我们财务要求软件类支出同比下降 10%",
             "payload": {"need_text": "软件支出同比下降 10%", "category": "cost", "explicitness": "explicit", "priority": "must"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r13", "fact_type": "concern", "statement": "我方报价比云枢高约 12%。",
             "span": "你们比云枢报得高，差不多高 12%",
             "payload": {"concern_text": "我方报价高于云枢约 12%", "verbatim_quote": "",
                         "category": "price", "severity": "high", "is_resolved": False},
             "basis": "paraphrase", "subject": ("account",)},
            {"rid": "r13", "fact_type": "need", "statement": "客户要求分期付款。",
             "span": "还要分期付款",
             "payload": {"need_text": "分期付款", "category": "other", "explicitness": "explicit", "priority": "should"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r13", "fact_type": "concern", "statement": "客户担心我方实施人力不足、重演延期。",
             "span": "我担心你们实施人手不够",
             "payload": {"concern_text": "担心实施人手不足导致延期", "verbatim_quote": "我担心你们实施人手不够",
                         "category": "trust", "severity": "high", "is_resolved": False},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r14", "fact_type": "risk_signal", "statement": "客户回复节奏明显放慢，POC 时间悬空。",
             "span": "等我们内部定，这两天没消息",
             "payload": {"signal_type": "silence", "baseline": "此前客户平均 2 个工作日内回复",
                         "observed": "10 月 9 日后 11 天未获实质回复", "severity": "medium", "detected_by": "model"},
             "basis": "inferred", "subject": ("account",)},
        ],
    },
    {
        "id": "c_30002",
        "name": "蓝海物流",
        "industry": "仓储物流",
        "stage": "续约谈判",
        "owner": "张帆（8 月起交接给你）",
        "profile": {
            "scale": "1800 人 · 42 个分拨中心",
            "since": "2021-09",
            "open_opportunity": {"name": "仓储系统续约", "amount": 260, "stage": "商务谈判"},
            "contract": {"expires_on": "2026-11-30", "arr": 260},
            "tickets_90d": {"n": 3, "avg_hours": 6},
        },
        "records": [
            {"id": "r21", "date": "2026-03-18", "type": "email", "type_cn": "邮件",
             "text": "关于移动端审批需求，我们内部已立项，希望 Q3 前上线。另请确认今年续约价格。"},
            {"id": "r22", "date": "2026-08-12", "type": "meeting_minutes", "type_cn": "会议纪要",
             "text": "8月12日例会。张涛提出报表要能按线路维度钻取。同时反映上季度两次升级都在白天做，"
                     "影响了分拣作业，要求以后必须安排在夜里。我们承诺 9 月 5 日前提供数据清理方案，"
                     "另外答应后续升级改到夜间窗口。"},
            {"id": "r23", "date": "2026-10-10", "type": "visit_note", "type_cn": "拜访纪要",
             "text": "10月10日拜访蓝海物流。张涛说续约要走集团采购，11 月底前定标。"
                     "他提到最近在扩仓，系统并发的压力比去年大不少。"},
        ],
        "facts": [
            {"rid": "r21", "fact_type": "need", "statement": "客户计划 Q3 前上线移动端审批。",
             "span": "希望 Q3 前上线",
             "payload": {"need_text": "上线移动端审批", "category": "function", "explicitness": "explicit", "priority": "must"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r22", "fact_type": "need", "statement": "报表需支持按线路维度钻取。",
             "span": "报表要能按线路维度钻取",
             "payload": {"need_text": "报表按线路维度钻取", "category": "function", "explicitness": "explicit", "priority": "must"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r22", "fact_type": "concern", "statement": "我方升级时段影响客户分拣作业。",
             "span": "上季度两次升级都在白天做，影响了分拣作业",
             "payload": {"concern_text": "升级安排在白天影响作业，须改夜间",
                         "verbatim_quote": "上季度两次升级都在白天做，影响了分拣作业",
                         "category": "support", "severity": "high", "is_resolved": False},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r22", "fact_type": "commitment", "statement": "我方承诺 9 月 5 日前提供数据清理方案。",
             "span": "我们承诺 9 月 5 日前提供数据清理方案",
             "payload": {"commitment_text": "提供数据清理方案", "direction": "we_owe", "owner_id": "u_1027",
                         "due_date": "2026-09-05", "due_precision": "day", "fulfillment": "overdue"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r22", "fact_type": "commitment", "statement": "我方答应后续升级改到夜间窗口（未定时间）。",
             "span": "另外答应后续升级改到夜间窗口",
             "payload": {"commitment_text": "后续升级改到夜间窗口", "direction": "we_owe", "owner_id": "u_1027",
                         "due_date": None, "due_precision": None, "fulfillment": "pending"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r22", "fact_type": "stakeholder", "statement": "张涛是信息部对接人，主动协助推进。",
             "span": "张涛提出报表要能按线路维度钻取",
             "payload": {"contact_id": None, "name": "张涛", "title": "信息部主管",
                         "role_in_deal": "champion", "attitude": "supportive", "influence": "medium"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r23", "fact_type": "timeline", "statement": "续约须在 11 月底前完成定标。",
             "span": "11 月底前定标",
             "payload": {"event": "续约定标", "target_date": "2026-11-30", "precision": "month",
                         "hardness": "hard", "driver": "contract"},
             "basis": "paraphrase", "subject": ("opportunity",)},
            {"rid": "r23", "fact_type": "need", "statement": "扩仓导致并发压力超出现有系统容量。",
             "span": "最近在扩仓，系统并发的压力比去年大不少",
             "payload": {"need_text": "支撑扩仓后的并发容量", "category": "performance",
                         "explicitness": "explicit", "priority": "should"},
             "basis": "paraphrase", "subject": ("account",)},
            {"rid": "r23", "fact_type": "risk_signal", "statement": "需求范围相比年初已发生偏移。",
             "span": "他提到最近在扩仓，系统并发的压力比去年大不少",
             "payload": {"signal_type": "requirement_drift", "baseline": "3 月确认范围为移动端审批",
                         "observed": "新增报表钻取与并发扩容要求", "severity": "high", "detected_by": "model"},
             "basis": "inferred", "subject": ("account",)},
        ],
    },
    {
        "id": "c_30003",
        "name": "星海电子",
        "industry": "电子制造",
        "stage": "初步接触",
        "owner": "你",
        "profile": {
            "scale": "900 人 · 年采购 1.2 亿",
            "since": "2026-09",
            "open_opportunity": {"name": "MES 集成预研", "amount": 0, "stage": "初步接触"},
            "tickets_90d": {"n": 0, "avg_hours": 0},
        },
        "records": [
            {"id": "r31", "date": "2026-10-22", "type": "im_message", "type_cn": "即时通讯",
             "text": "客户：你们在汽车电子行业做过类似项目吗？我们明年可能要上 MES，先了解一下。"},
        ],
        "facts": [
            {"rid": "r31", "fact_type": "stakeholder", "statement": "周敏为采购经理，是本项目当前唯一联系人。",
             "span": "客户：",
             "payload": {"contact_id": None, "name": "周敏", "title": "采购经理",
                         "role_in_deal": "gatekeeper", "attitude": "unknown", "influence": "high"},
             "basis": "direct_quote", "subject": ("account",)},
            {"rid": "r31", "fact_type": "need", "statement": "客户关注汽车电子行业的项目经验。",
             "span": "你们在汽车电子行业做过类似项目吗",
             "payload": {"need_text": "具备汽车电子行业经验", "category": "other",
                         "explicitness": "explicit", "priority": "could"},
             "basis": "paraphrase", "subject": ("account",)},
        ],
    },
    {
        "id": "c_30004",
        "name": "东岭铜业",
        "industry": "有色金属",
        "stage": "方案确认阶段",
        "owner": "你",
        # 这一户是专门为「演进链」造的：同一件事被提到四次，三次改期、最后被客户作废。
        # 演示价值在于——把它拖回 10-01，承诺还是逾期的；拖到 10-09 之后，
        # 客户那头一句「本期不做了」，P0 自己消失。这才叫「一条记录改变结论」。
        "profile": {
            "scale": "5200 人 · 年营收 120 亿",
            "since": "2018-06",
            "open_opportunity": {"name": "设备联网改造", "amount": 310, "stage": "方案确认"},
            "contract": {"expires_on": "2028-06-30", "arr": 0},
            "tickets_90d": {"n": 2, "avg_hours": 8},
        },
        "records": [
            {"id": "r41", "date": "2026-07-28", "type": "visit_note", "type_cn": "拜访纪要",
             "text": "7月28日拜访东岭铜业，设备部赵启明部长接待。他们 6 条产线的设备运行数据还靠人工抄表，"
                     "想接进系统做实时看板。我们答应 7 月底前把设备联网接入方案发过来。"},
            {"id": "r42", "date": "2026-08-19", "type": "meeting_minutes", "type_cn": "会议纪要",
             "text": "8月19日方案评审会。赵部长说这版只覆盖了 2 条产线，要求覆盖全部 6 条。"
                     "我们承诺 8 月 26 日前给改好的版本。另外原 IT 对接人调岗，新的技术接口人还没定。"},
            {"id": "r43", "date": "2026-09-15", "type": "call_transcript", "type_cn": "通话记录",
             "text": "客户：方案呢？这是第三次了，我们领导都在问。销售：这周一定给，9 月底前交付，"
                     "我拿人头担保。客户：最好是这样。"},
            {"id": "r44", "date": "2026-10-09", "type": "meeting_minutes", "type_cn": "会议纪要",
             "text": "10月9日会议。客户内部决定本期先不上设备联网，挪到二期再做，接入方案不用交了。"
                     "本期范围收缩为报表，11 月中旬做一次需求评审。"},
            {"id": "r45", "date": "2026-10-21", "type": "im_message", "type_cn": "即时通讯",
             "text": "客户：确认下，本期我们就做报表，联网的事二期再说，方案你们先不用出了。"},
        ],
        "facts": [
            {"rid": "r41", "fact_type": "need", "statement": "客户希望把 6 条产线的设备运行数据接进系统。",
             "span": "他们 6 条产线的设备运行数据还靠人工抄表",
             "payload": {"need_text": "设备运行数据接入系统", "category": "integration",
                         "explicitness": "explicit", "priority": "must"},
             "basis": "paraphrase"},
            {"rid": "r41", "fact_type": "commitment", "statement": "我方承诺 7 月 31 日前提交设备联网接入方案。",
             "span": "7 月底前把设备联网接入方案发过来",
             "payload": {"commitment_text": "提交设备联网接入方案", "direction": "we_owe",
                         "owner_id": "u_1027", "due_date": "2026-07-31", "due_precision": "day",
                         "fulfillment": "pending"},
             "basis": "direct_quote"},
            {"rid": "r41", "fact_type": "stakeholder", "statement": "赵启明任设备部部长，是联网需求的提出者。",
             "span": "设备部赵启明部长接待",
             "payload": {"contact_id": None, "name": "赵启明", "title": "设备部部长",
                         "role_in_deal": "influencer", "attitude": "supportive", "influence": "high"},
             "basis": "direct_quote"},
            {"rid": "r42", "fact_type": "need", "statement": "接入方案须覆盖全部 6 条产线，只覆盖 2 条被退回。",
             "span": "这版只覆盖了 2 条产线，要求覆盖全部 6 条",
             "payload": {"need_text": "接入方案覆盖全部 6 条产线", "category": "integration",
                         "explicitness": "explicit", "priority": "must"},
             "basis": "direct_quote"},
            {"rid": "r42", "fact_type": "commitment", "statement": "我方承诺 8 月 26 日前提交覆盖 6 条产线的接入方案。",
             "span": "承诺 8 月 26 日前给改好的版本",
             "payload": {"commitment_text": "提交设备联网接入方案", "direction": "we_owe",
                         "owner_id": "u_1027", "due_date": "2026-08-26", "due_precision": "day",
                         "fulfillment": "pending"},
             "basis": "direct_quote"},
            {"rid": "r42", "fact_type": "risk_signal", "statement": "客户侧 IT 对接人调岗，技术接口关系中断。",
             "span": "原 IT 对接人调岗，新的技术接口人还没定",
             "payload": {"signal_type": "contact_change", "baseline": "此前由原 IT 对接人跟进技术方案",
                         "observed": "对接人调岗，新接口人未定", "severity": "medium", "detected_by": "rule"},
             "basis": "direct_quote"},
            {"rid": "r43", "fact_type": "commitment", "statement": "我方承诺 9 月 30 日前交付接入方案。",
             "span": "9 月底前交付",
             "payload": {"commitment_text": "提交设备联网接入方案", "direction": "we_owe",
                         "owner_id": "u_1027", "due_date": "2026-09-30", "due_precision": "day",
                         "fulfillment": "pending"},
             "basis": "direct_quote"},
            {"rid": "r43", "fact_type": "concern", "statement": "客户对我方三次延期已明确表达不满。",
             "span": "这是第三次了，我们领导都在问",
             "payload": {"concern_text": "方案交付三次延期，客户不满且已被其领导关注",
                         "verbatim_quote": "这是第三次了，我们领导都在问",
                         "category": "trust", "severity": "high", "is_resolved": False},
             "basis": "direct_quote"},
            # 承诺被客户作废。fulfillment 是「有序字段」，pending → waived 属于推进，
            # 于是这条取代前面三条，整条承诺从「逾期」变成「已关闭」。
            # 这一步由引擎判，不是手写的——把 fulfillment 手填成 waived 就演不出判据了。
            {"rid": "r44", "fact_type": "commitment", "statement": "客户决定本期不再要求交付接入方案，该承诺关闭。",
             "span": "接入方案不用交了",
             "payload": {"commitment_text": "提交设备联网接入方案", "direction": "we_owe",
                         "owner_id": "u_1027", "due_date": None, "due_precision": None,
                         "fulfillment": "waived"},
             "basis": "direct_quote"},
            {"rid": "r44", "fact_type": "need", "statement": "本期范围收缩为报表，联网推迟到二期。",
             "span": "本期范围收缩为报表",
             "payload": {"need_text": "本期只做报表，联网改到二期", "category": "function",
                         "explicitness": "explicit", "priority": "must"},
             "basis": "direct_quote"},
            {"rid": "r44", "fact_type": "timeline", "statement": "11 月中旬做一次报表需求评审。",
             "span": "11 月中旬做一次需求评审",
             "payload": {"event": "报表需求评审", "target_date": "2026-11-15", "precision": "month",
                         "hardness": "soft", "driver": "business"},
             "basis": "paraphrase"},
            {"rid": "r45", "fact_type": "need", "statement": "客户再次确认本期只做报表，联网留到二期。",
             "span": "本期我们就做报表，联网的事二期再说",
             "payload": {"need_text": "本期只做报表，联网改到二期", "category": "function",
                         "explicitness": "explicit", "priority": "must"},
             "basis": "direct_quote"},
        ],
    },
    {
        "id": "c_30005",
        "name": "瑞禾食品",
        "industry": "食品加工",
        "stage": "商务谈判",
        "owner": "你",
        # 健康客户：有事实、有结论，但没有 P0。
        # 留这一户是为了让「没有待办」也被演示一次——如果每个客户都满屏红标，
        # 用户会以为这是常态；说不出「这个客户不用管」，这套东西就没有判断力。
        "profile": {
            "scale": "2400 人 · 6 个生产基地",
            "since": "2022-11",
            "open_opportunity": {"name": "质量追溯一期", "amount": 95, "stage": "商务谈判"},
            "contract": {"expires_on": "2026-12-31", "arr": 95},
            "tickets_90d": {"n": 1, "avg_hours": 4},
        },
        "records": [
            {"id": "r51", "date": "2026-09-12", "type": "visit_note", "type_cn": "拜访纪要",
             "text": "9月12日拜访瑞禾食品，信息部王磊经理接待。一期要覆盖 3 个车间，年底前上线，"
                     "预算 150 万已经批下来了。王经理主动说会帮我们推内部流程。"},
            {"id": "r52", "date": "2026-10-08", "type": "meeting_minutes", "type_cn": "会议纪要",
             "text": "10月8日技术评审通过。之前客户担心的与我们 ERP 的集成风险，这次方案里给了对接说明，"
                     "客户认可了，不再作为顾虑。合同预计 11 月 5 日签署。"},
            {"id": "r53", "date": "2026-10-19", "type": "email", "type_cn": "邮件",
             "text": "合同正在内部流转，预计 11 月初签。客户确认一期上线节点定在 12 月 15 日，"
                     "要求我们提前排实施资源。"},
        ],
        "facts": [
            {"rid": "r51", "fact_type": "need", "statement": "一期需覆盖 3 个车间，年底前上线。",
             "span": "一期要覆盖 3 个车间，年底前上线",
             "payload": {"need_text": "一期覆盖 3 个车间并上线", "category": "function",
                         "explicitness": "explicit", "priority": "must"},
             "basis": "direct_quote"},
            {"rid": "r51", "fact_type": "need", "statement": "项目预算 150 万已批复。",
             "span": "预算 150 万已经批下来了",
             "payload": {"need_text": "预算 150 万", "category": "cost",
                         "explicitness": "explicit", "priority": "must"},
             "basis": "direct_quote"},
            {"rid": "r51", "fact_type": "stakeholder", "statement": "王磊任信息部经理，主动帮我们推内部流程。",
             "span": "王经理主动说会帮我们推内部流程",
             "payload": {"contact_id": None, "name": "王磊", "title": "信息部经理",
                         "role_in_deal": "champion", "attitude": "supportive", "influence": "high"},
             "basis": "direct_quote"},
            # 已解决的顾虑不该再进待办。这条留在这里，是给「is_resolved」一个现场证据。
            {"rid": "r52", "fact_type": "concern", "statement": "客户此前担心与我方 ERP 的集成风险。",
             "span": "之前客户担心的与我们 ERP 的集成风险",
             "payload": {"concern_text": "担心与现有 ERP 的集成风险",
                         "verbatim_quote": "之前客户担心的与我们 ERP 的集成风险",
                         "category": "risk", "severity": "medium", "is_resolved": True},
             "basis": "direct_quote"},
            {"rid": "r52", "fact_type": "timeline", "statement": "合同预计 11 月 5 日签署。",
             "span": "合同预计 11 月 5 日签署",
             "payload": {"event": "合同签署", "target_date": "2026-11-05", "precision": "day",
                         "hardness": "soft", "driver": "contract"},
             "basis": "direct_quote"},
            {"rid": "r53", "fact_type": "timeline", "statement": "一期上线节点定在 12 月 15 日。",
             "span": "一期上线节点定在 12 月 15 日",
             "payload": {"event": "一期上线", "target_date": "2026-12-15", "precision": "day",
                         "hardness": "hard", "driver": "business"},
             "basis": "direct_quote"},
        ],
    },
    {
        "id": "c_30006",
        "name": "云启软件",
        "industry": "软件服务",
        "stage": "初步接触",
        "owner": "你",
        # 只有一句话的客户。用来演示「知道得少就说知道得少」：
        # 卡片上只有一条需求，其余全是缺口，且没有任何建议——
        # 因为凭一句话给待办，就是在编。
        "profile": {
            "scale": "300 人",
            "since": "2026-10",
            "open_opportunity": {"name": "私有化部署评估", "amount": 0, "stage": "初步接触"},
            "tickets_90d": {"n": 0, "avg_hours": 0},
        },
        "records": [
            {"id": "r61", "date": "2026-10-20", "type": "im_message", "type_cn": "即时通讯",
             "text": "客户：你们支持私有化部署吗？我们下周内部讨论一下，有需要再找你。"},
        ],
        "facts": [
            {"rid": "r61", "fact_type": "need", "statement": "客户询问是否支持私有化部署。",
             "span": "你们支持私有化部署吗",
             "payload": {"need_text": "支持私有化部署", "category": "function",
                         "explicitness": "explicit", "priority": "could"},
             "basis": "direct_quote"},
        ],
    },
]

TYPE_CN = {
    "need": "需求", "concern": "顾虑", "commitment": "承诺", "stakeholder": "关键人",
    "competition": "竞争", "timeline": "时间节点", "risk_signal": "风险信号",
}
BASIS_CN = {"direct_quote": "原话", "paraphrase": "转述", "inferred": "推断"}
SOURCE_CN = {"visit_note": "拜访纪要", "meeting_minutes": "会议纪要", "email": "邮件",
             "call_transcript": "通话记录", "im_message": "即时通讯", "ticket": "工单"}


# ---------------------------------------------------------------------------
# 计算：全部交给 Stage 2 引擎
# ---------------------------------------------------------------------------

def _is_overdue(f) -> bool:
    """我方承诺逾期未履行。"""
    if f.fact_type != "commitment":
        return False
    p = f.payload or {}
    if p.get("direction") != "we_owe" or p.get("fulfillment") in ("fulfilled", "waived"):
        return False
    due = _to_date(p.get("due_date"))
    return bool(due and due < AS_OF)


def _date_passed(f) -> bool:
    if f.fact_type != "timeline":
        return False
    t = _to_date((f.payload or {}).get("target_date"))
    return bool(t and t < AS_OF)


def to_facts(customer: dict) -> list:
    """把演示素材转成 Fact 对象。

    演示用的事实是人工撰写的（等价于「抽取结果」），但归并、冲突、演进、
    过期、一句话结论、建议一律由 src 下的引擎计算。这样原型上看到的每一个判断，
    都能在代码里找到对应的一行——评审时被问「这个冲突是怎么判的」，
    可以直接打开 conflict.py 而不是翻这篇脚本。
    """
    rec_map = {r["id"]: r for r in customer["records"]}
    out = []
    for f in customer["facts"]:
        rec = rec_map[f["rid"]]
        raw = {
            "fact_type": f["fact_type"],
            "customer_id": customer["id"],
            "statement": f["statement"],
            "payload": f["payload"],
            "source_type": rec["type"],
            "source_id": f["rid"],
            "source_span": f.get("span") or f["statement"][:12],
            # 事实可以自带事件时间。存量客户池里那条「本条为 9 月底补录」的拜访
            # 就是靠它回填到 6 月——入库时间是 9 月，事情发生在 6 月，
            # 这两件事在时间机器上必须能分开。
            "stated_at": f.get("stated_at") or rec["date"],
            "stated_at_precision": "day",
            "confidence_basis": f["basis"],
            "lang": "zh-CN",
            "recorded_at": rec["date"],
        }
        ctx = {
            "customer_id": customer["id"],
            "source_id": f["rid"],
            "source_type": rec["type"],
            "occurred_at": rec["date"],
            "current_user_id": "me",
        }
        nf = normalize_fact(raw, ctx)
        conf = nf.pop("_recomputed_confidence", None)
        if conf is None:
            conf = compute_confidence(nf["fact_type"], nf["source_type"],
                                      f["basis"], nf["payload"])
        nf["confidence"] = conf if conf is not None else 0.5
        nf.pop("_enum_repaired", None)
        out.append(to_fact(nf))
    return out


def enrich(customer: dict) -> dict:
    facts = to_facts(customer)
    profile = CustomerProfile(
        customer_id=customer["id"], name=customer["name"],
        industry=customer["industry"], stage=customer["stage"],
        owner_id="me", as_of=AS_OF.isoformat(),
    )
    card = build_card(profile, facts, today=AS_OF)
    adv = generate_advice(card, today=AS_OF)

    rec_map = {r["id"]: r for r in customer["records"]}
    key_of = {f.fact_id: f"k{i}" for i, f in enumerate(facts)}
    group_of = {f.fact_id: g for g in card.groups for f in g.facts}

    rows = []
    for f in facts:
        g = group_of.get(f.fact_id)
        # 代表事实用带印证加成的置信度，其余用自身公式值
        conf = (g.effective_confidence
                if g and g.head and g.head.fact_id == f.fact_id else f.confidence)
        p = f.payload or {}
        conflict_with = []
        if g and g.has_conflict:
            conflict_with = [key_of[x.fact_id] for x in g.facts
                             if x.fact_id != f.fact_id]
        rows.append({
            "key": key_of[f.fact_id],
            "rid": f.source_id,          # 渲染层用它把事实挂回原文
            "span": f.source_span,       # 抽屉里高亮的那一段
            "fact_type": f.fact_type,
            "statement": f.statement,
            "payload": p,
            "basis": f.confidence_basis,
            "basis_cn": BASIS_CN.get(f.confidence_basis, f.confidence_basis),
            "type_cn": TYPE_CN.get(f.fact_type, f.fact_type),
            "source_cn": rec_map[f.source_id]["type_cn"],
            "source_date": f.stated_at,
            "conf": round(conf, 4),
            "tier": display_tier(conf),
            "status": f.status,
            "expired": f.status == "expired",
            "overdue": _is_overdue(f),
            "date_passed": _date_passed(f),
            "no_quote": f.fact_type == "concern" and not (p.get("verbatim_quote") or ""),
            "conflict": bool(g and g.has_conflict),
            "conflict_with": conflict_with,
        })

    # P0 一律全给，不截断：把「承诺逾期」藏进「更多」里，等于没提醒。
    # P1/P2 补到 4 条就够——一次给八条待办，用户一条都不会做。
    p0 = [a for a in adv.advice if a.priority == "P0"]
    rest = [a for a in adv.advice if a.priority != "P0"]
    shown = p0 + rest[: max(0, 4 - len(p0))]

    actions = []
    for a in shown:
        actions.append({
            "p": a.priority,
            "title": a.title,
            "why": a.why,
            "basis": key_of.get(a.basis[0]) if a.basis else None,
            "owner": "你",
            "rules": a.tags,
            # 18 个 rule_id 各配一句「今天能做的事」，与演示页同源（viewmodel.NEXT_STEP）
            "next_step": next((NEXT_STEP[t] for t in a.tags if t in NEXT_STEP), ""),
        })

    # 档案条与三种简报：与演示页同一条 brief.py 管道，构建时算好内嵌。
    # 有主数据就画主数据，没有就从记录推——判定在 account_strip 里，
    # 这里不再自己 `if not profile`，否则原型和演示页会各用一套规则。
    view = {
        "id": customer["id"], "name": customer["name"],
        "industry": customer.get("industry", ""), "stage": customer.get("stage", ""),
        "facts": rows, "records": customer["records"],
        "summary": card.verdict,
    }
    alive_rows = [r for r in rows if r["status"] == "active" and not r["expired"]]
    strip = account_strip(customer.get("profile"), AS_OF, view)
    briefs = {
        "visit": visit_brief(view, AS_OF),
        "proposal": proposal_brief(view, AS_OF),
        "handover": handover(view, AS_OF, customer.get("profile")),
    }

    return {
        **customer,
        "facts": rows,
        "actions": actions,
        "summary": card.verdict,
        "gaps": card.gaps,
        "stats": card.stats,
        "strip": strip,
        "briefs": briefs,
    }


def main() -> int:
    data = {
        "as_of": AS_OF.isoformat(),
        "customers": [enrich(dict(c)) for c in CUSTOMERS],
        "type_cn": TYPE_CN,
    }
    total = sum(len(c["facts"]) for c in data["customers"])
    acts = sum(len(c["actions"]) for c in data["customers"])

    payload = json.dumps(data, ensure_ascii=False)
    html = (TEMPLATE
            .replace("/*__DATA__*/null", payload)
            .replace("__N_CUST__", str(len(data["customers"])))
            .replace("__N_FACT__", str(total))
            .replace("__N_ACT__", str(acts)))
    OUT.write_text(html, encoding="utf-8")
    print(f"已生成 {OUT}")
    print(f"  {len(data['customers'])} 个客户 · {total} 条事实 · {acts} 条建议")
    for c in data["customers"]:
        tiers = {}
        for f in c["facts"]:
            tiers[f["tier"]] = tiers.get(f["tier"], 0) + 1
        print(f"  {c['name']}: {len(c['records'])} 条记录 / {len(c['facts'])} 条事实 / 档位 {tiers}")
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>客户认知卡 · 原型</title>
<style>
:root{
  --bg:#f5f6f8; --card:#ffffff; --ink:#1a1d21; --muted:#5f6672; --line:#e3e6ea;
  --accent:#1f6feb; --accent-soft:#eaf1fd;
  --ok:#0f7b46; --ok-soft:#e8f5ee;
  --warn:#9a5b00; --warn-soft:#fdf3e3;
  --danger:#b42318; --danger-soft:#fdecea;
  --grey:#6b7280; --grey-soft:#f1f2f4;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.04);
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  color:var(--ink);background:var(--bg);-webkit-font-smoothing:antialiased;
}
.app{display:grid;grid-template-columns:248px 1fr;height:calc(100vh - 30px);overflow:hidden}
/* 顶部身份条：原型容易被误当成已上线的系统，这条必须常在 */
.demobar{height:30px;display:flex;align-items:center;gap:10px;padding:0 14px;
  background:#2b2f36;color:#e8eaed;font-size:12px;letter-spacing:.2px}
.demobar b{color:#ffd66b;font-weight:600}
.demobar span{color:#a8aeb8}

/* ---------- 侧栏 ---------- */
.sidebar{background:#fff;border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.brand{padding:18px 18px 12px;border-bottom:1px solid var(--line)}
.brand b{font-size:15px;letter-spacing:.2px}
.brand span{display:block;color:var(--muted);font-size:11.5px;margin-top:3px}
.search{padding:12px 14px 6px}
.search input{
  width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
  font-size:13px;background:#fbfbfc;color:var(--ink);outline:none
}
.search input:focus{border-color:var(--accent);background:#fff}
.list{overflow:auto;padding:6px 8px 12px;flex:1;min-height:0}
.item{padding:10px 10px;border-radius:8px;cursor:pointer;margin-bottom:2px}
.item:hover{background:#f7f8fa}
.item.on{background:var(--accent-soft)}
.item .n{font-weight:600;font-size:13.5px}
.item .m{color:var(--muted);font-size:11.5px;margin-top:2px;display:flex;gap:6px;flex-wrap:wrap}
.dot{width:6px;height:6px;border-radius:50%;display:inline-block;margin-top:6px}
.foot{padding:10px 16px;border-top:1px solid var(--line);color:var(--muted);font-size:11px}

/* ---------- 主区 ---------- */
main{overflow:auto;min-height:0}
.head{padding:20px 28px 14px;background:#fff;border-bottom:1px solid var(--line)}
.head h1{margin:0;font-size:20px;letter-spacing:.3px}
.head .meta{color:var(--muted);font-size:12.5px;margin-top:6px;display:flex;gap:14px;flex-wrap:wrap}
.verdict{
  margin-top:12px;padding:10px 12px;background:var(--accent-soft);border-left:3px solid var(--accent);
  border-radius:0 6px 6px 0;font-size:13px
}
.tabs{display:flex;gap:4px;padding:0 24px;background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.tab{padding:11px 14px;font-size:13px;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent}
.tab.on{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.panel{padding:20px 28px 60px}

h2.sec{font-size:12px;letter-spacing:1px;color:var(--muted);text-transform:uppercase;margin:26px 0 10px;font-weight:700}
h2.sec:first-child{margin-top:4px}

/* ---------- 建议卡 ---------- */
.actions{display:grid;gap:10px}
.act{
  background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;
  box-shadow:var(--shadow);display:grid;grid-template-columns:44px 1fr;gap:12px
}
.act .p{
  font-size:11px;font-weight:700;letter-spacing:.5px;align-self:start;
  padding:2px 0;text-align:center;border-radius:6px
}
.p0{color:var(--danger);background:var(--danger-soft)}
.p1{color:var(--warn);background:var(--warn-soft)}
.act .t{font-weight:600;font-size:14px}
.act .w{color:var(--muted);font-size:12.5px;margin-top:3px}
.act .o{font-size:11.5px;color:var(--accent);margin-top:5px}

/* ---------- 事实卡 ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.fact{
  background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;
  box-shadow:var(--shadow);position:relative;overflow:hidden
}
.fact::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--grey)}
.fact.t-need::before{background:#1f6feb}
.fact.t-concern::before{background:#b42318}
.fact.t-commitment::before{background:#7a5af8}
.fact.t-stakeholder::before{background:#0f7b46}
.fact.t-competition::before{background:#9a5b00}
.fact.t-timeline::before{background:#0e7490}
.fact.t-risk_signal::before{background:#c2410c}
.fact.dim{opacity:.66}
.fh{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.tag{font-size:11px;font-weight:600;padding:1px 7px;border-radius:20px;background:var(--grey-soft);color:var(--muted)}
.badge{font-size:10.5px;padding:1px 6px;border-radius:4px;font-weight:600;letter-spacing:.2px}
.b-ok{background:var(--ok-soft);color:var(--ok)}
.b-mid{background:var(--warn-soft);color:var(--warn)}
.b-low{background:var(--grey-soft);color:var(--grey)}
.b-exp{background:#eef0f2;color:#495057}
.b-ovd{background:var(--danger-soft);color:var(--danger)}
.b-cft{background:var(--danger-soft);color:var(--danger)}
.st{margin:2px 0 8px;font-size:13.5px;line-height:1.55}
blockquote{
  margin:0 0 8px;padding:6px 10px;border-left:2px solid var(--line);
  color:#404650;font-size:12.5px;background:#fafbfc;border-radius:0 4px 4px 0
}
.ff{display:flex;justify-content:space-between;align-items:center;font-size:11.5px;color:var(--muted)}
.ff button{
  border:1px solid var(--line);background:#fff;color:var(--accent);border-radius:6px;
  padding:2px 8px;font-size:11.5px;cursor:pointer
}
.ff button:hover{background:var(--accent-soft);border-color:var(--accent)}
.fact.flash{outline:2px solid var(--accent);outline-offset:2px}

/* ---------- 时间线 ---------- */
.tl{position:relative;margin-left:6px;padding-left:22px;border-left:2px solid var(--line)}
.tli{position:relative;margin-bottom:18px}
.tli::before{
  content:"";position:absolute;left:-29px;top:5px;width:9px;height:9px;border-radius:50%;
  background:#fff;border:2px solid var(--accent)
}
.tli.past::before{border-color:var(--grey)}
.tli .d{font-size:11.5px;color:var(--muted);letter-spacing:.3px}
.tli .c{font-size:13.5px;margin-top:2px}
.tli .r{font-size:11.5px;color:var(--muted);margin-top:2px}

/* ---------- 记录 ---------- */
.rec{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px;box-shadow:var(--shadow)}
.rec .rh{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--muted);margin-bottom:8px}
.rec .rt{font-size:13.5px;line-height:1.75;white-space:pre-wrap}
.rec .rf{margin-top:10px;display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:11px;padding:2px 7px;border-radius:4px;background:var(--grey-soft);color:var(--muted);cursor:pointer}
.chip:hover{background:var(--accent-soft);color:var(--accent)}
mark{background:#fff3b0;padding:0 1px;border-radius:2px}

/* ---------- 抽屉 ---------- */
.mask{position:fixed;inset:0;background:rgba(17,24,39,.28);opacity:0;pointer-events:none;transition:.18s;z-index:20}
.drawer{
  position:fixed;top:0;right:0;bottom:0;width:min(560px,92vw);background:#fff;z-index:21;
  transform:translateX(102%);transition:transform .22s ease;display:flex;flex-direction:column;
  box-shadow:-8px 0 24px rgba(16,24,40,.12)
}
.drawer.on{transform:none}
.mask.on{opacity:1;pointer-events:auto}
.dh{padding:16px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:flex-start}
.dh b{font-size:14px}
.dh .x{border:0;background:none;font-size:20px;line-height:1;cursor:pointer;color:var(--muted)}
.db{padding:18px 20px;overflow:auto;flex:1}
.kv{display:grid;grid-template-columns:78px 1fr;gap:4px 10px;font-size:12.5px;margin-bottom:14px}
.kv .k{color:var(--muted)}
.src{background:#fbfbfc;border:1px solid var(--line);border-radius:8px;padding:12px 14px;font-size:13.5px;line-height:1.85;white-space:pre-wrap}
.hint{font-size:12px;color:var(--muted);margin-top:10px}
.empty{color:var(--muted);font-size:13px;padding:16px;background:#fff;border:1px dashed var(--line);border-radius:10px}

/* ---------- 档案条 ---------- */
.strip{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.stripi{background:#fff;border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:12px}
.stripi b{font-size:12.5px;color:var(--ink);margin-right:6px}
.stripi span{color:var(--muted)}

/* ---------- 场景简报 ---------- */
.bsel{display:flex;gap:8px;margin:4px 0 16px;flex-wrap:wrap}
.bsel button{
  padding:8px 16px;border:1px solid var(--line);border-radius:8px;background:#fff;
  color:var(--ink);font-size:13px;cursor:pointer;font-weight:600
}
.bsel button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.bhead{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:14px;box-shadow:var(--shadow)}
.bhead b{font-size:14.5px}
.bhead .sub{color:var(--muted);font-size:12px;margin-top:4px}
.bblock{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:10px;box-shadow:var(--shadow)}
.bblock h3{margin:0 0 8px;font-size:13px;color:var(--accent)}
.bblock .bi{font-size:13.5px;line-height:1.6;padding:4px 0;border-bottom:1px dashed var(--line)}
.bblock .bi:last-child{border-bottom:0}
.bblock .bm{font-size:11.5px;color:var(--muted);margin-top:1px}
.chainline{background:var(--accent-soft);border-radius:8px;padding:8px 12px;font-size:12.5px;margin-bottom:12px}
</style>
</head>
<body>
<div class="demobar">
  <b>可点击原型</b>
  <span>以下 __N_CUST__ 家客户、__N_FACT__ 条事实、__N_ACT__ 条建议均为演示素材；归并 / 冲突 / 过期 / 结论 / 建议 / 简报由 src 与 demo 引擎真实计算；无后端，不能写入。完整交互版（时间机器 / 上传 / 实时抽取 / 撤回）：python3 demo/serve.py</span>
</div>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><b>客户认知助手</b><span>原型 v0.2 · 数据截至 2026-10-25</span></div>
    <div class="search"><input id="q" placeholder="搜索客户名称…" autocomplete="off"></div>
    <div class="list" id="list"></div>
    <div style="padding:8px 10px"><button id="todoBtn" style="width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:var(--accent-soft);color:var(--accent);font-weight:600;font-size:13px;cursor:pointer">☰ 今天要做的事（跨客户）</button></div>
    <div class="foot">演示数据：文本与事实为人工整理，置信度 / 保鲜期 / 简报由引擎计算</div>
  </aside>
  <main>
    <div class="head">
      <h1 id="cname"></h1>
      <div class="meta" id="cmeta"></div>
      <div class="verdict" id="verdict"></div>
    </div>
    <nav class="tabs" id="tabs">
      <div class="tab on" data-tab="card">认知卡</div>
      <div class="tab" data-tab="briefs">场景简报</div>
      <div class="tab" data-tab="timeline">时间线</div>
      <div class="tab" data-tab="records">全部记录</div>
    </nav>
    <div class="panel" id="panel"></div>
  </main>
</div>

<div class="mask" id="mask"></div>
<div class="drawer" id="drawer">
  <div class="dh"><b id="dtitle">原文</b><button class="x" id="dclose">×</button></div>
  <div class="db" id="dbody"></div>
</div>

<script>
const DATA = /*__DATA__*/null;
let cur = DATA.customers[0];
let tab = 'card';

const el = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function tierBadge(f){
  const map = {show:['b-ok','高置信'], confirm:['b-mid','待确认'], candidate:['b-low','候选']};
  const [cls,label] = map[f.tier] || map.candidate;
  return `<span class="badge ${cls}">${label} ${f.conf.toFixed(2)}</span>`;
}

function factCard(f){
  const flags = [];
  if (f.overdue) flags.push('<span class="badge b-ovd">承诺逾期</span>');
  if (f.date_passed) flags.push('<span class="badge b-ovd">节点已过</span>');
  if (f.expired) flags.push('<span class="badge b-exp">已过保鲜期</span>');
  if (f.conflict) {
    const t = f.conflict_with && f.conflict_with.length ? f.conflict_with[0] : '';
    flags.push(`<span class="badge b-cft" style="cursor:pointer" onclick="jump('${t}')">与另一条冲突 · 去对比</span>`);
  }
  if (f.no_quote) flags.push('<span class="badge b-mid">无原话·待确认</span>');
  const quote = f.fact_type === 'concern' && f.payload.verbatim_quote
    ? `<blockquote>“${esc(f.payload.verbatim_quote)}”</blockquote>` : '';
  return `<article class="fact t-${f.fact_type} ${f.expired ? 'dim' : ''}" id="${f.key}">
    <div class="fh">
      <span class="tag">${f.type_cn}</span>${tierBadge(f)}
      <span class="tag">依据：${f.basis_cn}</span>${flags.join('')}
    </div>
    <p class="st">${esc(f.statement)}</p>
    ${quote}
    <div class="ff">
      <span>${f.source_cn} · ${f.source_date}</span>
      <button onclick="openRec('${cur.id}','${f.rid}','${esc(f.span).replace(/'/g,'')}')">看原文</button>
    </div>
  </article>`;
}

function renderCard(c){
  const order = ['need','concern','commitment','timeline','competition','stakeholder','risk_signal'];
  let html = '<h2 class="sec">现在最该做的事</h2><div class="actions">';
  if (!c.actions.length) html += '<div class="empty">没有待办。这个客户暂时不需要动作。</div>';
  for (const a of c.actions){
    html += `<div class="act">
      <div class="p ${a.p.toLowerCase()}">${a.p}</div>
      <div><div class="t">${esc(a.title)}</div><div class="w">${esc(a.why)}</div>
      <div class="o">负责人：${esc(a.owner)}${a.basis ? `　·　<a href="#" onclick="jump('${a.basis}');return false" style="color:inherit">跳到依据事实</a>` : ''}</div>
      ${a.next_step ? `<div class="w" style="color:var(--accent)">→ ${esc(a.next_step)}</div>` : ''}</div>
    </div>`;
  }
  html += '</div>';

  for (const t of order){
    const fs = c.facts.filter(f => f.fact_type === t);
    if (!fs.length) continue;
    html += `<h2 class="sec">${DATA.type_cn[t]} · ${fs.length}</h2><div class="grid">` + fs.map(factCard).join('') + '</div>';
  }
  return html;
}

function renderTimeline(c){
  const items = [];
  for (const f of c.facts){
    const d = f.fact_type === 'timeline' ? f.payload.target_date : f.source_date;
    items.push({d, f});
  }
  items.sort((a,b) => a.d < b.d ? -1 : 1);
  let html = '<h2 class="sec">按时间看这个客户</h2><div class="tl">';
  for (const it of items){
    const past = it.d < DATA.as_of;
    const label = it.f.fact_type === 'timeline' ? '计划节点' : '事实发生';
    html += `<div class="tli ${past ? 'past' : ''}">
      <div class="d">${it.d} · ${label}${past ? ' · 已过去' : ' · 未来'}</div>
      <div class="c">${esc(it.f.statement)}</div>
      <div class="r">${it.f.type_cn} · 来源 ${it.f.source_cn}（${it.f.source_date}）· 置信度 ${it.f.conf.toFixed(2)}</div>
    </div>`;
  }
  html += '</div><div class="hint">说明：时间线按「事件时间」排；补录的记录不会因入库晚而挤到后面。</div>';
  return html;
}

function renderRecords(c){
  let html = '<h2 class="sec">全部原始记录 · ' + c.records.length + '</h2>';
  for (const r of c.records){
    const fs = c.facts.filter(f => f.rid === r.id);
    html += `<div class="rec">
      <div class="rh"><span>${r.type_cn} · ${r.date}</span><span>抽出 ${fs.length} 条事实</span></div>
      <div class="rt">${esc(r.text)}</div>
      <div class="rf">${fs.map(f => `<span class="chip" onclick="openRec('${c.id}','${r.id}','${esc(f.span).replace(/'/g,'')}')">${f.type_cn}</span>`).join('')}</div>
    </div>`;
  }
  return html;
}

function renderBriefs(c){
  const kinds = [['visit','拜访简报'],['proposal','方案准备'],['handover','交接说明']];
  if (renderBriefs.kind == null) renderBriefs.kind = 'visit';
  const b = c.briefs[renderBriefs.kind] || Object.values(c.briefs)[0];
  let html = `<div class="bsel">` + kinds.map(([k,label]) =>
    `<button class="${k===renderBriefs.kind?'on':''}" onclick="renderBriefs.kind='${k}';render()">${label}</button>`).join('') + `</div>`;
  html += `<div class="bhead"><b>${esc(b.title)}</b><div class="sub">${esc(b.subtitle)}</div></div>`;
  if (b.chain && (b.chain.nodes || b.chain.missing)){
    const nodes = (b.chain.nodes||[]).map(n => `${n.name || '未具名'}（${n.role || '?'}）`).join(' → ');
    const miss = (b.chain.missing||[]);
    html += `<div class="chainline">决策链：${nodes || '（暂无关键人记录）'}` +
      (miss.length ? `　·　<b>缺：${miss.map(esc).join('、')}</b>` : '　·　链路完整') + `</div>`;
  }
  if (b.empty) html += '<div class="empty">这个客户暂无有效事实，简报为空——系统明说没有，不硬凑。</div>';
  for (const blk of (b.blocks || [])){
    html += `<div class="bblock"><h3>${esc(blk.h)}</h3>` +
      (blk.items || []).map(it => `<div class="bi">${esc(it.text)}${it.meta ? `<div class="bm">${esc(it.meta)}</div>` : ''}</div>`).join('') +
      `</div>`;
  }
  html += '<div class="hint">简报由事实拼装生成，一行模型不调；每一条都能在「全部记录」里点回原文。</div>';
  return html;
}

function render(){
  el('cname').textContent = cur.name;
  el('cmeta').innerHTML = `<span>${cur.industry}</span><span>${cur.stage}</span><span>客户负责人：${cur.owner}</span>
    <span>${cur.records.length} 条记录 · ${cur.facts.length} 条事实</span>`;
  el('verdict').innerHTML = `<b>一句话结论：</b>${esc(cur.summary)}` +
    (cur.strip && cur.strip.length
      ? `<div class="strip">` + cur.strip.map(s =>
          `<span class="stripi"><b>${esc(s.label)}</b><span>${esc(s.value)}${s.hint ? ` · ${esc(s.hint)}` : ''}</span></span>`).join('') + `</div>`
      : '');
  el('panel').innerHTML =
    tab === 'card' ? renderCard(cur) :
    tab === 'briefs' ? renderBriefs(cur) :
    tab === 'timeline' ? renderTimeline(cur) : renderRecords(cur);
  document.querySelectorAll('.item').forEach(n => n.classList.toggle('on', n.dataset.id === cur.id));
}

function openTodos(){
  const rows = [];
  for (const c of DATA.customers){
    for (const a of c.actions){
      rows.push({c, a});
    }
  }
  const rank = {P0: 0, P1: 1, P2: 2};
  rows.sort((x,y) => (rank[x.a.p] ?? 9) - (rank[y.a.p] ?? 9));
  el('dtitle').textContent = `今天要做的事 · 跨 ${DATA.customers.length} 个客户 · ${rows.length} 件`;
  let html = rows.length ? '' : '<div class="empty">所有客户都没有待办。宁可少给，不硬凑。</div>';
  for (const {c, a} of rows){
    html += `<div class="act" style="margin-bottom:10px">
      <div class="p ${a.p.toLowerCase()}">${a.p}</div>
      <div><div class="t">${esc(c.name)} · ${esc(a.title)}</div><div class="w">${esc(a.why)}</div>
      ${a.next_step ? `<div class="w" style="color:var(--accent)">→ ${esc(a.next_step)}</div>` : ''}
      <div class="o"><a href="#" onclick="closeRec();sel('${c.id}');tab='card';document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x.dataset.tab==='card'));render();return false" style="color:inherit">去处理 →</a></div></div>
    </div>`;
  }
  el('dbody').innerHTML = html;
  el('drawer').classList.add('on');
  el('mask').classList.add('on');
}

function renderList(q=''){
  el('list').innerHTML = DATA.customers
    .filter(c => !q || c.name.includes(q))
    .map(c => `<div class="item" data-id="${c.id}" onclick="sel('${c.id}')">
      <div class="n">${c.name}</div>
      <div class="m"><span>${c.stage}</span><span>·</span><span>${c.facts.length} 条事实</span></div>
    </div>`).join('') || '<div class="empty" style="margin:10px">没有匹配的客户</div>';
}

function sel(id){
  cur = DATA.customers.find(c => c.id === id);
  render();
}
function jump(key){
  const n = document.getElementById(key);
  if (!n) return;
  n.scrollIntoView({behavior:'smooth', block:'center'});
  n.classList.add('flash');
  setTimeout(()=>n.classList.remove('flash'), 1400);
}

function openRec(cid, rid, span){
  const c = DATA.customers.find(x => x.id === cid);
  const r = c.records.find(x => x.id === rid);
  const fs = c.facts.filter(f => f.rid === rid);
  el('dtitle').textContent = `${r.type_cn} · ${r.date}`;
  const body = esc(r.text);
  const marked = span && body.includes(esc(span)) ? body.replace(esc(span), `<mark>${esc(span)}</mark>`) : body;
  el('dbody').innerHTML = `
    <div class="kv">
      <div class="k">来源类型</div><div>${r.type_cn}</div>
      <div class="k">事件时间</div><div>${r.date}</div>
      <div class="k">客户</div><div>${c.name}</div>
    </div>
    <div class="src">${marked}</div>
    <div class="hint">高亮部分是这条事实的溯源片段。原文没写的东西，系统不会替你补。</div>
    <h2 class="sec">这条记录抽出的事实</h2>
    <div class="grid">${fs.map(factCard).join('')}</div>`;
  el('drawer').classList.add('on');
  el('mask').classList.add('on');
}
function closeRec(){
  el('drawer').classList.remove('on');
  el('mask').classList.remove('on');
}

el('mask').onclick = closeRec;
el('dclose').onclick = closeRec;
el('todoBtn').onclick = openTodos;
el('q').oninput = e => renderList(e.target.value);
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  tab = t.dataset.tab;
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === t));
  render();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeRec(); });

renderList();
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
