# 用一条 SQL 完成全文 + 向量混合检索

## 为什么把两路召回放进 seekdb

传统做法是全文检索走 Elasticsearch、向量检索走向量库，应用层再合并结果。两套系统意味着两份写入、两套权限、两次网络往返，而且在“先过滤再召回”的场景里很难保证过滤条件一致。

seekdb 把全文索引和向量索引放在同一张表上，可以用一条 SQL 同时拿到两个通道的候选集：

- `FULLTEXT INDEX ... WITH PARSER ik` 负责中文与英文关键词命中；
- `VECTOR INDEX ... WITH (DISTANCE=l2, TYPE=hnsw, LIB=vsag)` 负责语义相近的片段；
- 两个子查询各自用 `ROW_NUMBER()` 计算通道内排名，再 `UNION ALL` 返回。

这样只有一个事务边界、一份文档元数据，过滤条件（集合、文档 ID）对两个通道天然一致。

## 表结构的关键选择

```sql
CREATE TABLE cf_chunks (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  document_id VARCHAR(64) NOT NULL,
  page_no INT NOT NULL,
  heading VARCHAR(512) NOT NULL DEFAULT '',
  text MEDIUMTEXT NOT NULL,
  embedding VECTOR(384) NOT NULL,
  FULLTEXT INDEX cf_ft (text) WITH PARSER ik,
  VECTOR INDEX cf_vec (embedding)
    WITH (DISTANCE=l2, TYPE=hnsw, LIB=vsag, SYNC_MODE=IMMEDIATE)
) ORGANIZATION = HEAP;
```

`SYNC_MODE=IMMEDIATE` 是必须的：默认的异步索引在写入后要等后台刷新，演示时会出现“刚导入的文档搜不到”。改成立即同步后，导入返回即可检索。

## 两个通道的写法

全文通道按相关度倒序，并把 `MATCH ... AGAINST` 的分数一起选出来，方便排查：

```sql
SELECT c.id, c.page_no, 'fts' AS channel,
       ROW_NUMBER() OVER (ORDER BY MATCH(c.text) AGAINST (%s) DESC) AS rank_in_channel,
       MATCH(c.text) AGAINST (%s) AS channel_score
FROM cf_chunks c
WHERE c.collection_id = %s AND MATCH(c.text) AGAINST (%s)
ORDER BY MATCH(c.text) AGAINST (%s) DESC
LIMIT 24
```

向量通道用 `l2_distance` 排序，并显式加 `APPROXIMATE` 走 HNSW 近似扫描：

```sql
SELECT c.id, c.page_no, 'vector' AS channel,
       ROW_NUMBER() OVER (ORDER BY l2_distance(c.embedding, %s) ASC) AS rank_in_channel,
       l2_distance(c.embedding, %s) AS channel_score
FROM cf_chunks c
WHERE c.collection_id = %s
ORDER BY l2_distance(c.embedding, %s) APPROXIMATE
LIMIT 24
```

`EXPLAIN` 里应同时看到 `TEXT RETRIEVAL SCAN` 和 `VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)`，说明过滤条件在向量扫描之前生效。

## RRF 融合而不是分数相加

全文分数和 L2 距离量纲不同，直接相加没有意义。CiteFlow 用 Reciprocal Rank Fusion：

```
rrf = Σ 1 / (60 + rank_channel)
```

只依赖排名、不依赖原始分数，因此两路召回规模不同也不会互相压制。k=60 是常用默认值，排名靠前的候选仍有明显优势，但不会出现“单通道霸榜”。

## 工程上的四个坑

1. 向量维度必须和嵌入模型一致，表定义 `VECTOR(384)` 对应 multilingual MiniLM。
2. 中文查询与英文原文的相似度依赖多语言模型，纯英文模型会退化成关键词匹配。
3. `MATCH` 在没有全文命中时返回空集，不能把它当成“分数为 0”继续排序。
4. 每条片段要保留页码与标题，否则回答阶段无法给出可回查的引用。
