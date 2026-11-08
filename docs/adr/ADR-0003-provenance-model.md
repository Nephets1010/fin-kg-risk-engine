# ADR-0003：溯源模型与存储形式

- **状态**：已接受
- **日期**：2026-10-29

## 背景

风控场景里，「这个风险分是怎么来的」比「风险分是多少」更重要。一个风控分析师要能回答：

- 这个事实来自哪个源？可靠吗？
- 是谁在什么时候抽取的？
- 抽取时的置信度是多少？
- 如果同一条事实有多个来源，哪个支撑了这个结论？

这要求**溯源是一等公民**，而不是塞进备注里的一个字符串。

## 决策

### 1. 溯源是值对象，不是字符串

```python
class Provenance(BaseModel):
    source_type: SourceType        # registry / filing / court / news / synthetic / ...
    source_id: str                  # 逻辑源标识，如 "tushare:stock_basic"
    record_id: str                  # 源内记录标识
    extraction_method: ExtractionMethod
    source_url: str | None
    confidence: float               # 抽取置信度，不是源的可靠度
    extractor_version: str | None
    retrieved_at: datetime          # 平台获知时间
    valid_time: date | None         # 事实在现实中成立的时间
    content_hash: str | None
```

**双时间模型**：`retrieved_at` 是「平台什么时候知道的」，`valid_time` 是「这件事什么时候在世界上发生的」。二者不同 —— 一条 2023 年的判决可能在 2026 年才被抓取。

### 2. 置信度属于抽取，不属于源

`confidence` 描述的是**这条事实被抽取出来的把握程度**，不是源的权威性。人工录入的工商记录和 LLM 从新闻里抽出的当事人，两者来源不同、置信度也不同。用一个字段表达两种含义会让阈值无法设定。

### 3. 溯源以 JSON 字符串存储在节点属性上

```json
{"provenance": "[{\"sourceId\": \"csv:companies\", \"confidence\": 1.0, ...}]"}
```

**为什么是字符串而不是 list of map**：Neo4j 的属性值类型不支持 list of map —— 这是 ADR-0001 选型的直接代价。序列化后单属性存储，读取时反序列化（`fin_kg.ingestion.pipeline.stored_items`）。

**为什么不用独立节点 `(:Provenance)`**：溯源记录数量与节点同量级，独立节点会让 10 万实体的图膨胀到 40 万节点，而溯源几乎从不被单独查询 —— 它总是随主体一起读。字符串属性的读取成本远低于多一次 JOIN。

### 4. 溯源按证据指纹去重

`Provenance.fingerprint()` = `sha256(source_id | record_id | source_url)[:32]`。同一事实被多个源佐证时，溯源列表保留多条（互相印证），但完全相同的证据只留一条。

## 后果

**正面**

- 任何风险分都可以回溯到具体的源记录与抽取方法。
- 合成数据与真实数据在溯源上无法混淆：合成源的 `source_type` 恒为 `SYNTHETIC`，`confidence` 恒为 0.5。
- 抽取器的版本变化可以通过 `extractor_version` 追溯。

**负面 / 代价**

- 溯源 JSON 让节点属性变长，读取时需要解析。10 万节点量级下可接受。
- 序列化格式变更需要数据迁移。当前格式没有版本号，未来加 `provenanceFormatVersion` 字段。
- 溯源不能直接用 Cypher 聚合（例如「有多少条事实来自 court 源」需要全表扫描）。若该需求出现，应改为独立节点。

## 验证方式

- `tests/unit/test_domain.py::TestProvenance` 覆盖指纹稳定性、URL 校验、属性序列化。
- `tests/unit/test_ingestion.py` 覆盖溯源随清洗流程附加、反序列化的往返。
- 所有合成数据记录的 `source_type` 断言为 `SYNTHETIC`（`test_synthetic_source_marks_provenance`）。

## 撤销条件

当「按来源统计事实数量」成为高频需求时，改为独立的 `:Provenance` 节点并建立 `(entity)-[:SOURCED_FROM]->(:Provenance)` 关系。
