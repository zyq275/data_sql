"""数仓库（dw）建表与样例数据生成（阶段 4）。

    python -m app.scripts.seed_dw                 # 建库建表 + 重造样例数据（幂等：DROP 后重建）
    python -m app.scripts.seed_dw --no-drop       # 表已存在时跳过建表，只补数据
    python -m app.scripts.seed_dw --create-account  # 额外创建只读账号（需 DB_META 具备 GRANT 权限）

为什么要造样例数据：dw 是 Text-to-SQL 的查询目标，TDD 8.2 只给了表结构，
没有数据则「召回能命中对应项」「SQL 执行返回正确结果」都无从验证。
样例数据用固定随机种子生成，保证可复现（同一份数据反复构建得到同样结果）。

建表 DDL 必须与 conf/meta_config.yaml 的声明一一对应，两侧不一致会导致
构建出的知识库指向不存在的字段。build_meta.py 结束时会做一次校验。

注意：本脚本用 db_meta 的账号（具备 DDL 权限）操作同一台 MySQL 实例；
应用运行期访问 dw 应使用 DB_DW__USER 指定的只读账号（ADR-008 / NFR-SEC-007）。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import random
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings

# --------------------------------------------------------------------------
# DDL（TDD 8.2 数仓表）
# --------------------------------------------------------------------------
# year / month / day 是 MySQL 的保留字/函数名，DDL 与查询中都必须加反引号。

TABLE_DDL: dict[str, str] = {
    "dim_region": """
        CREATE TABLE dim_region (
            region_id   INT          NOT NULL COMMENT '地区主键',
            province    VARCHAR(32)  NOT NULL COMMENT '省份名称',
            region_name VARCHAR(32)  NOT NULL COMMENT '大区名称',
            country     VARCHAR(32)  NOT NULL DEFAULT '中国' COMMENT '所属国家',
            PRIMARY KEY (region_id),
            KEY idx_region_name (region_name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='地区维度表'
    """,
    "dim_customer": """
        CREATE TABLE dim_customer (
            customer_id   INT          NOT NULL COMMENT '客户主键',
            customer_name VARCHAR(64)  NOT NULL COMMENT '客户姓名',
            gender        VARCHAR(8)   NULL COMMENT '性别：男/女',
            member_level  VARCHAR(16)  NULL COMMENT '会员等级：普通/银卡/金卡/钻石',
            PRIMARY KEY (customer_id),
            KEY idx_member_level (member_level)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客户维度表'
    """,
    "dim_product": """
        CREATE TABLE dim_product (
            product_id   INT          NOT NULL COMMENT '商品主键',
            product_name VARCHAR(128) NOT NULL COMMENT '商品名称',
            category     VARCHAR(64)  NULL COMMENT '一级类目',
            brand        VARCHAR(64)  NULL COMMENT '品牌',
            PRIMARY KEY (product_id),
            KEY idx_category (category)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品维度表'
    """,
    "dim_date": """
        CREATE TABLE dim_date (
            date_id INT NOT NULL COMMENT '日期主键，格式 yyyymmdd',
            `year`  INT NOT NULL COMMENT '自然年',
            quarter INT NOT NULL COMMENT '自然季度 1-4',
            `month` INT NOT NULL COMMENT '自然月 1-12',
            `day`   INT NOT NULL COMMENT '月内第几天',
            PRIMARY KEY (date_id),
            KEY idx_year_month (`year`, `month`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日期维度表'
    """,
    "fact_order": """
        CREATE TABLE fact_order (
            order_id       BIGINT        NOT NULL COMMENT '订单主键',
            customer_id    INT           NOT NULL COMMENT '客户ID -> dim_customer',
            product_id     INT           NOT NULL COMMENT '商品ID -> dim_product',
            date_id        INT           NOT NULL COMMENT '下单日期 -> dim_date',
            region_id      INT           NOT NULL COMMENT '地区ID -> dim_region',
            order_quantity INT           NOT NULL COMMENT '购买件数',
            order_amount   DECIMAL(18,2) NOT NULL COMMENT '订单金额（元，不含运费）',
            order_status   VARCHAR(16)   NOT NULL COMMENT '订单状态：已支付/已取消/已退款',
            PRIMARY KEY (order_id),
            KEY idx_customer_id (customer_id),
            KEY idx_product_id (product_id),
            KEY idx_date_id (date_id),
            KEY idx_region_id (region_id),
            KEY idx_order_status (order_status),
            CONSTRAINT fk_order_customer FOREIGN KEY (customer_id) REFERENCES dim_customer (customer_id),
            CONSTRAINT fk_order_product  FOREIGN KEY (product_id)  REFERENCES dim_product (product_id),
            CONSTRAINT fk_order_date     FOREIGN KEY (date_id)     REFERENCES dim_date (date_id),
            CONSTRAINT fk_order_region   FOREIGN KEY (region_id)   REFERENCES dim_region (region_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单事实表'
    """,
}

# 建表顺序需满足外键依赖；删表则相反。
TABLE_ORDER = ("dim_region", "dim_customer", "dim_product", "dim_date", "fact_order")

# --------------------------------------------------------------------------
# 样例数据
# --------------------------------------------------------------------------

REGIONS: dict[str, list[str]] = {
    "华东": ["上海市", "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省"],
    "华南": ["广东省", "广西壮族自治区", "海南省"],
    "华北": ["北京市", "天津市", "河北省", "山西省", "内蒙古自治区"],
    "华中": ["河南省", "湖北省", "湖南省"],
    "西南": ["四川省", "重庆市", "云南省", "贵州省"],
    "西北": ["陕西省", "甘肃省", "新疆维吾尔自治区"],
    "东北": ["辽宁省", "吉林省", "黑龙江省"],
}

SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张"
GIVEN = ["建国", "淑芬", "伟", "敏", "静", "磊", "洋", "勇", "艳", "杰", "涛", "明", "超", "霞", "平", "刚"]

PRODUCTS: list[tuple[str, str, str]] = [
    ("智享 X5 智能手机", "手机数码", "星辰"),
    ("锐驰 T3 平板电脑", "手机数码", "星辰"),
    ("悦动 A2 蓝牙耳机", "手机数码", "悦动"),
    ("磐石 K7 笔记本电脑", "电脑办公", "磐石"),
    ("磐石 M1 显示器", "电脑办公", "磐石"),
    ("清风 S1 空气净化器", "家用电器", "清风"),
    ("清風 B2 变频空调", "家用电器", "清风"),
    ("暖阳 H3 电热水器", "家用电器", "暖阳"),
    ("冰川 R4 双门冰箱", "家用电器", "冰川"),
    ("云居 C5 扫地机器人", "家用电器", "云居"),
    ("轻语 F6 电动牙刷", "个护健康", "轻语"),
    ("轻语 G7 电动剃须刀", "个护健康", "轻语"),
    ("山行 J8 户外冲锋衣", "服饰鞋包", "山行"),
    ("山行 K9 徒步鞋", "服饰鞋包", "山行"),
    ("墨白 L1 真皮双肩包", "服饰鞋包", "墨白"),
    ("拾光 P2 全自动咖啡机", "厨房电器", "拾光"),
    ("拾光 Q3 破壁料理机", "厨房电器", "拾光"),
    ("炊烟 R5 电饭煲", "厨房电器", "炊烟"),
    ("初雪 V8 保湿面霜", "美妆个护", "初雪"),
    ("初雪 W9 精华液", "美妆个护", "初雪"),
]

MEMBER_LEVELS = ["普通", "银卡", "金卡", "钻石"]
ORDER_STATUS = ["已支付", "已支付", "已支付", "已支付", "已取消", "已退款"]  # 已支付占比约 2/3

DATA_START = dt.date(2024, 1, 1)
# 结束日期取「运行当天」而非硬编码：样例数据始终覆盖到最近一天，
# 「去年」「今年」「近三个月」这类相对时间问法才有数据可查。
DATA_END = dt.date.today()


def build_rows() -> dict[str, tuple[list[str], list[tuple]]]:
    """生成全部样例数据，返回 {表名: (列名列表, 行列表)}。"""
    rng = random.Random(20251008)  # 固定种子 -> 可复现

    # --- dim_region ---
    regions: list[tuple] = []
    rid = 1
    for region_name, provinces in REGIONS.items():
        for province in provinces:
            regions.append((rid, province, region_name, "中国"))
            rid += 1

    # --- dim_customer ---
    customers: list[tuple] = []
    for cid in range(1, 61):
        name = rng.choice(SURNAMES) + rng.choice(GIVEN)
        customers.append((cid, name, rng.choice(["男", "女"]), rng.choice(MEMBER_LEVELS)))

    # --- dim_product ---
    products = [(i, name, category, brand) for i, (name, category, brand) in enumerate(PRODUCTS, 1)]

    # --- dim_date ---
    dates: list[tuple] = []
    day = DATA_START
    while day <= DATA_END:
        dates.append((int(day.strftime("%Y%m%d")), day.year, (day.month - 1) // 3 + 1, day.month, day.day))
        day += dt.timedelta(days=1)

    # --- fact_order ---
    # 让各地区的销售额随月份自然波动，并给华东区略高的权重，
    # 这样「去年华东区销售额前三的省份」有稳定且可解释的答案。
    region_weights = {r[0]: (3.0 if r[2] == "华东" else 1.0) for r in regions}
    weight_pool: list[int] = []
    for region_id, weight in region_weights.items():
        weight_pool.extend([region_id] * int(weight * 10))

    orders: list[tuple] = []
    order_id = 10_000_001
    for day_row in dates:
        date_id, year, _quarter, month, _d = day_row
        # 季节性：11-12 月旺季、2 月淡季（春节）
        season = 2.2 if month in (11, 12) else (0.5 if month == 2 else 1.0)
        daily_count = max(0, int(rng.gauss(4.0 * season, 1.6)))
        for _ in range(daily_count):
            product_id, _pname, _cat, _brand = rng.choice(products)
            quantity = rng.choices([1, 2, 3, 4, 5], weights=[55, 22, 12, 7, 4])[0]
            unit_price = round(rng.uniform(60, 6800), 2)
            orders.append(
                (
                    order_id,
                    rng.randint(1, len(customers)),
                    product_id,
                    date_id,
                    rng.choice(weight_pool),
                    quantity,
                    round(unit_price * quantity, 2),
                    rng.choice(ORDER_STATUS),
                )
            )
            order_id += 1

    return {
        "dim_region": (["region_id", "province", "region_name", "country"], regions),
        "dim_customer": (["customer_id", "customer_name", "gender", "member_level"], customers),
        "dim_product": (["product_id", "product_name", "category", "brand"], products),
        "dim_date": (["date_id", "`year`", "quarter", "`month`", "`day`"], dates),
        "fact_order": (
            [
                "order_id",
                "customer_id",
                "product_id",
                "date_id",
                "region_id",
                "order_quantity",
                "order_amount",
                "order_status",
            ],
            orders,
        ),
    }


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


async def server_engine() -> AsyncEngine:
    """连到 MySQL 实例本身（不指定库），用于 CREATE DATABASE。"""
    settings = get_settings()
    admin = settings.db_meta.model_copy(update={"database": ""})
    return create_async_engine(admin.url, pool_pre_ping=True)


async def create_account(engine: AsyncEngine, db_name: str) -> None:
    """创建 dw 只读账号（ADR-008 / NFR-SEC-007）。

    口令从环境变量 DW_READONLY_PASSWORD 读取，不接受命令行参数，
    避免进入 shell 历史与进程列表（NFR-SEC-003）。
    """
    import os

    settings = get_settings()
    user = settings.db_dw.user
    password = os.environ.get("DW_READONLY_PASSWORD", "")
    if not password:
        print(f"[跳过] 未提供 DW_READONLY_PASSWORD，未创建只读账号 {user}")
        print(f"       如需创建：DW_READONLY_PASSWORD='<口令>' python -m app.scripts.seed_dw --create-account")
        return
    async with engine.begin() as conn:
        for host in ("localhost", "127.0.0.1"):
            await conn.execute(text(f"CREATE USER IF NOT EXISTS '{user}'@'{host}' IDENTIFIED BY :p"),
                               {"p": password})
            await conn.execute(text(f"ALTER USER '{user}'@'{host}' IDENTIFIED BY :p"), {"p": password})
            # 只给 SELECT：写操作在机制上不可能发生（ADR-008）
            await conn.execute(text(f"GRANT SELECT ON `{db_name}`.* TO '{user}'@'{host}'"))
        # 不要在这里 FLUSH PRIVILEGES：CREATE USER/GRANT 是即时生效的 DDL，flush 没有意义，
        # 反而会清空 MySQL 的 caching_sha2_password 服务端缓存 —— 之后任何一次连接都要走
        # 完整的 RSA 握手，缺少 cryptography 包时直接抛 RuntimeError，把后续步骤全带崩。
    print(f"[OK] 只读账号 {user}@localhost / {user}@127.0.0.1 已就绪（仅 SELECT on {db_name}.*）")


async def seed(no_drop: bool, with_account: bool) -> None:
    settings = get_settings()
    db_name = settings.db_dw.database

    srv = await server_engine()
    try:
        async with srv.begin() as conn:
            await conn.execute(
                text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                     "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
            )
        print(f"[OK] 库 {db_name} 就绪")

        if with_account:
            await create_account(srv, db_name)
    finally:
        await srv.dispose()

    # 建表/灌数据是离线管理操作，用 db_meta 账号（有 DDL 权限）连到 dw 库；
    # db_dw 的只读账号是给运行期查询用的（ADR-008 / NFR-SEC-007），没有 DDL 权限。
    dw = create_async_engine(
        settings.db_meta.model_copy(update={"database": db_name}).url, pool_pre_ping=True
    )
    try:
        async with dw.begin() as conn:
            if no_drop:
                existing = set(
                    (await conn.execute(text(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema=:d"),
                        {"d": db_name})).scalars()
                )
            else:
                existing = set()
                for t in reversed(TABLE_ORDER):
                    await conn.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
                print("[OK] 旧表已清除")

            for t in TABLE_ORDER:
                if t in existing:
                    print(f"[跳过] 表 {t} 已存在（--no-drop）")
                    continue
                await conn.execute(text(TABLE_DDL[t]))
                print(f"[OK] 建表 {t}")

        rows_by_table = build_rows()
        async with dw.begin() as conn:
            if no_drop:
                fact_count = (await conn.execute(text("SELECT COUNT(*) FROM fact_order"))).scalar()
                if fact_count:
                    print(f"[跳过] 已有 {fact_count} 条订单数据，不重复灌入（--no-drop）")
                    return
            for t in TABLE_ORDER:
                columns, rows = rows_by_table[t]
                # 绑定名用 c0/c1… 而非 0/1：text() 的绑定名必须是合法标识符
                names = [f"c{i}" for i in range(len(columns))]
                placeholders = ", ".join(f":{n}" for n in names)
                stmt = text(f"INSERT INTO `{t}` ({', '.join(columns)}) VALUES ({placeholders})")
                # 分批插入，避免单条 SQL 超出 max_allowed_packet
                batch = 500
                for i in range(0, len(rows), batch):
                    await conn.execute(stmt, [dict(zip(names, r)) for r in rows[i:i + batch]])
                print(f"[OK] 灌入 {t}: {len(rows)} 行")
    finally:
        await dw.dispose()

    print(f"\n完成。库 {db_name}，表 {len(TABLE_ORDER)} 张。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建数仓库并生成样例数据（阶段 4）")
    parser.add_argument("--no-drop", action="store_true", help="表已存在时跳过重建，只补数据")
    parser.add_argument("--create-account", action="store_true",
                        help="创建/更新 dw 只读账号（口令取自环境变量 DW_READONLY_PASSWORD）")
    args = parser.parse_args(argv)

    asyncio.run(seed(no_drop=args.no_drop, with_account=args.create_account))
    return 0


if __name__ == "__main__":
    sys.exit(main())
