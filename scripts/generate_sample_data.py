"""Generate the sample dataset under ``data/samples``.

The generated CSVs are a *realistic scenario* rather than random noise: an
anchor company with a multi-level guarantee chain, subsidiaries with
ownership links, the people who run them, and the adverse events that make
the chain worth looking at.

The generator is deterministic, so the committed sample files and the
regenerated ones are identical.

Usage::

    python scripts/generate_sample_data.py --output data/samples
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

COMPANY_FIELDS = [
    "id",
    "name",
    "credit_code",
    "reg_capital",
    "industry",
    "status",
    "founded_date",
    "region",
]

PERSON_FIELDS = ["id", "name", "role"]

EVENT_FIELDS = [
    "id",
    "name",
    "type",
    "severity",
    "date",
    "amount",
    "court",
    "case_number",
    "description",
]

PRODUCT_FIELDS = ["id", "name", "type", "issuer_id", "amount", "issue_date", "rating"]

RELATION_FIELDS = ["id", "type", "source", "target", "percent", "guaranteed_amount"]

#: The scenario. ``group`` marks the companies that belong to the same
#: guarantee cluster — the "担保链穿透" demo case.
SCENARIO: list[dict[str, object]] = [
    {
        "id": "C001",
        "name": "华信控股集团有限公司",
        "credit_code": "91110000MA01ABCD1X",
        "reg_capital": "500000",
        "industry": "综合投资",
        "status": "存续",
        "region": "上海",
        "founded": "2008-03-18",
        "group": "G1",
    },
    {
        "id": "C002",
        "name": "华信置业发展有限公司",
        "credit_code": "91110000MA01ABCD2Y",
        "reg_capital": "120000",
        "industry": "房地产开发",
        "status": "存续",
        "region": "上海",
        "founded": "2011-07-02",
        "group": "G1",
    },
    {
        "id": "C003",
        "name": "华信供应链管理有限公司",
        "credit_code": "91110000MA01ABCD3Z",
        "reg_capital": "30000",
        "industry": "贸易",
        "status": "存续",
        "region": "上海",
        "founded": "2014-11-25",
        "group": "G1",
    },
    {
        "id": "C004",
        "name": "中信创业投资有限公司",
        "credit_code": "91310000MA01ABCD4A",
        "reg_capital": "80000",
        "industry": "创业投资",
        "status": "存续",
        "region": "上海",
        "founded": "2010-05-11",
        "group": "G1",
    },
    {
        "id": "C005",
        "name": "新盛贸易有限公司",
        "credit_code": "91440300MA01ABCD5B",
        "reg_capital": "5000",
        "industry": "批发零售",
        "status": "存续",
        "region": "深圳",
        "founded": "2016-09-08",
        "group": "G2",
    },
    {
        "id": "C006",
        "name": "新盛实业有限公司",
        "credit_code": "91440300MA01ABCD6C",
        "reg_capital": "2000",
        "industry": "制造业",
        "status": "存续",
        "region": "深圳",
        "founded": "2017-04-19",
        "group": "G2",
    },
    {
        "id": "C007",
        "name": "恒基建筑工程有限公司",
        "credit_code": "91330100MA01ABCD7D",
        "reg_capital": "15000",
        "industry": "土木工程建筑",
        "status": "存续",
        "region": "杭州",
        "founded": "2012-12-03",
        "group": "G2",
    },
    {
        "id": "C008",
        "name": "远达物流有限公司",
        "credit_code": "91320100MA01ABCD8E",
        "reg_capital": "8000",
        "industry": "仓储物流",
        "status": "存续",
        "region": "南京",
        "founded": "2015-02-14",
        "group": "G3",
    },
    {
        "id": "C009",
        "name": "远达能源科技有限公司",
        "credit_code": "91320500MA01ABCD9F",
        "reg_capital": "60000",
        "industry": "能源科技",
        "status": "存续",
        "region": "苏州",
        "founded": "2013-08-21",
        "group": "G3",
    },
    {
        "id": "C010",
        "name": "明德教育咨询有限公司",
        "credit_code": "91110108MA01ABCDA0",
        "reg_capital": "1000",
        "industry": "教育咨询",
        "status": "存续",
        "region": "北京",
        "founded": "2018-06-30",
        "group": "G4",
    },
    {
        "id": "C011",
        "name": "宏远医疗器械有限公司",
        "credit_code": "91440300MA01ABCDB1",
        "reg_capital": "4500",
        "industry": "医疗器械",
        "status": "吊销",
        "region": "深圳",
        "founded": "2015-10-09",
        "group": "G4",
    },
    {
        "id": "C012",
        "name": "正和资产管理有限公司",
        "credit_code": "91310000MA01ABCDC2",
        "reg_capital": "100000",
        "industry": "资产管理",
        "status": "存续",
        "region": "上海",
        "founded": "2010-01-15",
        "group": "G1",
    },
]

PEOPLE = [
    ("P001", "陈建国", "法定代表人"),
    ("P002", "李慧敏", "法定代表人"),
    ("P003", "王志强", "法定代表人"),
    ("P004", "赵雅芳", "董事"),
    ("P005", "孙立群", "实际控制人"),
    ("P006", "周天明", "法定代表人"),
    ("P007", "吴丽华", "法定代表人"),
    ("P008", "郑海峰", "法定代表人"),
]

PRODUCTS = [
    ("PR001", "华信控股2023年第一期公司债", "公司债券", "C001", "500000000", "2023-03-15", "AA+"),
    ("PR002", "华信控股2024年第一期中期票据", "中期票据", "C001", "300000000", "2024-04-20", "AA"),
    ("PR003", "中信创业成长一期基金", "私募基金", "C004", "80000000", "2022-09-01", None),
    (
        "PR004",
        "新盛实业2023年定向资产支持证券",
        "资产支持证券",
        "C006",
        "120000000",
        "2023-06-30",
        "A",
    ),
]


def _credit_code(seed: str) -> str:
    """Build a deterministic, syntactically valid 18-character credit code."""
    import hashlib

    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest().upper()
    body = "".join(ch for ch in digest if ch.isdigit() or ch.isalpha())[:17]
    return f"91{body}"


def generate(output: Path, *, seed: int = 20260301) -> dict[str, int]:
    """Write the sample CSV files and return the record counts."""
    random.Random(seed)
    output.mkdir(parents=True, exist_ok=True)

    companies = [
        {
            "id": row["id"],
            "name": row["name"],
            "credit_code": row["credit_code"],
            "reg_capital": row["reg_capital"],
            "industry": row["industry"],
            "status": row["status"],
            "founded_date": row["founded"],
            "region": row["region"],
        }
        for row in SCENARIO
    ]

    people = [{"id": pid, "name": name, "role": role} for pid, name, role in PEOPLE]

    events: list[dict[str, object]] = []
    relations: list[dict[str, object]] = []
    today = date.today()

    # --- the guarantee chain that the demo is built around -----------------
    chain = [
        ("C001", "C002", 150000),
        ("C002", "C003", 80000),
        ("C003", "C004", 45000),
        ("C002", "C007", 60000),
        ("C001", "C006", 30000),
        ("C005", "C006", 12000),
        ("C007", "C008", 20000),
        ("C009", "C008", 35000),
        ("C012", "C001", 90000),
        ("C001", "C011", 25000),
        ("C004", "C010", 5000),
    ]
    for index, (source, target, amount) in enumerate(chain):
        relations.append(
            {
                "id": f"R-GUA-{index:03d}",
                "type": "GUARANTEE",
                "source": source,
                "target": target,
                "percent": "",
                "guaranteed_amount": amount,
            }
        )

    # --- ownership ---------------------------------------------------------
    ownership = [
        ("C001", "C002", 1.0),
        ("C001", "C003", 0.85),
        ("C001", "C004", 0.6),
        ("C001", "C011", 0.51),
        ("C012", "C001", 0.45),
        ("C004", "C005", 0.7),
        ("C005", "C006", 0.55),
        ("C007", "C008", 0.65),
        ("C009", "C010", 0.4),
    ]
    for index, (source, target, percent) in enumerate(ownership):
        relations.append(
            {
                "id": f"R-HOLD-{index:03d}",
                "type": "HOLD",
                "source": source,
                "target": target,
                "percent": percent,
                "guaranteed_amount": "",
            }
        )

    # --- people ------------------------------------------------------------
    for index, (pid, _, _) in enumerate(PEOPLE):
        company_id = SCENARIO[index % len(SCENARIO)]["id"]
        relations.append(
            {
                "id": f"R-LEGAL-{index:03d}",
                "type": "LEGAL_REP",
                "source": pid,
                "target": company_id,
                "percent": "",
                "guaranteed_amount": "",
            }
        )

    # --- adverse events ----------------------------------------------------
    event_specs = [
        (
            "C011",
            "医疗器械召回",
            "行政处罚",
            "较大",
            120,
            50000,
            "广东省药品监督管理局",
            "穗药监罚〔2025〕18号",
            "因产品召回问题被处以行政处罚",
        ),
        (
            "C011",
            "涉诉",
            "涉诉",
            "重大",
            240,
            86000,
            "深圳市中级人民法院",
            "(2025)粤01民初1234号",
            "买卖合同纠纷，标的金额较大",
        ),
        (
            "C006",
            "被执行",
            "强制执行",
            "重大",
            90,
            35000,
            "深圳南山区法院",
            "(2025)粤0305执88号",
            "买卖合同纠纷进入强制执行程序",
        ),
        (
            "C005",
            "欠税",
            "欠税",
            "一般",
            45,
            12000,
            "深圳市税务局",
            "深税稽通〔2025〕22号",
            "欠缴税款未按期申报",
        ),
        (
            "C003",
            "负面舆情",
            "负面舆情",
            "一般",
            15,
            None,
            None,
            None,
            "被报道资金链紧张，供应商上门催款",
        ),
        (
            "C001",
            "评级下调",
            "评级下调",
            "较大",
            200,
            None,
            None,
            None,
            "主体信用评级由 AA+ 下调至 AA",
        ),
        (
            "C009",
            "股权冻结",
            "股权冻结",
            "较大",
            60,
            28000,
            "苏州市中级人民法院",
            "(2025)苏05执701号",
            "部分股权被司法冻结",
        ),
        (
            "C002",
            "经营异常",
            "经营异常",
            "一般",
            30,
            None,
            None,
            None,
            "因地址无法联系被列入经营异常名录",
        ),
        ("C004", "违约", "违约", "重大", 10, 45000, None, None, "一笔私募基金份额兑付逾期"),
        (
            "C012",
            "行政处罚",
            "行政处罚",
            "较大",
            150,
            20000,
            "中国证监会上海监管局",
            "沪证监决〔2025〕9号",
            "私募基金管理存在不规范情形",
        ),
        (
            "C007",
            "被执行",
            "强制执行",
            "较大",
            75,
            52000,
            "杭州滨江区法院",
            "(2025)浙0108执332号",
            "建设工程施工合同纠纷被强制执行",
        ),
        (
            "C008",
            "欠税",
            "欠税",
            "一般",
            25,
            8000,
            "南京市税务局",
            "宁税稽通〔2025〕7号",
            "欠缴税款",
        ),
        ("C010", "负面舆情", "负面舆情", "一般", 8, None, None, None, "被家长投诉办学资质问题"),
        ("C011", "经营异常", "经营异常", "一般", 180, None, None, None, "因登记住所失联被列入异常"),
    ]
    for index, (company, name, kind, severity, days, amount, court, case_no, note) in enumerate(
        event_specs
    ):
        event_id = f"E{index + 1:03d}"
        events.append(
            {
                "id": event_id,
                "name": f"{company}-{name}",
                "type": kind,
                "severity": severity,
                "date": (today - timedelta(days=days)).isoformat(),
                "amount": amount if amount is not None else "",
                "court": court or "",
                "case_number": case_no or "",
                "description": note,
            }
        )
        relations.append(
            {
                "id": f"R-INV-{index:03d}",
                "type": "INVOLVED_IN",
                "source": company,
                "target": event_id,
                "percent": "",
                "guaranteed_amount": "",
            }
        )

    # --- products ----------------------------------------------------------
    products = [
        {
            "id": pid,
            "name": name,
            "type": kind,
            "issuer_id": issuer,
            "amount": amount,
            "issue_date": issue,
            "rating": rating or "",
        }
        for pid, name, kind, issuer, amount, issue, rating in PRODUCTS
    ]
    for index, product in enumerate(products):
        relations.append(
            {
                "id": f"R-ISS-{index:03d}",
                "type": "ISSUE",
                "source": str(product["issuer_id"]),
                "target": str(product["id"]),
                "percent": "",
                "guaranteed_amount": "",
            }
        )

    # --- write -------------------------------------------------------------
    _write(output / "companies.csv", COMPANY_FIELDS, companies)
    _write(output / "persons.csv", PERSON_FIELDS, people)
    _write(output / "events.csv", EVENT_FIELDS, events)
    _write(output / "products.csv", PRODUCT_FIELDS, products)
    _write(output / "relations.csv", RELATION_FIELDS, relations)

    return {
        "companies": len(companies),
        "persons": len(people),
        "events": len(events),
        "products": len(products),
        "relations": len(relations),
    }


def _write(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    """Write one CSV file with a UTF-8 BOM, so Excel opens it correctly."""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Generate the sample dataset")
    parser.add_argument("--output", default="data/samples")
    parser.add_argument("--seed", type=int, default=20260301)
    args = parser.parse_args()

    counts = generate(Path(args.output), seed=args.seed)
    for name, count in counts.items():
        print(f"{name:<12} {count}")
    print(f"\nwritten to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
