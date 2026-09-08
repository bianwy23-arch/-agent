# 日常导购商品库：Amazon Daily v1

本次范围是准备和交付商品数据，以及可独立调用的读取、校验、品类/预算过滤接口。12 个品类，每类 50 条，共 600 条。旧网页的 `shop_v1` 仍使用原接口；本次没有把美元价格塞进其固定人民币的字段，也没有生成 240 个评估案例。

## 数据口径

- 来源：McAuley Lab 的 Amazon Reviews 2023 **商品元数据**，不是 Amazon 实时数据库。
- 固定版本：`2b6d039ed471f2ba5fd2acb718bf33b0a7e5598e`。
- 官方资料：[字段说明](https://amazon-reviews-2023.github.io/main.html#for-item-metadata)、[固定版本原始文件](https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/tree/2b6d039ed471f2ba5fd2acb718bf33b0a7e5598e/raw/meta_categories)。
- 只采集商品元数据。没有下载用户评论，没有按评论评分筛选或推荐。
- 价格是原始 `price` 字段中的正数，单位 **USD**，类型为 `historical_snapshot`。没有生成价格、汇率换算或缺失价格填充。
- 单条价格采集时间没有提供，`observed_at=null`。首次上架日期不是价格采集时间。
- 商品 ID 保留 `parent_asin`。一条记录代表其资料所描述的商品/配置，不宣称拥有完整的所有变体及报价关系。
- 商品标题、卖点、描述、详情保留英文原文；品类名提供中文映射。不存在模型翻译后再当作原始规格的字段。
- 已标识标题明确写出的翻新品，其余商品的成色为 `unspecified`，不擅自断言全新。

## 可回答范围

所有商品有有效历史价格、标题、至少一段卖点或描述，以及下表的必要属性。其他属性只有原文提供时才存在。属性值是来源报告的文本，不等同于第三方实测结论。

| 品类 ID | 中文品类 | 必要属性 | 第一版主要支持的判断 |
|---|---|---|---|
| headphones | 耳机 | 连接方式、佩戴形式 | 预算、有线/无线、入耳/头戴；兼容性只按具体原文判断 |
| bluetooth_speakers | 便携蓝牙音箱 | 连接方式、音箱类型 | 预算、蓝牙连接、便携使用；续航/防水需另外有证据 |
| keyboards | 键盘 | 连接方式、键盘描述 | 预算、连接方式、来源所述键盘类型；不把营销性的机械手感当成机械轴 |
| mice | 鼠标 | 连接方式、追踪技术 | 预算、连接方式、光学/激光等原文属性 |
| rice_cookers | 电饭煲 | 容量、功率或供电说明 | 预算、容量、供电；生米杯/熟饭杯必须保留语境，不能直接混比 |
| electric_kettles | 电热水壶 | 容量、材料 | 预算、容量、材料；车载/家用、电压按原文分别判断 |
| vacuum_cleaners | 吸尘器 | 形态、供电方式 | 预算、手持/立式/机器人、供电；不将不同测量条件的吸力直接排名 |
| desk_lamps | 台灯 | 光源、供电方式 | 预算、光源、供电；调光和色温按有证据的商品回答 |
| backpacks | 双肩包 | 产品尺寸、材料 | 预算、尺寸、材料；包含通勤、户外及专用双肩携带包，具体用途看原文 |
| water_bottles | 水杯／保温杯 | 容量、材料 | 预算、容量、材料；保温时长仅引用提供了证据的记录 |
| electric_toothbrushes | 电动牙刷 | 供电方式、适用年龄 | 预算、成人/儿童、供电；清洁效果不根据营销文字作实测承诺 |
| electric_shavers | 电动剃须刀 | 供电方式、剃刮用途 | 预算、面部/头部用途、供电；刀头类型仅在提供时回答 |

字段不在这张表中不代表永远不能回答，而是不能保证每条商品都具备。例如用户要求某个确定的防水等级，缺失该信息的商品应返回证据不足，而不是自动满足或自动断言不防水。

“材料”“尺寸”等原始文字也可能描述不同部件；在使用特定约束前需要读取证据语境。通用词匹配不能替代产品级判断。

## 筛选与核验

1. 在 Electronics、Home and Kitchen、Beauty and Personal Care、Sports and Outdoors、Appliances、Health and Household、Tools and Home Improvement 七个原始类目文件中，下载固定字节窗口。
2. 跳过窗口开头/末尾不完整 JSON 行。记录每个窗口的字节数、SHA-256、完整记录数；这种采样不是随机抽样，不代表全库分布。
3. 依据标题、必要详情和排除规则识别 12 个目标品类，排除替换配件、非目标物品、无效价格及缺失必要属性的记录。
4. 检查部分可确定的冲突：标题和详情的公升容量/瓦数、耳机佩戴形式、牙刷年龄。水杯容量以磅等重量单位填写的记录不入选。未对所有单位和全部语义冲突实现通用判定。
5. 人工查看候选标题和部分具体规格，发现的误收补入规则；记录级排除及原因保存在 `review_exclusions.json`。没有声称对 600 条商品的全部营销文案逐句核实。
6. 按 `parent_asin` 去重；各类按价格四分位轮流选取，优先增加品牌/商店标签多样性，最后按品类与 ID 排序。精确同标题不重复选入。不同标题的近似变体可能保留。
7. 每个提取属性携带 `evidence.field` 和原文片段。来源记录保存源文件、固定版本、字节位置、原始行 SHA-256 和规范化记录 SHA-256。
8. 加载器检查文件校验和、商品和来源一一对应、每条价格与来源相等、每个提取属性有原文证据、必要属性覆盖。

品牌字段优先使用 `details.Brand`/`Brand Name`，其次使用 `Manufacturer` 或 `store`，来源字段明确保留。因此质量报告的 `brands` 是品牌/厂商/商店标签数，不是核实后的独立品牌企业数。

### 文件

- `data/amazon/catalog_v1/products.jsonl`：600 条供应用读取的商品。
- `data/amazon/catalog_v1/source_records.jsonl`：这 600 条对应的完整商品元数据与来源定位。
- `data/amazon/catalog_v1/manifest.json`：版本、数量及文件校验和。
- `data/amazon/catalog_v1/quality_report.json`：各类候选、通过、选取数量，属性覆盖率、价格范围及拒绝原因。
- `data/amazon/source/candidates.jsonl.gz`：压缩候选池，供离线重新构建，不需要重新下载大型原始文件。
- `data/amazon/source/acquisition.json`：源窗口下载与扫描记录。
- `data/amazon/source/review_exclusions.json`：人工确认的记录级排除理由。
- `scripts/build_amazon_catalog.py`：获取、筛选和可复现构建脚本，仅用 Python 标准库及 curl。
- `src/shopping_agent/amazon_catalog.py`：独立读取、校验及美元预算过滤接口。

原始元数据可能自带平均评分等字段，保留在完整来源记录中用于忠实归档；它们不参与构建规则，也不作为当前导购证据或推荐排名依据。

## 使用

在仓库根目录执行：

```bash
# 校验全部商品、来源与属性证据，并显示各类数量
.venv/bin/python -m shopping_agent.amazon_catalog

# 查看历史价格不超过 50 美元的电热水壶
.venv/bin/python -m shopping_agent.amazon_catalog \
  --category electric_kettles --max-price-usd 50 --limit 3

# 从已提交的压缩候选池离线重新构建
.venv/bin/python scripts/build_amazon_catalog.py build \
  --output /tmp/amazon-rebuild

# 跑针对性的导入、证据和预算过滤检查
.venv/bin/python -m pytest tests/test_amazon_catalog.py
```

应用代码可以直接调用：

```python
from decimal import Decimal
from shopping_agent.amazon_catalog import load_amazon_catalog, filter_products

products = load_amazon_catalog()
candidates = filter_products(
    products, category_id="electric_kettles", max_price_usd=Decimal("50")
)
```

这里的过滤器只负责确定的品类和预算条件，不宣称是完整语义检索或 Agent 推荐器。

需要从上游完整复现采样时：

```bash
.venv/bin/python scripts/build_amazon_catalog.py fetch --cache /tmp/amazon-source-cache
```

`fetch` 根据 acquisition 清单取回精确窗口并校验 SHA-256；会产生较大的临时下载。再根据输出的 `SourceCategory:byte_start:local_path` 列表，给 `ingest` 传入多个 `--chunk`，然后运行 `build`。日常使用和离线构建不需要这一步。

## 接入旧 Agent 的边界

旧 `CatalogItem`/`DisplayPrice` 只接受 CNY，且部分原文验证依赖旧 eval probes。本次新数据接口没有复用这两条假设。后续接入新 ShoppingTask 状态设计时，需让价格携带 currency，并让约束检查读取本数据的实际商品证据。不能直接把 `price.amount` 写入旧 `amount_cny`。

评估优先验证功能正确性与可复现问题修复，基础 Agent 对照不作为必做项；第一版验收规模确定为约 40～60 个完整任务案例，具体清单待编制，不将此前的 240 个案例规模建议视为当前要求或已完成结果。当前约定见 [评估范围与简历表述约定](evaluation-and-resume-scope.md)。评估答案要由冻结后的这份商品库支持，不能根据源文件总量或候选池推断当前商品库一定有匹配项。

## 本次实际统计

扫描固定窗口中的 **1,263,580 条**完整元数据，获得 **14,688 条**目标候选；通过当前规则与记录级排除后 **2,010 条**，最终选取 **600 条**。未扫描全库，不能把这些比例解释为 Amazon 全库质量。

| 品类 | 通过筛选 | 入选 | 品牌/厂商/商店标签数 | 历史价格范围（USD） |
|---|---:|---:|---:|---:|
| 耳机 | 426 | 50 | 50 | 3.99–169.57 |
| 便携蓝牙音箱 | 92 | 50 | 50 | 9.99–249.00 |
| 键盘 | 53 | 50 | 40 | 7.99–399.95 |
| 鼠标 | 82 | 50 | 46 | 4.99–129.99 |
| 电饭煲 | 68 | 50 | 40 | 24.99–715.00 |
| 电热水壶 | 129 | 50 | 50 | 18.99–149.00 |
| 吸尘器 | 319 | 50 | 50 | 5.00–409.99 |
| 台灯 | 91 | 50 | 50 | 8.99–207.77 |
| 双肩包 | 85 | 50 | 50 | 11.99–241.47 |
| 水杯／保温杯 | 528 | 50 | 50 | 6.99–188.00 |
| 电动牙刷 | 82 | 50 | 50 | 7.88–326.62 |
| 电动剃须刀 | 55 | 50 | 36 | 8.93–223.23 |

验证结果：146 项测试全部通过；四个发布文件离线重建后逐字节一致。另对 3 个不同源文件中的记录重新请求原始字节，均与保存的原始行 SHA-256 一致；其中一条是后来排除的背包收纳车候选，另外两条为入选商品。这项检查验证来源定位，不是商品质量背书。

应用商品文件约 2.45 MB，600 条原始证据文件约 3.58 MB。压缩候选池另约 16.89 MB，用于离线重建。
