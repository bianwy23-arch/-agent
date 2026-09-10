# 商品硬条件核验范围

冻结库共 12 品类、每类 50 条，共 600 条商品。下表统计能从资料明确解析的记录数，不能据此宣称所有商品都有全部规格。原始数据与历史 USD 价格不修改。

| 品类 | 字段：已知 / 未知 / 冲突（每项共 50） |
| --- | --- |
| headphones | connectivity: 50/0/0；form_factor: 48/0/2；weight: 48/2/0；waterproof: 6/44/0 |
| bluetooth_speakers | connectivity: 49/1/0；speaker_type: 36/14/0；weight: 46/4/0；waterproof: 27/23/0 |
| keyboards | connectivity: 50/0/0；keyboard_description: 17/33/0；number_of_keys: 46/4/0；compatible_devices: 32/18/0；weight: 47/3/0 |
| mice | connectivity: 49/1/0；tracking: 50/0/0；number_of_buttons: 11/39/0；weight: 49/1/0 |
| rice_cookers | capacity: 24/26/0；power: 44/6/0；voltage: 38/12/0；material: 37/13/0；power_source: 29/21/0 |
| electric_kettles | capacity: 48/2/0；power: 46/4/0；voltage: 43/7/0；material: 49/1/0；weight: 50/0/0 |
| vacuum_cleaners | vacuum_type: 50/0/0；power_source: 50/0/0；power: 33/17/0；weight: 50/0/0；capacity: 24/26/0；battery_life: 11/39/0 |
| desk_lamps | light_source: 49/1/0；power_source: 47/3/0；power: 44/6/0；voltage: 34/16/0；material: 22/28/0 |
| backpacks | capacity: 17/33/0；material: 42/8/0；weight: 37/13/0 |
| water_bottles | capacity: 21/29/0；material: 45/5/0；weight: 46/4/0 |
| electric_toothbrushes | age_range: 49/1/0；power_source: 49/1/0 |
| electric_shavers | shaving_use: 45/5/0；power_source: 50/0/0；head_type: 24/26/0；battery_life: 1/49/0 |

## 核验规则

- 数值条件支持等于、大于/小于及含边界比较；使用 Decimal。容量统一升、功率瓦、电压伏、商品重量克、续航分钟，按键数计个数。只转换明确定义的单位。
- Cups、含糊的容量 ounces、范围电压以及无法确定单位的值保持未知；冻结库的 472 条可用重量均来自 details.Item Weight；没有为任意未来数据源建立通用的包装重量识别器。
- 连接、佩戴形式、材料、光源、供电、适用年龄等使用显式词表；开放字段缺少某个词不证明不支持。泛化的 tablet 标签不证明兼容某年某型号设备；材料包含不锈钢不证明纯不锈钢内胆。
- IP 等级只匹配明确原文，未实现等级高低推导。明确多值冲突、耳机标题与佩戴字段冲突、水壶/水杯标题容量与详情冲突不能通过硬条件。未建设覆盖任意营销文案的通用矛盾检测。
- 格式合法但暂不能核验的要求继续保存在正式需求中，资格为 unknown；不通过丢条件获得推荐资格。
- 搜索按当前条件核验，已违反的候选列入 eliminated；未知与冲突仍单独保留，不能作为无条件主推荐。正式资格与需求版本一起保存；修改后清除旧资格、保留事实。
- 主推荐必须本轮查看原文、通过全部已记录硬条件及引用校验。最终商品名称、价格和满足条件说明由可信数据渲染；模型草稿留在工具轨迹，不把任意规格、品牌保证或未验证最优声明直接展示给用户。
- 模型仍决定搜索词、查看哪些候选、推荐谁和何时停止。渲染不替模型选择商品，也不把固定用户评估输入变成固定工具工作流。

## 限制

这是对资料标注的核验，不是独立商品实测、实时价格服务或完整 SKU 配置报价验证。是否完整理解用户语义、非推荐回答是否准确，仍需要逐任务语义复核。完整用户选择状态、推断依赖传播和跨不同参数的语义无进展识别仍未完成；执行上限保留为兜底。
