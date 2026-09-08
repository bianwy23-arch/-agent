# KuaiSearch 导购适用性核查

核查日期：2026-09-07。结论基于当前官方论文、仓库字段说明、Hugging Face 文件清单与原始文件前缀；不是全量数据质量审计。

## 结论与选型方向

“商品主表没有价格和详细规格”成立；“整个 KuaiSearch 没有价格或属性”不成立。Ranking 有 target_item_price，Relevance 有 attr_value。内部搜索行为关联存在，但公开字段不提供完整的产品型号—SKU—销售方案关系。

基于本项目需要预算筛选、参数比较与可追溯商品证据，建议更换主要商品数据来源；KuaiSearch 可作为独立搜索研究数据保留。替代首选候选为 Amazon Reviews 2023 原始 metadata + reviews，需经目标品类字段覆盖与身份粒度验收后确定。尚未迁移运行数据。

## 实际读取方法

通过 Hugging Face 文件 API 确认 items、rank、relevance、recall 下均有 train.jsonl。分别用 HTTP Range 读取前 65,536 字节，忽略末尾不完整 JSON 行。以下计数只表示非随机前缀样本，不能外推覆盖率。

| 文件 | 完整样本行数 | 核查结果 |
| --- | --- | --- |
| items/train.jsonl | 158 | 12 个字段，标题及商品、品牌、商家、三级类目 ID/名称 |
| rank/train.jsonl | 84 | target_item_id、target_item_price、用户/会话/行为等字段 |
| relevance/train.jsonl | 196 | query、item_title、brand、seller_name、attr_value、score、split；没有 item_id |
| recall/train.jsonl | 68 | 商品曝光、点击、购买 ID 列表，用户/会话、query、time_index、split |

来源：[商品文件](https://huggingface.co/datasets/benchen4395/KuaiSearch/blob/main/items/train.jsonl)、[排序文件](https://huggingface.co/datasets/benchen4395/KuaiSearch/blob/main/rank/train.jsonl)、[相关性文件](https://huggingface.co/datasets/benchen4395/KuaiSearch/blob/main/relevance/train.jsonl)、[召回文件](https://huggingface.co/datasets/benchen4395/KuaiSearch/blob/main/recall/train.jsonl)。

商品样本完整字段：item_id、item_title、brand_id、brand_name、seller_id、seller_name、category_level1_id、category_level1_name、category_level2_id、category_level2_name、category_level3_id、category_level3_name。

## 价格：存在，但不在主表

Ranking 样本中 target_item_id=58 对应 target_item_price=5990.0。官方论文表 2 与 Ranking Data Construction 明确提供商品价格。不能据此认定 5990.0 是 5990 元或 59.90 元，本次未核实计价单位。

target_item_id 在设计上可关联 Item.item_id，但本次未统计全量关联成功率、有效价格比例或同商品多次报价差异。搜索记录里的价格不是实时库存报价，也不意味着全部商品都具有可用价格。

来源：[官方论文表 2 和第 3 节](https://arxiv.org/html/2602.11518v1)、[官方数据预览](https://benchen4395.github.io/KuaiSearch/)。

## 规格：有局部文本，缺少统一商品规格库

Item 主表没有 description、规格键值表或 SKU 配置字段。Relevance 是 query—商品相关性标注数据，其 attr_value 样本为逗号分隔属性值文本，例如产地、风格、年龄段、品牌等，未提供对应属性名，也未提供 item_id。

据此推断：不能将其直接作为整个商品池的可关联规格表。用标题、品牌、商家模糊匹配存在歧义，且局部相关性样本不能等同于全商品规格覆盖。标题偶尔出现参数或金额也不等于规范字段。

来源：[官方字段定义](https://github.com/benchen4395/KuaiSearch#data-schema)、上述原始商品和相关性文件。

## 关联：搜索关系存在，导购关系不足

- 有：商品与品牌、商家、类目，以及搜索会话与曝光/点击/购买商品之间的 ID 关系。
- 未见：公开 schema 中的产品型号—SKU 变体—商家报价完整映射、商品评论正文关联、参数来源链接、实时库存与配送字段。
- 官方论文说明用户和商品等标识经过重映射，时间使用相对表示。因此不能直接把 item_id 当成可访问平台商品详情的原始标识。

上述“未见”是公开 schema 与前缀样本的核查结论，不是对全部文件逐行扫描后得到的绝对不存在证明。

来源：[官方论文第 3 节](https://arxiv.org/html/2602.11518v1)、[官方字段定义](https://github.com/benchen4395/KuaiSearch#data-schema)。

## 替代候选与下一步

参见 [导购数据集替代方案核查](research-shopping-dataset-alternatives.md)。Amazon Reviews 2023 提供商品详情、历史价格与评论关联，但仍可能缺价、缺参数，parent_asin 不代表精确 SKU 映射。

迁移前先验证一个候选品类：真实有效价格、参数有效性、对象与配置匹配、商品—评论关联、来源与版本可追溯。不可用伪价格或模型生成参数补齐后宣称完整。
