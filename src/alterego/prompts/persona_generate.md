# 生成人设

根据下面的描述，生成一份完整、具体、**自洽**的人设。

用户填的原始描述：

```
{user_input}
```

## 输出要求

只输出 JSON：

```json
{
  "name": "",
  "age": 0,
  "occupation": "",
  "city": "",
  "big_five": {
    "openness": 0.5,
    "conscientiousness": 0.5,
    "extraversion": 0.5,
    "agreeableness": 0.5,
    "neuroticism": 0.5
  },
  "tone": "",
  "verbosity": "",
  "emoji_habit": "",
  "catchphrases": [],
  "typing_quirks": [],
  "likes": [],
  "dislikes": [],
  "habits": [],
  "fears": [],
  "desires": [],
  "backstory": "",
  "current_situation": "",
  "goals": [],
  "emotion_baseline": {"valence": 0.1, "arousal": 0.4, "fatigue": 0.2}
}
```

**约束**：

- 各项取值的含义见 `docs/design/01-architecture.md` § 3.2。
- `big_five` 五个维度都是 0~1 的浮点数，不要全给 0.5——那样等于没有人格。
- `tone` / `verbosity` / `emoji_habit` 用**具体**描述，不要「正常」「适中」：
  - ✅ `tone`: 「说话直，不太会拐弯，熟了之后爱损人」
  - ❌ `tone`: 「友好」
- `backstory` 写 3~5 句。要有**具体细节**（地名、年份、具体的人），
  不要写成一串形容词。
- 允许并鼓励留一些**不完美**：不会的东西、做不好的事、没说出口的心事。
  一个没有缺点的人设撑不起「像真人」这个目标。
- `catchphrases` 2~4 条。`typing_quirks` 是打字习惯（不用标点、爱发「嗯」、
  一句话拆成好几条…）。
