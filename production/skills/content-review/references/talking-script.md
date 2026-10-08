# 口播审核

用于 `content_route=talking_script`。审核当前口播正文与断言证据，不以分镜、音效或画面字段缺失为由拒收。

## 逐稿标准

- `originality`：观点和论证是否形成原创表达，而非参考同义改写。
- `reference_reuse`：是否复用参考的连续表达，尤其是 `do_not_reuse` 片段。
- `evidence_support`：正文事实是否被绑定资料支持；无事实表述视为通过。
- `style_fit`：是否符合明确的账号风格与创作要求。
- `spoken_quality`：是否自然可朗读、标题与正文一致，无论文腔或控制标记；不强制提问开头或固定模板。
- `no_fabrication`：是否编造来源、URL、数据、结论或未经提供的亲历。

主观上的“够不够抓人”不替代上述标准。需要失败时引用正文真实原话，并说明违反哪项要求及可执行的修正；不将来源原句必须照抄进正文设为门槛。

## 输出契约

```json
{
  "candidates": [
    {
      "candidate_id": "给定 ID",
      "criteria": {
        "originality": "pass",
        "reference_reuse": "pass",
        "evidence_support": "pass",
        "style_fit": "pass",
        "spoken_quality": "pass",
        "no_fabrication": "pass"
      },
      "findings": []
    }
  ],
  "set": {"distinct": true, "rewrite_slots": [], "findings": []}
}
```

每项标准为 `pass` 或 `fail`。仅对 `review_ids` 输出逐稿结论；集合检查真实观点或论证路径，不只比较标题。不因另两篇失败阻止当前通过稿进入业务采用流程。失败草稿保留供用户编辑或重审，定向重写须由用户明确发起，不自动循环返工。

当前 `core5/extended8` 人工评分接口仅适用于分镜，不为口播虚构该评分记录；口播采用按业务层现行校验执行。
