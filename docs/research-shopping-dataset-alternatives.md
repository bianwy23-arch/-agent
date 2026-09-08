# 导购商品数据集替代方案核查

> 后续决定：第一版仅使用商品元数据与历史价格，不引入用户评论。12 类、600 条的数据准备及独立读取接口已落地，见 [Amazon 数据交付说明](amazon-data.md)。下文保留选型时的调研记录；涉及评论的建议不再是当前实施范围。

核查日期：2026-09-07。范围：原发布方文档、数据卡、公开示例与字段定义；未下载全量数据，未计算原始大数据集的整体缺失率，未执行项目数据迁移。

## 结论

建议优先审计 **McAuley-Lab/Amazon-Reviews-2023 的原始商品元数据与评论**，作为离线导购商品库候选。它提供价格、描述、详情和评论关联，但不能据此宣称价格完整、SKU 完整或支持实时购买。最终选型应以目标品类抽样核验结果为准。

## 1. 首选候选：Amazon Reviews 2023 原始发布

- 商品字段包括 `title`、`features`、`description`、`details`、`price`、`categories`、`store`、`parent_asin`；`price` 的语义是采集时的美元价格。
- 评论提供 `asin`、`parent_asin`、文本、评分和时间，可按 `parent_asin` 关联商品元数据。
- `parent_asin` 聚合不同颜色、尺寸、风格等变体。因此评论中的具体 ASIN 不能等同于商品元数据已经提供该变体的准确配置、报价。
- `bought_together` 是搭配关联字段，不代表兼容性证明；`images.variant` 是图片位置，不是商品 SKU 变体编号。

来源：[原发布方字段说明](https://amazon-reviews-2023.github.io/main.html)。

已确认的缺陷：官方 HF 示例本身就出现 `price: 'None'`、空 `description`、空 `features`、空 `categories`、`bought_together: None`。HF schema 中 `price` 和 `details` 可为字符串，需解析；字段存在不等于字段完整。**本次未测全量或各品类覆盖率。**

来源：[官方 HF 数据卡及示例](https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023)、[原始 README schema](https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/blob/main/README.md?code=true)。

原发布者声明主要提供给研究，未为原始数据指定使用许可证。不能把衍生仓库的 MIT 标签直接当作原始数据完整的授权结论。

来源：[McAuley-Lab 对许可的正式回复](https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/discussions/1)。

## 2. 方便的小子集，但不是完整替代：milistu/AMAZON-Products-2023

该发布者从 Amazon Reviews 2023 筛选首次上架时间在 2023 年的商品并加入向量，共 117,243 条；数据卡报告价格缺失 35,869 条，约 **30.59%**。有 `parent_asin`、价格、描述、features、details，但不自带评论正文，需要另关联。HF 预览也能直接看到价格 null。

该子集便于试验，但没有解决价格缺失和父商品粒度问题；不建议因为名字叫 Products 就认为比原始 metadata 更完整。上述计数来自数据卡，未本地复算。

来源：[子集发布者数据卡](https://huggingface.co/datasets/milistu/AMAZON-Products-2023)。

## 3. 其他可选方向

| 候选 | 已确认提供 | 对本项目的定位 |
|---|---|---|
| Amazon-M2 | 商品价格、标题、品牌、颜色、描述及多地区购物会话 | 若重视会话推荐可再审计；本次未确认完整 SKU、评论关联或缺失率，暂不优于首选 |
| Amazon ESCI | 查询与商品相关性标注，商品标题、描述、卖点、品牌、颜色、地区 | 适合检索评测；官方商品表没有价格列，不能解决预算筛选缺陷 |
| Google extended_amazon_2023_dataset | 从 Amazon Reviews 2023 扩展的标题、描述、图片描述和特征 | 这些新增内容由 LLM 生成，不能当成独立核验的商品事实来源；其 schema 没有价格 |

来源：[Amazon-M2 原论文](https://cdn.amazon.science/5e/3b/4977e5ab458385773ca9df987cf5/amazon-m2-a-multilingual-multi-locale-shopping-session-dataset-for-recommendation-and-text-generation.pdf)、[Amazon ESCI 官方仓库及字段](https://github.com/amazon-science/esci-data)、[Google 扩展集方法与 schema](https://huggingface.co/datasets/google/extended_amazon_2023_dataset)。

## 4. 推荐验收办法（设计建议，尚未执行）

先从原始 metadata 中选目标品类，保留固定版本和可复现抽样方法，统计：可解析且为正的价格比例、描述/详情有效比例、关键属性覆盖率、商品身份粒度、评论关联成功率。缺失值、空数组、字符串 `None`、零价格分别报告，不能通过填充伪价格提高覆盖率。

商品库发布为“历史快照导购”，明确美元与数据年份；中文交互不改变原始货币。未来实时价格、库存、配送需要单独数据源。评论是用户体验证据，不自动成为硬规格事实。

若业务必须落实到精确 SKU：抽样检查变体参数与价格能否对应；若不能，选择配置歧义较少的子集，或额外补充可靠资料。不要仅凭 `parent_asin` 可关联就宣布商品关系完整。
