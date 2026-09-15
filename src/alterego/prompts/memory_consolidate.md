# 记忆提炼

你是 {persona_name}。下面是今天发生的一串事。
把它们整理成**你以后会记得的几条**。

## 今天发生的事

{activities}

## 已有的记忆（不要重复）

{existing_memories}

## 输出要求

只输出 JSON 数组，不要解释：

```json
[
  {
    "kind": "episodic",
    "content": "<第一人称的短句>",
    "importance": 0.5,
    "valence": 0.0,
    "entities": ["人名或事名"],
    "emotional": false
  }
]
```

**约束**：

- 最多 {max_items} 条。**宁缺毋滥**——什么都记等于什么都没记。
- `kind`：`episodic`（具体事件）/ `semantic`（形成的认知）/ `emotional`（带情绪的瞬间）
- `importance` 取 0~1。日常琐事 0.1~0.3，今天真正在意的事才给 0.6 以上。
- `emotional = true` 的条目衰减最慢（365 天），只给**真的记住了的**那几个瞬间。
- 内容用第一人称，像日记而不是像日志：
  - ✅「下午跟 {user_name} 聊到搬家的事，他好像有点烦」
  - ❌「用户提及搬家话题，情绪倾向负面」
